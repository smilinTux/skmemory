"""Memory schema v2 (SKMEM-P3-01): fields, v1 compatibility, byte stability."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from skmemory.backends.file_backend import FileBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory

V2 = (
    "kind",
    "outcome",
    "event_at",
    "valid_from",
    "valid_to",
    "supersedes_id",
    "source_tier",
    "schema_version",
)

# A v1 file as written by a pre-v2 node: no v2 keys at all.
V1_DICT = {
    "id": "v1-memory-0001",
    "created_at": "2026-01-02T03:04:05+00:00",
    "updated_at": "2026-01-02T03:04:05+00:00",
    "layer": "mid-term",
    "role": "general",
    "title": "Old note",
    "content": "Written before schema v2.",
    "summary": "",
    "tags": ["a"],
    "source": "manual",
    "source_ref": "",
    "emotional": {
        "intensity": 2.0,
        "valence": 0.5,
        "labels": ["joy"],
        "resonance_note": "",
        "cloud9_achieved": False,
    },
    "related_ids": [],
    "parent_id": None,
    "context_tag": "@chef-only",
    "intent": "",
    "integrity_hash": "",
    "metadata": {},
}


def _v1_sealed_hash() -> str:
    # The pre-v2 formula, spelled out so a future change to it is caught.
    payload = "v1-memory-0001:Old note:Written before schema v2.:joy@2.0/+0.5"
    return hashlib.sha256(payload.encode()).hexdigest()


def test_defaults():
    m = Memory(title="t", content="c")
    assert m.kind == "episode" and m.outcome is None
    assert m.event_at == m.created_at == m.valid_from
    assert m.valid_to is None and m.supersedes_id is None
    assert m.source_tier == "self-generated" and m.schema_version == 2


@pytest.mark.parametrize(
    "field,bad", [("kind", "goal"), ("outcome", "win"), ("source_tier", "rumor")]
)
def test_invalid_vocabulary_rejected(field, bad):
    with pytest.raises(ValidationError):
        Memory(title="t", content="c", **{field: bad})


def test_v1_file_loads_and_is_not_rewritten(tmp_path):
    be = FileBackend(str(tmp_path))
    d = dict(V1_DICT, integrity_hash=_v1_sealed_hash())
    path = tmp_path / "mid-term" / "v1-memory-0001.json"
    raw = json.dumps(d, indent=2).encode()
    path.write_bytes(raw)
    before = path.stat()
    m = be.load("v1-memory-0001")
    assert m is not None
    assert m.event_at == d["created_at"] and m.kind == "episode"
    assert be.list_memories() and be.search_text("schema")
    assert path.read_bytes() == raw  # content identical
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert sorted(os.listdir(path.parent)) == ["v1-memory-0001.json"]


def test_v1_integrity_hash_and_seal_still_verify(tmp_path):
    d = dict(V1_DICT, integrity_hash=_v1_sealed_hash())
    m = Memory(**d)
    assert m.compute_integrity_hash() == _v1_sealed_hash()
    assert m.verify_integrity() is True
    # Setting every v2 field to a non-default value must not move the hash.
    m2 = Memory(
        **d,
        kind="rule",
        outcome="lesson",
        source_tier="web",
        supersedes_id="x",
        valid_to="2030-01-01T00:00:00+00:00",
    )
    assert m2.compute_integrity_hash() == _v1_sealed_hash()
    assert m2.verify_integrity() is True


def test_v1_at_rest_sidecar_checksum_uses_disk_bytes(tmp_path):
    """verify-on-read hashes the raw on-disk bytes, so a v1 file's checksum
    is unaffected by new model defaults as long as the file is untouched."""
    from skmemory import sealing

    be = FileBackend(str(tmp_path))
    raw = json.dumps(V1_DICT, indent=2).encode()
    (tmp_path / "mid-term" / "v1-memory-0001.json").write_bytes(raw)
    expected = sealing.CHECKSUM_PREFIX + hashlib.sha256(raw).hexdigest()
    assert sealing.content_checksum(raw) == expected
    assert be.load("v1-memory-0001") is not None
    assert (
        sealing.content_checksum((tmp_path / "mid-term" / "v1-memory-0001.json").read_bytes())
        == expected
    )


def test_v2_roundtrip_byte_stable(tmp_path):
    be = FileBackend(str(tmp_path))
    m = Memory(
        id="v2-0001",
        title="t",
        content="c",
        kind="procedure",
        outcome="success",
        source_tier="user-stated",
        supersedes_id="old-1",
        valid_to="2031-01-01T00:00:00+00:00",
    )
    be.save(m)
    p = tmp_path / "short-term" / "v2-0001.json"
    first = p.read_bytes()
    be.save(be.load("v2-0001"))
    assert p.read_bytes() == first


def test_unknown_fields_survive_resave(tmp_path):
    be = FileBackend(str(tmp_path))
    d = dict(V1_DICT, future_field={"a": 1}, kind="fact")
    (tmp_path / "mid-term" / "v1-memory-0001.json").write_text(json.dumps(d))
    be.save(be.load("v1-memory-0001"))
    out = json.loads((tmp_path / "mid-term" / "v1-memory-0001.json").read_text())
    assert out["future_field"] == {"a": 1} and out["kind"] == "fact"


def test_old_node_behaviour_documented():
    """An extra='ignore' model (pre-v2 nodes) drops v2 keys on re-save."""

    class OldMemory(BaseModel):
        model_config = ConfigDict(extra="ignore")
        id: str
        title: str
        content: str

    new = Memory(id="a", title="t", content="c", kind="rule").model_dump()
    assert not set(V2) & set(OldMemory(**new).model_dump())


def _old_sqlite(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE memories (id TEXT PRIMARY KEY, title TEXT NOT NULL,
      layer TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'general', tags TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT 'manual', source_ref TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '', content_preview TEXT NOT NULL DEFAULT '',
      emotional_intensity REAL NOT NULL DEFAULT 0.0, emotional_valence REAL NOT NULL DEFAULT 0.0,
      emotional_labels TEXT NOT NULL DEFAULT '', cloud9_achieved INTEGER NOT NULL DEFAULT 0,
      parent_id TEXT, related_ids TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL, file_path TEXT NOT NULL)""")
    con.execute(
        "INSERT INTO memories (id,title,layer,created_at,updated_at,file_path)"
        " VALUES ('old','t','short-term','x','x','/nope')"
    )
    con.commit()
    con.close()


def test_sqlite_alter_on_open_twice_and_v2_columns_written(tmp_path):
    _old_sqlite(tmp_path / "index.db")
    be = SQLiteBackend(str(tmp_path))
    be2 = SQLiteBackend(str(tmp_path))  # second opener, columns already there
    cols = {r[1] for r in be._get_conn().execute("PRAGMA table_info(memories)")}
    assert set(V2) <= cols
    old = (
        be._get_conn()
        .execute("SELECT schema_version, kind FROM memories WHERE id='old'")
        .fetchone()
    )
    assert tuple(old) == (1, "episode")  # pre-existing rows stay v1
    be2.save(
        Memory(
            id="n1",
            title="t",
            content="c",
            kind="fact",
            outcome="lesson",
            source_tier="web",
            supersedes_id="old",
        )
    )
    r = be._get_conn().execute("SELECT * FROM memories WHERE id='n1'").fetchone()
    assert (
        r["kind"],
        r["outcome"],
        r["source_tier"],
        r["supersedes_id"],
        r["schema_version"],
    ) == ("fact", "lesson", "web", "old", 2)
    assert r["event_at"] and r["valid_from"] and r["valid_to"] is None


def test_sqlite_alter_tolerates_concurrent_opener(tmp_path, monkeypatch):
    """Another opener adds the column after our PRAGMA but before our ALTER."""
    _old_sqlite(tmp_path / "index.db")
    be = SQLiteBackend(str(tmp_path))
    con = be._get_conn()
    con.execute("ALTER TABLE memories DROP COLUMN kind")
    con.commit()
    real_execute = con.execute

    class Proxy:
        def __getattr__(self, n):
            return getattr(con, n)

        def execute(self, sql, *a):
            cur = real_execute(sql, *a)
            if sql.startswith("PRAGMA table_info(memories)"):
                rows = cur.fetchall()
                real_execute(
                    "ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'episode'"
                )
                return types.SimpleNamespace(fetchall=lambda: rows)
            return cur

    monkeypatch.setattr(be, "_get_conn", lambda: Proxy())
    be._init_schema()  # must not raise "duplicate column name"


# --- skmem-pg migration (live throwaway DB; skipped when unreachable) -------

MIGRATION = Path(__file__).resolve().parent.parent / "deploy" / "skmem-pg"


def _live_conn():
    dsn = os.environ.get("SKMEMORY_PG_DSN", "")
    if not dsn or "127.0.0.1" not in dsn and "localhost" not in dsn:
        pytest.skip("needs a local throwaway SKMEMORY_PG_DSN")
    import psycopg

    try:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=3)
    except Exception:
        pytest.skip("throwaway skmem-pg unreachable")


def _run_sql_file(conn, name: str) -> None:
    text = "\n".join(
        ln
        for ln in (MIGRATION / name).read_text().splitlines()
        if not ln.lstrip().startswith("\\")
    )
    conn.execute(text)


def test_manifest_lists_v2_migration():
    lines = (MIGRATION / "migrations.txt").read_text().splitlines()
    assert "06-memory-schema-v2.sql | verify-memory-schema-v2.sql" in lines


def test_pg_migration_is_idempotent_and_save_writes_columns(monkeypatch):
    conn = _live_conn()
    # _live_conn only passes for a loopback DSN, i.e. the throwaway container,
    # whose database happens to share the production name the guard blocks.
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", "1")
    _run_sql_file(conn, "06-memory-schema-v2.sql")
    _run_sql_file(conn, "06-memory-schema-v2.sql")  # second run must be a no-op
    got = dict(conn.execute((MIGRATION / "verify-memory-schema-v2.sql").read_text()).fetchall())
    assert got == {"columns": "8", "supersedes_idx": "1"}

    from skmemory.backends.pgvector_backend import PGVectorBackend

    be = PGVectorBackend(agent="test-agent", embed_fn=lambda _t: [0.1] * 1024)
    m = Memory(
        id="p301-live",
        title="t",
        content="c",
        kind="rule",
        outcome="lesson",
        source_tier="web",
        supersedes_id="p301-old",
    )
    try:
        be.save(m)
        row = conn.execute(
            "SELECT kind, outcome, source_tier, supersedes_id, schema_version, "
            "event_at IS NOT NULL, valid_to FROM memories WHERE id = 'p301-live'"
        ).fetchone()
        assert row == ("rule", "lesson", "web", "p301-old", 2, True, None)
        assert (
            Memory.model_validate_json(
                conn.execute(
                    "SELECT memory_json::text FROM memories WHERE id='p301-live'"
                ).fetchone()[0]
            ).kind
            == "rule"
        )
    finally:
        conn.execute("DELETE FROM memories WHERE id = 'p301-live'")


def test_pg_save_skips_typed_columns_when_unmigrated(monkeypatch):
    from skmemory.backends import pgvector_backend as mod

    class Cur:
        def __init__(self):
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchone(self):
            return (0,) if self.sql and "information_schema" in self.sql[-1] else None

    cur = Cur()

    class Conn:
        closed = False

        def cursor(self):
            return cur

    be = mod.PGVectorBackend(agent="test-agent", embed_fn=lambda _t: [0.1] * 1024)
    be._conn = Conn()
    be.save(Memory(id="u1", title="t", content="c"))
    insert = [q for q in cur.sql if q.startswith("INSERT")][0]
    assert "schema_version" not in insert and "memory_json" in insert


# --- tolerant reads, honest schema_version, promote ----------------------

BAD = {"kind": "hypothesis", "outcome": "sucess", "source_tier": "user_stated"}


def _bad_dict(mid="bad-0001"):
    return dict(V1_DICT, id=mid, schema_version=2, **BAD)


def _assert_degraded(m):
    assert (m.kind, m.outcome, m.source_tier) == ("episode", None, "self-generated")
    assert m.metadata["schema_v2_unknown"] == BAD


def test_public_construction_stays_strict():
    with pytest.raises(ValidationError):
        Memory(title="t", content="c", kind="hypothesis")


def test_file_backend_reads_unknown_values_leniently(tmp_path, caplog):
    be = FileBackend(str(tmp_path))
    p = tmp_path / "mid-term" / "bad-0001.json"
    p.write_text(json.dumps(_bad_dict()))
    raw = p.read_bytes()
    _assert_degraded(be.load("bad-0001"))
    assert [m.id for m in be.list_memories()] == ["bad-0001"]
    assert [m.id for m in be.search_text("schema")] == ["bad-0001"]
    assert p.read_bytes() == raw


def test_unknown_value_logged_once(tmp_path, caplog):
    import logging

    from skmemory import models

    models._warned_unknown.clear()
    be = FileBackend(str(tmp_path))
    (tmp_path / "mid-term" / "bad-0001.json").write_text(json.dumps(_bad_dict()))
    with caplog.at_level(logging.WARNING, logger="skmemory.models"):
        be.load("bad-0001")
        be.load("bad-0001")
    assert len([r for r in caplog.records if "unknown kind" in r.getMessage()]) == 1


def test_sqlite_load_list_reindex_see_unknown_values(tmp_path):
    be = SQLiteBackend(str(tmp_path))
    p = tmp_path / "mid-term" / "bad-0001.json"
    p.write_text(json.dumps(_bad_dict()))
    _assert_degraded(be.load("bad-0001"))
    be.reindex()
    row = be._get_conn().execute("SELECT kind, outcome, source_tier FROM memories").fetchall()
    assert [tuple(r) for r in row] == [("episode", None, "self-generated")]
    _assert_degraded(be.load("bad-0001"))
    assert [m.id for m in be.list_memories()] == ["bad-0001"]
    # And a fresh backend with no index row at all still finds it.
    (tmp_path / "index.db").unlink()
    be2 = SQLiteBackend(str(tmp_path))
    assert be2.reindex() >= 1 or be2.load("bad-0001") is not None
    assert [m.id for m in be2.list_memories()] == ["bad-0001"]


def test_v1_file_reports_schema_version_1_without_rewrite(tmp_path):
    for be in (FileBackend(str(tmp_path / "f")), SQLiteBackend(str(tmp_path / "s"))):
        base = Path(be.base_path)
        p = base / "mid-term" / "v1-memory-0001.json"
        raw = json.dumps(V1_DICT, indent=2).encode()
        p.write_bytes(raw)
        m = be.load("v1-memory-0001")
        assert m.schema_version == 1
        assert p.read_bytes() == raw
    assert Memory(title="t", content="c").schema_version == 2


def test_promote_does_not_copy_supersession():
    m = Memory(title="t", content="c", supersedes_id="old", valid_to="2031-01-01T00:00:00+00:00")
    from skmemory.models import MemoryLayer

    p = m.promote(MemoryLayer.LONG)
    assert p.supersedes_id is None and p.valid_to is None and p.parent_id == m.id


def test_pgvector_load_unknown_values_on_throwaway(monkeypatch):
    conn = _live_conn()
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", "1")
    from skmemory.backends.pgvector_backend import PGVectorBackend

    be = PGVectorBackend(agent="test-agent", embed_fn=lambda _t: [0.1] * 1024)
    d = _bad_dict("p301-bad")
    conn.execute(
        "INSERT INTO memories (id, agent, layer, title, content, memory_json) "
        "VALUES ('p301-bad','test-agent','mid-term','t','c', %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET memory_json = EXCLUDED.memory_json",
        (json.dumps(d),),
    )
    try:
        _assert_degraded(be.load("p301-bad"))
    finally:
        conn.execute("DELETE FROM memories WHERE id = 'p301-bad'")


def test_has_v2_columns_probe_is_cached(monkeypatch):
    from skmemory.backends import pgvector_backend as mod

    probes = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            probes.append(sql)

        def fetchone(self):
            return (0,)

    class Conn:
        closed = False

        def cursor(self):
            return Cur()

    be = mod.PGVectorBackend(agent="test-agent", embed_fn=lambda _t: [0.1] * 1024)
    assert be._has_v2_columns(Conn()) is False
    assert be._has_v2_columns(Conn()) is False
    assert len(probes) == 1


def test_resave_restores_raw_unknown_values(tmp_path):
    be = FileBackend(str(tmp_path))
    p = tmp_path / "mid-term" / "bad-0001.json"
    p.write_text(json.dumps(_bad_dict(), indent=2))
    be.save(be.load("bad-0001"))
    out = json.loads(p.read_text())
    assert {k: out[k] for k in BAD} == BAD
    assert "schema_v2_unknown" not in out["metadata"]
    # byte-stable on a second pass
    first = p.read_bytes()
    be.save(be.load("bad-0001"))
    assert p.read_bytes() == first


def test_deliberate_change_beats_restore_and_promote_survives():
    m = Memory.from_stored(_bad_dict())
    m.kind = "rule"
    d = m.model_dump()
    assert d["kind"] == "rule" and d["outcome"] == "sucess"
    from skmemory.models import MemoryLayer

    assert Memory.from_stored(_bad_dict()).promote(MemoryLayer.LONG).kind == "episode"
