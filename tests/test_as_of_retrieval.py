"""as_of retrieval and memory_history (SKMEM-P3-03).

Tmp SQLite stores and fakes only; the pg SQL / AGE Cypher forms are asserted at
the string level here and executed against the throwaway DB when it is set.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os

import pytest
from click.testing import CliRunner

from skmemory import mcp_server
from skmemory.audience import READER_ENV
from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore
from skmemory.validity import normalize_as_of, valid_at

OLD, MID, NEW = (
    "2026-01-01T00:00:00+00:00",
    "2026-06-01T00:00:00+00:00",
    "2026-10-01T00:00:00+00:00",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    monkeypatch.delenv("SKMEMORY_SEARCH_FUSION", raising=False)


def _mem(mid, content, created, **kw):
    return Memory(
        id=mid,
        title=kw.pop("title", mid),
        content=content,
        layer=MemoryLayer.MID,
        created_at=created,
        updated_at=created,
        **kw,
    )


def _store(tmp_path, *mems):
    primary = SQLiteBackend(str(tmp_path))
    for m in mems:
        primary.save(m)
    return MemoryStore(primary=primary)


def _chain_store(tmp_path):
    return _store(
        tmp_path,
        _mem("old", "gateway port is 8000", OLD, valid_to=MID),
        _mem("new", "gateway port is 9000", MID, supersedes_id="old"),
        _mem("other", "gateway uses tls", NEW),
    )


# ------------------------------------------------------------- the rule


def test_valid_at_rule():
    m = _mem("a", "x", MID, valid_to=NEW)
    assert valid_at(m, MID)  # starts at valid_from
    assert not valid_at(m, OLD)  # before it starts
    assert valid_at(m, "2026-09-30T00:00:00+00:00")
    assert not valid_at(m, NEW)  # closed AT the instant is closed
    v1 = Memory.model_validate({**_mem("b", "x", MID).model_dump(), "valid_from": None})
    v1.valid_from = None  # a v1 row as stored: no valid_from
    assert valid_at(v1, NEW) and not valid_at(v1, OLD)  # created_at stands in
    assert valid_at(_mem("c", "x", OLD), "2099-01-01T00:00:00+00:00")  # null valid_to is open


def test_normalize_as_of():
    assert normalize_as_of(None) is None and normalize_as_of("") is None
    assert normalize_as_of("2026-06-01T02:00:00+02:00") == "2026-06-01T00:00:00+00:00"
    assert normalize_as_of("2026-06-01") == "2026-06-01T00:00:00+00:00"
    assert normalize_as_of("2026-06-01T00:00:00Z") == "2026-06-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        normalize_as_of("last tuesday")


# ------------------------------------------------------------- store.search


@pytest.mark.parametrize("fusion", ["off", "on"])
def test_search_as_of_hides_closed_and_future(tmp_path, monkeypatch, fusion):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", fusion)
    s = _chain_store(tmp_path)
    ids = lambda t: {m.id for m in s.search("gateway port", as_of=t)}  # noqa: E731
    assert ids("2026-03-01T00:00:00+00:00") == {"old"}
    assert ids("2026-07-01T00:00:00+00:00") == {"new"}
    assert ids(MID) == {"new"}  # old closed at exactly MID
    assert ids("2025-01-01T00:00:00+00:00") == set()


@pytest.mark.parametrize("fusion", ["off", "on"])
def test_default_hides_nothing(tmp_path, monkeypatch, fusion):
    """No as_of means no temporal filter: a closed fact is still returned."""
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", fusion)
    s = _chain_store(tmp_path)
    assert {m.id for m in s.search("gateway port")} == {"old", "new"}


def test_v1_row_without_valid_from_falls_back_to_created_at(tmp_path):
    s = _chain_store(tmp_path)
    conn = s.primary._get_conn()
    conn.execute("UPDATE memories SET valid_from = NULL, valid_to = NULL WHERE id='old'")
    conn.commit()
    got = s.primary.search_lexical_local("gateway port", as_of="2026-03-01T00:00:00+00:00")
    assert {m.id for m in got} == {"old"}
    got = s.primary.search_lexical_local("gateway port", as_of="2025-03-01T00:00:00+00:00")
    assert got == []  # created_at 2026-01 is after as_of
    # valid_to null: open forever, so old is valid again after MID
    got = s.primary.search_lexical_local("gateway port", as_of="2026-12-01T00:00:00+00:00")
    assert "old" in {m.id for m in got}


def test_lexical_local_arm_is_filtered_in_sql(tmp_path):
    s = _chain_store(tmp_path)
    got = s.primary.search_lexical_local("gateway", limit=1, as_of="2026-07-01T00:00:00+00:00")
    # limit 1: the filter ran before LIMIT, or the closed row could have taken the slot
    assert len(got) == 1 and got[0].id in {"new", "other"} and got[0].id != "old"


def test_unparseable_as_of_raises(tmp_path):
    with pytest.raises(ValueError):
        _chain_store(tmp_path).search("gateway", as_of="yesterday-ish")


def test_as_of_with_reader_still_enforces_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    s = _store(
        tmp_path,
        _mem("pub", "gateway port 1", OLD, context_tag="@public"),
        _mem("priv", "gateway port 2", OLD, context_tag="@chef-only"),
    )
    got = s.search("gateway port", as_of=MID, reader="@public")
    assert {m.id for m in got} == {"pub"}


class _Vec:
    """Vector backend that predates as_of (no kwarg) and returns a closed row."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search_semantic(
        self, query, limit=10, *, layer=None, tags=None, context_tag=None, source=None
    ):
        self.calls.append("semantic")
        return list(self.rows)


class _VecAsOf(_Vec):
    def search_semantic(
        self, query, limit=10, *, layer=None, tags=None, context_tag=None, source=None, as_of=None
    ):
        self.calls.append(("semantic", as_of))
        return list(self.rows)


def test_arm_without_as_of_is_not_failed_and_is_still_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    s = _chain_store(tmp_path)
    old = s.primary.load("old")
    s.vector = _Vec([old])
    out = s.search_fused("zzz", as_of="2026-07-01T00:00:00+00:00", return_report=True)
    assert s.vector.calls == ["semantic"]  # called (not dropped by a TypeError)
    ids = [] if out[0] is None else [m.id for m in out[0]]
    assert "old" not in ids  # and its closed row did not get through


def test_arm_with_as_of_receives_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    s = _chain_store(tmp_path)
    s.vector = _VecAsOf([])
    s.search("gateway", as_of="2026-07-01T00:00:00+00:00")
    assert s.vector.calls == [("semantic", "2026-07-01T00:00:00+00:00")]
    s.vector.calls.clear()
    s.search("gateway")
    assert s.vector.calls == [("semantic", None)] or s.vector.calls == []  # never sent when unset


def test_graph_arm_receives_as_of_and_rechecks(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_GRAPH_ARM", "on")  # the arm is off by default
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    s = _chain_store(tmp_path)
    seen = {}

    class G:
        def search_by_entity_tokens(
            self,
            query,
            limit=20,
            *,
            layer=None,
            tags=None,
            context_tag=None,
            source=None,
            as_of=None,
        ):
            seen["as_of"] = as_of
            return [{"id": "old"}, {"id": "new"}]  # ignores the filter on purpose

    s.graph = G()
    got = s.search("gateway", as_of="2026-07-01T00:00:00+00:00")
    assert seen["as_of"] == "2026-07-01T00:00:00+00:00"
    assert "old" not in {m.id for m in got}


# ------------------------------------------------------------- SQL / Cypher shape


def test_age_filter_clause_is_a_coarse_prefilter():
    where, params = AGEGraphBackend._memory_filter(None, None, None, None, as_of=MID)
    # date-wide bounds, never an exact lexical compare against the instant text
    assert "$as_of_hi" in where and "$as_of_lo" in where and MID not in params.values()
    assert params["as_of_hi"] == "2026-06-03" and params["as_of_lo"] == "2026-05-31"
    assert AGEGraphBackend._memory_filter(None, None, None, None) == ("", {})


needs_psycopg = pytest.mark.skipif(
    importlib.util.find_spec("psycopg") is None, reason="psycopg not installed"
)


class _Cur:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)


class _Conn:
    closed = False

    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


@needs_psycopg
@pytest.mark.parametrize("migrated", [True, False])
def test_pg_arms_push_as_of_into_where(monkeypatch, migrated):
    from skmemory.backends.pgvector_backend import PGVectorBackend

    be = PGVectorBackend(agent="lumina")
    cur = _Cur()
    be._conn = _Conn(cur)
    be._embed = lambda q: [0.1, 0.2, 0.3]
    be._v2_columns = migrated
    be._bm25_available = False
    be.search_semantic("q", as_of=MID)
    be.search_lexical("q", as_of=MID)
    be.list_memories(as_of=MID)
    assert len(cur.executed) == 3
    for sql, params in cur.executed:
        assert "COALESCE(" in sql and "created_at) <= %s::timestamptz" in sql
        assert sql.index("<= %s::timestamptz") < sql.index("LIMIT")
        assert params.count(MID) == 2
        assert ("valid_from" in sql and "memory_json->>'valid_from'" not in sql) == migrated


# ------------------------------------------------------------- memory_history


def _call(store, monkeypatch, name, **args):
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    return asyncio.run(mcp_server.call_tool(name, args))[0].text


def _hist_store(tmp_path):
    return _store(
        tmp_path,
        _mem("a", "v1", OLD, valid_to=MID, context_tag="@public"),
        _mem("b", "v2", MID, valid_to=NEW, supersedes_id="a", context_tag="@chef-only"),
        _mem("c", "v3", NEW, supersedes_id="b", context_tag="@public"),
    )


def test_history_owner_sees_whole_chain_from_any_member(tmp_path):
    s = _hist_store(tmp_path)
    for start in ("a", "b", "c"):
        h = s.history(start)
        assert [r["id"] for r in h] == ["a", "b", "c"]
        assert [r["id"] for r in h if r["requested"]] == [start]
    assert [r["current"] for r in s.history("a")] == [False, False, True]
    assert s.history("nope") == []


def test_history_reader_is_cut_at_hidden_member(tmp_path):
    s = _hist_store(tmp_path)
    # b is hidden from @public: from a nothing past b, from c nothing before b
    ha, hc = s.history("a", reader="@public"), s.history("c", reader="@public")
    assert [r["id"] for r in ha] == ["a"] and [r["id"] for r in hc] == ["c"]
    assert s.history("b", reader="@public") == []  # hidden start is not-found
    blob = json.dumps([ha, hc])
    assert '"b"' not in blob and "v2" not in blob  # no link to or text of the hidden member
    assert ha[0]["superseded_by"] is None and hc[0]["supersedes_id"] is None


def test_memory_history_tool_policy_and_reader(tmp_path, monkeypatch):
    assert mcp_server.TOOL_READER_POLICY["memory_history"][0] == "filtered"
    tool = next(t for t in asyncio.run(mcp_server.list_tools()) if t.name == "memory_history")
    assert "reader" in tool.inputSchema["properties"]
    s = _hist_store(tmp_path)
    assert [
        r["id"] for r in json.loads(_call(s, monkeypatch, "memory_history", memory_id="a"))
    ] == [
        "a",
        "b",
        "c",
    ]
    hidden = _call(s, monkeypatch, "memory_history", memory_id="b", reader="@public")
    missing = _call(s, monkeypatch, "memory_history", memory_id="zz", reader="@public")
    assert "not found" in hidden.lower() and hidden.replace("b", "X") == missing.replace("zz", "X")
    monkeypatch.setenv(READER_ENV, "@public")  # env ceiling too
    assert "b" not in [
        r["id"] for r in json.loads(_call(s, monkeypatch, "memory_history", memory_id="a"))
    ]


def test_memory_search_tool_takes_as_of(tmp_path, monkeypatch):
    s = _chain_store(tmp_path)
    out = json.loads(
        _call(
            s,
            monkeypatch,
            "memory_search",
            query="gateway port",
            as_of="2026-03-01T00:00:00+00:00",
        )
    )
    assert [m["id"] for m in out] == ["old"]
    tool = next(t for t in asyncio.run(mcp_server.list_tools()) if t.name == "memory_search")
    assert "as_of" in tool.inputSchema["properties"]


# ------------------------------------------------------------- CLI


def test_cli_search_as_of_and_history(tmp_path, monkeypatch):
    from skmemory import cli as cli_mod

    s = _chain_store(tmp_path)
    monkeypatch.setattr(cli_mod, "MemoryStore", lambda *a, **k: s, raising=False)
    runner = CliRunner()
    # call the commands directly with a ready context object
    res = runner.invoke(
        cli_mod.search, ["gateway port", "--as-of", "2026-03-01"], obj={"store": s}
    )
    assert (
        res.exit_code == 0 and "old" in res.output and "new" not in res.output.replace("none", "")
    )
    bad = runner.invoke(cli_mod.search, ["x", "--as-of", "nonsense"], obj={"store": s})
    assert bad.exit_code != 0 and "as-of" in bad.output.lower()
    res = runner.invoke(cli_mod.history, ["new"], obj={"store": s})
    assert res.exit_code == 0 and res.output.index("old") < res.output.index("new")


# ------------------------------------------------------------- throwaway pg

_DSN = os.environ.get("SKMEMORY_PG_DSN")


@pytest.mark.skipif(
    not _DSN or importlib.util.find_spec("psycopg") is None, reason="no throwaway pg"
)
def test_pg_as_of_executes_on_real_postgres(monkeypatch, caplog):
    from skmemory.backends.pgvector_backend import PGVectorBackend

    be = PGVectorBackend(dsn=_DSN, agent="lumina")
    be._embed = lambda q: [1.0] + [0.0] * 1023
    conn = be._connection()
    try:
        for mid, created, vto in (("p303-old", OLD, MID), ("p303-new", MID, None)):
            m = _mem(mid, "p303 gateway port", created, valid_to=vto)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO memories (id, agent, layer, role, title, content, summary, tags, "
                    "source, created_at, updated_at, memory_json, embedding) VALUES "
                    "(%s,%s,'mid-term','ai',%s,%s,'',%s,'t',%s,%s,%s::jsonb,%s::vector)",
                    (
                        mid,
                        "lumina",
                        mid,
                        m.content,
                        [],
                        created,
                        created,
                        m.model_dump_json(),
                        str([1.0] + [0.0] * 1023),
                    ),
                )
        for migrated in (False, True):
            be._v2_columns = migrated
            if migrated:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE memories SET valid_from=created_at, valid_to=(memory_json->>'valid_to')::timestamptz WHERE agent='lumina' AND id LIKE 'p303-%'"
                    )
            ids = lambda t: {m.id for m in be.list_memories(as_of=t)}  # noqa: E731
            assert ids("2026-03-01T00:00:00+00:00") == {"p303-old"}
            assert ids("2026-07-01T00:00:00+00:00") == {"p303-new"}
            assert ids("2025-01-01T00:00:00+00:00") == set()
            sem = lambda t: {m.id for m in be.search_semantic("q", as_of=t)}  # noqa: E731
            assert sem("2026-03-01T00:00:00+00:00") == {"p303-old"}
            assert sem("2026-07-01T00:00:00+00:00") == {"p303-new"}
            assert sem("2025-01-01T00:00:00+00:00") == set()
            assert {
                m.id for m in be.search_lexical("p303 gateway", as_of="2026-07-01T00:00:00+00:00")
            } <= {"p303-new"}
            assert be._has_bm25_index()
            assert "BM25 lexical search failed" not in caplog.text  # BM25 kept, not ts_rank
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE agent='lumina' AND id LIKE 'p303-%'")


# ------------------------------------------------------------- default ranking


@pytest.mark.parametrize("fusion", ["on"])
def test_closed_record_ranks_below_its_open_head_without_as_of(tmp_path, monkeypatch, fusion):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", fusion)
    # old matches the query far better than new, so plain fusion would lead with it
    s = _store(
        tmp_path,
        _mem("old", "gateway port gateway port gateway port", OLD, valid_to=MID),
        _mem("new", "gateway port moved", MID, supersedes_id="old"),
        _mem("mid", "gateway port notes", NEW),
    )
    ids = [m.id for m in s.search("gateway port", limit=10)]
    assert set(ids) == {"old", "new", "mid"}  # nothing hidden
    assert ids.index("new") < ids.index("old")


def test_demote_is_stable_and_only_reorders():
    from skmemory.store import _demote_superseded

    ms = {
        "a": _mem("a", "x", OLD, valid_to=MID),
        "b": _mem("b", "x", MID, valid_to=NEW, supersedes_id="a"),
        "c": _mem("c", "x", NEW, supersedes_id="b"),
        "z": _mem("z", "x", NEW),
        "q": _mem("q", "x", OLD, valid_to=MID),  # closed, no known superseder: stays put
    }
    assert _demote_superseded(["a", "z", "b", "q", "c"], ms) == ["z", "q", "c", "b", "a"]
    assert _demote_superseded(["a", "z"], ms) == ["a", "z"]  # head not pooled: untouched


def test_memory_search_flags_superseded(tmp_path, monkeypatch):
    s = _chain_store(tmp_path)
    out = {
        m["id"]: m
        for m in json.loads(_call(s, monkeypatch, "memory_search", query="gateway port"))
    }
    assert out["old"]["superseded"] is True and out["old"]["superseded_by"] == "new"
    assert out["new"]["superseded"] is False and out["new"]["superseded_by"] is None


def test_class_bench_must_not_ranks_below_gold():
    """Conflict queries: every must_not memory that is returned ranks below every gold one."""
    import tempfile

    from skmemory.eval import class_bench as cb
    from skmemory.eval.fixtures import load_corpus, load_queries

    with tempfile.TemporaryDirectory() as home, cb._fusion_env():
        store = cb.build_production_store(home, load_corpus())
        checked = 0
        for q in load_queries():
            if q.query_class != "conflict" or not q.must_not_ids or not q.gold_ids:
                continue
            ids = [m.id for m in store.search(q.query, limit=10, as_of=q.as_of)]
            for bad in q.must_not_ids:
                if bad in ids:
                    assert all(ids.index(g) < ids.index(bad) for g in q.gold_ids if g in ids), q.id
                    checked += 1
        assert checked >= 2


# ------------------------------------------------------------- Z vs +00:00 boundary

BOUND = "2026-10-29T12:00:00"


@pytest.mark.parametrize("stored", ["+00:00", "Z"])
@pytest.mark.parametrize("bound", ["+00:00", "Z"])
def test_created_bounds_are_inclusive_exclusive_in_both_forms(tmp_path, stored, bound):
    s = _store(
        tmp_path,
        _mem("edge", "edge row", BOUND + "+00:00"),
        _mem("before", "before row", "2026-10-29T11:59:59+00:00"),
    )
    conn = s.primary._get_conn()
    conn.execute("UPDATE memories SET created_at = replace(created_at, '+00:00', ?)", (stored,))
    conn.commit()
    lo = BOUND + bound
    ids = lambda **k: {m.id for m in s.primary.list_memories(limit=10, **k)}  # noqa: E731
    assert ids(created_after=lo) == {"edge"}  # inclusive lower bound keeps the boundary row
    assert ids(created_before=lo) == {"before"}  # exclusive upper bound drops it


@pytest.mark.parametrize("form", ["+00:00", "Z"])
def test_as_of_boundary_with_either_stored_or_bound_form(tmp_path, form):
    s = _chain_store(tmp_path)
    conn = s.primary._get_conn()
    conn.execute(
        "UPDATE memories SET valid_to = replace(valid_to, '+00:00', ?) WHERE valid_to IS NOT NULL",
        (form,),
    )
    conn.commit()
    at_close = "2026-06-01T00:00:00" + form
    got = {
        m.id
        for m in s.primary.search_lexical_local("gateway port", as_of=normalize_as_of(at_close))
    }
    assert got == {"new"}  # old closed AT the boundary
    got = {m.id for m in s.search("gateway port", as_of=at_close)}
    assert got == {"new"}


def test_temporal_arm_keeps_row_on_the_window_bound(tmp_path, monkeypatch):
    """parse_time_window emits 'Z' bounds; a row stamped exactly on one is inside."""
    import functools
    from datetime import datetime

    import skmemory.store as store_mod

    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    monkeypatch.setattr(
        store_mod,
        "parse_time_window",
        functools.partial(
            store_mod.parse_time_window, now=datetime.fromisoformat("2026-11-01T12:00:00+00:00")
        ),
    )
    s = _store(tmp_path, _mem("edge", "unrelated words", "2026-10-29T12:00:00+00:00"))
    got = s.search_fused("what changed in the last 3 days")
    assert got and [m.id for m in got] == ["edge"]


# ------------------------------------------------------------- review fixes


def test_history_masks_hidden_successor_edit(tmp_path):
    s = _hist_store(tmp_path)  # a (public, closed by hidden b), b hidden, c public
    ha = s.history("a", reader="@public")
    assert len(ha) == 1
    assert (
        ha[0]["valid_to"] is None and ha[0]["current"] is True and ha[0]["superseded_by"] is None
    )
    # the owner still sees the real window
    owner = s.history("a")[0]
    assert owner["valid_to"] == MID and owner["current"] is False
    # a closed member whose successor IS visible keeps its window for the reader
    s2 = _store(
        tmp_path / "v",
        _mem("x", "v1", OLD, valid_to=MID, context_tag="@public"),
        _mem("y", "v2", MID, supersedes_id="x", context_tag="@public"),
    )
    hx = s2.history("x", reader="@public")
    assert [r["id"] for r in hx] == ["x", "y"] and hx[0]["valid_to"] == MID


def test_memory_search_does_not_name_hidden_superseder(tmp_path, monkeypatch):
    s = _store(
        tmp_path,
        _mem("pubold", "gateway port 1", OLD, valid_to=MID, context_tag="@public"),
        _mem("privnew", "gateway port 2", MID, supersedes_id="pubold", context_tag="@chef-only"),
    )
    s.primary.save(
        _mem(
            "pubold",
            "gateway port 1",
            OLD,
            valid_to=MID,
            context_tag="@public",
            metadata={"superseded_by": {"id": "privnew"}},
        )
    )
    out = json.loads(
        _call(s, monkeypatch, "memory_search", query="gateway port", reader="@public")
    )
    assert [m["id"] for m in out] == ["pubold"]
    assert out[0]["superseded_by"] is None and out[0]["superseded"] is False
    owner = json.loads(_call(s, monkeypatch, "memory_search", query="gateway port"))
    assert {m["id"]: m["superseded_by"] for m in owner}["pubold"] == "privnew"


def test_decay_cannot_put_a_closed_fact_above_its_head(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    s = _store(
        tmp_path,
        _mem("old", "gateway port gateway port", OLD, valid_to=MID),
        _mem("new", "gateway port moved", MID, supersedes_id="old"),
    )
    # a decay channel that floats the OLD record to the top
    monkeypatch.setattr(
        MemoryStore,
        "_apply_decay",
        lambda self, rs: sorted(rs, key=lambda m: m.id != "old"),
    )
    assert [m.id for m in s.search("gateway port")] == ["new", "old"]
