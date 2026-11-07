"""SQLite FTS5 local lexical arm (SKMEM-P5-03)."""

import sqlite3

import pytest

from skmemory import fts_local
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.backends.vaulted_backend import VaultedSQLiteBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore

LONG_TAIL = "filler " * 60  # pushes the needle past the 150-char preview


def _mem(title, content, **kw):
    return Memory(title=title, content=content, **kw)


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "m"))


def test_indexes_full_content_not_just_preview(backend):
    m = _mem("alpha", LONG_TAIL + "zebrafishneedle")
    backend.save(m)
    assert (
        "zebrafishneedle"
        not in backend._get_conn().execute("SELECT content_preview FROM memories").fetchone()[0]
    )
    assert [r.id for r in backend.search_lexical_local("zebrafishneedle")] == [m.id]


def test_bm25_ranks_better_match_first(backend):
    weak = _mem("one", "pelican " + LONG_TAIL)
    strong = _mem("pelican pelican", "pelican pelican pelican")
    backend.save(weak)
    backend.save(strong)
    assert backend.search_lexical_local("pelican")[0].id == strong.id


@pytest.mark.parametrize(
    "q",
    [
        "prb-6f069c5e",
        'say "hello',
        "tag:foo",
        "a AND",
        "NEAR(",
        "(((",
        '"',
        "*",
        "it's",
        "-x",
        "a:b:c d-e",
    ],
)
def test_hostile_queries_never_raise(backend, q):
    backend.save(_mem("prb-6f069c5e", "note about prb-6f069c5e"))
    backend.search_lexical_local(q)


def test_hyphenated_id_matches_as_phrase(backend):
    m = _mem("t", "incident prb-6f069c5e closed")
    backend.save(m)
    backend.save(_mem("t2", "prb unrelated 6f069c5e"))
    assert backend.search_lexical_local("prb-6f069c5e")[0].id == m.id


def test_sanitize_empty():
    assert fts_local.sanitize_match("  --  ") is None
    assert fts_local.sanitize_match("") is None


def test_filters_layer_tags_and_source_context(backend):
    a = _mem(
        "a", "needle", layer=MemoryLayer.LONG, tags=["x", "y"], source="s1", context_tag="@public"
    )
    b = _mem(
        "b", "needle", layer=MemoryLayer.SHORT, tags=["x"], source="s2", context_tag="@chef-only"
    )
    backend.save(a)
    backend.save(b)
    ids = lambda **kw: {m.id for m in backend.search_lexical_local("needle", **kw)}  # noqa: E731
    assert ids() == {a.id, b.id}
    assert ids(layer="long-term") == {a.id}
    assert ids(layer=MemoryLayer.SHORT) == {b.id}
    assert ids(tags=["x"]) == {a.id, b.id}
    assert ids(tags=["x", "y"]) == {a.id}  # AND
    assert ids(tags=["xy"]) == set()  # no substring tag match
    assert ids(source="s2") == {b.id}
    assert ids(context_tag="@public") == {a.id}
    assert ids(context_tag=["@public", "@chef-only"]) == {a.id, b.id}
    assert ids(context_tag=[]) == set()  # fails closed


def test_update_and_delete_keep_index_in_sync(backend):
    m = _mem("t", "oldword")
    backend.save(m)
    m.content = "newword"
    backend.save(m)
    assert backend.search_lexical_local("oldword") == []
    assert len(backend.search_lexical_local("newword")) == 1
    backend.delete(m.id)
    assert backend.search_lexical_local("newword") == []
    assert backend.fts_status()["indexed"] == 0


def test_reindex_rebuilds_fts(backend):
    m = _mem("t", LONG_TAIL + "uniqueterm")
    backend.save(m)
    backend.reindex(force=True)
    assert [r.id for r in backend.search_lexical_local("uniqueterm")] == [m.id]


def test_open_does_not_backfill_and_lazy_step_converges(tmp_path, monkeypatch):
    base = str(tmp_path / "m")
    b = SQLiteBackend(base_path=base)
    ms = [_mem(f"t{i}", f"body {i} legacyterm") for i in range(5)]
    for m in ms:
        b.save(m)
    # Simulate a pre-FTS index.db: wipe the FTS tables' contents.
    conn = b._get_conn()
    conn.execute("DELETE FROM memory_fts")
    conn.execute("DELETE FROM fts_meta")
    conn.commit()
    b.close()

    b2 = SQLiteBackend(base_path=base)
    assert b2.fts_status() == {"enabled": True, "total": 5, "indexed": 0, "missing": 5}
    monkeypatch.setenv("SKMEMORY_FTS_LAZY_BACKFILL_S", "0")
    assert b2.search_lexical_local("legacyterm") == []  # lazy step disabled
    monkeypatch.setenv("SKMEMORY_FTS_LAZY_BACKFILL_S", "5")
    assert len(b2.search_lexical_local("legacyterm")) == 5
    assert b2.fts_status()["missing"] == 0


def test_explicit_backfill_bounded(tmp_path):
    base = str(tmp_path / "m")
    b = SQLiteBackend(base_path=base)
    for i in range(6):
        b.save(_mem(f"t{i}", f"x{i}"))
    c = b._get_conn()
    c.execute("DELETE FROM memory_fts")
    c.execute("DELETE FROM fts_meta")
    c.commit()
    out = b.fts_backfill(max_rows=4)
    assert out["indexed"] == 4 and out["complete"] is False
    assert b.fts_backfill()["complete"] is True


def test_vaulted_backend_writes_no_fts(tmp_path):
    pytest.importorskip("cryptography")
    base = str(tmp_path / "v")
    plain = SQLiteBackend(base_path=base)
    plain.save(_mem("t", LONG_TAIL + "secretplaintext"))
    plain.close()
    v = VaultedSQLiteBackend(passphrase="EXAMPLE-DO-NOT-USE", base_path=base)
    v.save(_mem("t2", LONG_TAIL + "anothersecretword"))
    names = {r[0] for r in v._get_conn().execute("SELECT name FROM sqlite_master").fetchall()}
    assert not any("fts" in n for n in names)
    assert v.fts_status() == {"enabled": False}
    assert v.search_lexical_local("anothersecretword") == []
    v.close()
    raw = b"".join(p.read_bytes() for p in (tmp_path / "v").glob("index.db*") if p.is_file())
    assert b"anothersecretword" not in raw and b"secretplaintext" not in raw
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(str(tmp_path / "v" / "index.db")).execute("SELECT * FROM memory_fts")


def test_store_search_uses_local_arm_without_pg(tmp_path):
    backend = SQLiteBackend(base_path=str(tmp_path / "m"))
    store = MemoryStore(primary=backend, use_sqlite=False)
    m = store.snapshot("title", LONG_TAIL + "quokkaneedle")
    assert [r.id for r in store.search("quokkaneedle")] == [m.id]


def test_fused_adds_local_arm_alongside_pg_style_arm(tmp_path):
    backend = SQLiteBackend(base_path=str(tmp_path / "m"))

    class DeadPg:
        def search_lexical(self, *a, **k):
            raise RuntimeError("pg down")

        def search_semantic(self, *a, **k):
            return []

    store = MemoryStore(primary=backend, vector=DeadPg(), use_sqlite=False)
    m = store.snapshot("title", LONG_TAIL + "okapineedle")
    hits, report = store.search_fused("okapineedle", return_report=True)
    assert [h.id for h in hits] == [m.id]
    status = {a.name: a.status for a in report.arms}
    assert status["lexical"] == "failed" and status["lexical_local"] == "ok"


def test_cli_fts_backfill(tmp_path):
    from click.testing import CliRunner

    from skmemory.cli import cli

    backend = SQLiteBackend(base_path=str(tmp_path / "m"))
    backend.save(_mem("t", "cliterm"))
    c = backend._get_conn()
    c.execute("DELETE FROM memory_fts")
    c.execute("DELETE FROM fts_meta")
    c.commit()
    store = MemoryStore(primary=backend, use_sqlite=False)
    res = CliRunner().invoke(cli, ["fts-backfill"], obj={"store": store})
    assert res.exit_code == 0, res.output
    assert "Indexed 1" in res.output and "complete=True" in res.output
    assert backend.fts_status()["missing"] == 0
