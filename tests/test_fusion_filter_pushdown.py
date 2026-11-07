"""Filters (layer, tags, context_tag, source) reach the semantic and graph arms.

The fusion docstring promised every filter is pushed into each arm, but the
semantic and graph arms were called with the bare query, so a filtered search
could return wrong-layer or wrong-tag hits. These tests pin the push-down at
the SQL / Cypher level (no live database) and at the store wiring level.

``as_of`` is deliberately absent: schema v2 valid_from/valid_to do not exist
yet, and that parameter belongs to card P3-03.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.store import MemoryStore

# ---------------------------------------------------------------- pg side

needs_psycopg = pytest.mark.skipif(
    importlib.util.find_spec("psycopg") is None, reason="psycopg (pg extra) not installed"
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


class _Conn:
    closed = False

    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _pg(monkeypatch):
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    from skmemory.backends.pgvector_backend import PGVectorBackend

    be = PGVectorBackend(agent="lumina")
    cur = _Cur()
    be._conn = _Conn(cur)
    be._embed = lambda q: [0.1, 0.2, 0.3]
    return be, cur


@needs_psycopg
def test_semantic_pushes_every_filter_into_sql(monkeypatch):
    be, cur = _pg(monkeypatch)
    be.search_semantic(
        "q", limit=7, layer="long-term", tags=["a", "b"], context_tag="@public", source="cli"
    )
    sql, params = cur.executed[0]
    assert "layer=%s" in sql
    assert "tags @> %s" in sql
    assert "memory_json->>'context_tag'" in sql
    assert "source=%s" in sql
    assert "long-term" in params
    assert ["a", "b"] in params
    assert "@public" in params or ["@public"] in params
    assert "cli" in params
    assert params[-1] == 7
    # filters sit before ORDER/LIMIT, so LIMIT truncates only matching rows
    assert sql.index("tags @> %s") < sql.index("ORDER BY") < sql.index("LIMIT")


@needs_psycopg
def test_semantic_context_tag_list_is_any_of(monkeypatch):
    be, cur = _pg(monkeypatch)
    be.search_semantic("q", context_tag=["@public", "@community"])
    sql, params = cur.executed[0]
    assert "= ANY(%s)" in sql
    assert ["@public", "@community"] in params


@needs_psycopg
def test_semantic_unfiltered_sql_is_unchanged_in_shape(monkeypatch):
    be, cur = _pg(monkeypatch)
    be.search_semantic("q", limit=3)
    sql, params = cur.executed[0]
    assert "tags @>" not in sql and "layer=%s" not in sql and "context_tag" not in sql
    assert params[-1] == 3


@needs_psycopg
def test_semantic_hostile_values_are_bound_not_interpolated(monkeypatch):
    be, cur = _pg(monkeypatch)
    evil = "x'; DROP TABLE memories; --"
    be.search_semantic("q", tags=[evil], context_tag=evil, layer=evil, source=evil)
    sql, params = cur.executed[0]
    assert "DROP" not in sql
    assert any(evil == p or (isinstance(p, list) and evil in p) for p in params)


# -------------------------------------------------------------- graph side


class _Rec(AGEGraphBackend):
    def __init__(self):
        self.calls = []
        self.graph = "test_knowledge"
        self.dsn = "postgresql://u:p@127.0.0.1:1/none"
        self.agent = "test"

    def _cypher(self, query, params=None, cols="v agtype", timeout_ms=None):
        self.calls.append((query, params or {}))
        return []


def test_graph_entity_search_pushes_filters_as_parameters():
    b = _Rec()
    b.search_by_entity(
        "infra", limit=5, layer="mid-term", tags=["t1", "t2"], context_tag="@public", source="cli"
    )
    query, params = b.calls[0]
    assert "m.layer = $layer" in query
    assert "$tag0 IN m.tags" in query and "$tag1 IN m.tags" in query
    assert "m.context_tag = $context_tag" in query
    assert "m.source = $source" in query
    assert params["layer"] == "mid-term"
    assert params["tag0"] == "t1" and params["tag1"] == "t2"
    assert params["context_tag"] == "@public"
    assert params["source"] == "cli"
    assert (
        query.index("MATCH (m:Memory)") < query.index("m.layer = $layer") < query.index("RETURN")
    )


def test_graph_context_tag_list_uses_in_parameter():
    b = _Rec()
    b.search_by_entity("infra", context_tag=["@public", "@community"])
    query, params = b.calls[0]
    assert "m.context_tag IN $context_tags" in query
    assert params["context_tags"] == ["@public", "@community"]


def test_graph_hostile_tags_never_reach_the_query_text():
    b = _Rec()
    evil = "x'}) DETACH DELETE m //"
    b.search_by_entity("infra", tags=[evil], layer=evil, context_tag=evil, source=evil)
    query, params = b.calls[0]
    assert "DELETE" not in query and evil not in query
    assert evil in json.dumps(params)


def test_graph_unfiltered_query_is_unchanged():
    b = _Rec()
    b.search_by_entity("infra", limit=5)
    query, params = b.calls[0]
    assert "m.layer" not in query.split("RETURN")[0].split("MENTIONS")[1]
    assert set(params) == {"pattern", "limit"}


def test_token_search_forwards_filters_to_every_lookup():
    b = _Rec()
    b.search_by_entity_tokens("skgateway routing decision", limit=5, layer="long-term")
    assert len(b.calls) >= 2
    for query, params in b.calls:
        assert params["layer"] == "long-term"
        assert "m.layer = $layer" in query


# --------------------------------------------------------- store wiring


class _Mem:
    def __init__(self, i, layer="long-term", tags=(), ctx="@public"):
        self.id, self.title, self.layer, self.tags, self.context_tag = i, i, layer, list(tags), ctx


class _Vec:
    def __init__(self):
        self.kw = None

    def search_semantic(self, query, limit=10, **kw):
        self.kw = kw
        return []

    def search_text(self, query, limit=10, layer=None, tags=None, source=None):
        return []


class _Graph:
    def __init__(self, rows=()):
        self.kw = None
        self.rows = list(rows)

    def search_by_entity_tokens(self, query, limit=20, **kw):
        self.kw = kw
        return self.rows


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.delenv("SKMEMORY_SEARCH_FUSION", raising=False)
    return MemoryStore()


def test_store_search_passes_filters_to_both_arms(store):
    store.vector, store.graph = _Vec(), _Graph()
    store.search("alpha beta", tags=["x"], layer="long-term", source="cli", context_tag="@public")
    expected = {
        "layer": "long-term",
        "tags": ["x"],
        "context_tag": "@public",
        "source": "cli",
    }
    assert store.vector.kw == expected
    assert store.graph.kw == expected


def test_graph_arm_drops_stale_rows_that_fail_the_filter(store, monkeypatch):
    """The graph is a derived projection: its node can say a layer the flat
    file no longer has. Hydrated rows are re-checked against the filter."""
    ok = _Mem("ok", layer="long-term")
    stale = _Mem("stale", layer="short-term")
    store.graph = _Graph([{"id": "ok"}, {"id": "stale"}])
    monkeypatch.setattr(store, "recall", lambda i: {"ok": ok, "stale": stale}.get(i))
    out = store.search_fused("alpha beta", limit=5, layer="long-term")
    assert [m.id for m in out] == ["ok"]


def test_graph_arm_blocks_private_memory_for_public_reader(store, monkeypatch):
    priv = _Mem("priv", ctx="@chef-only")
    pub = _Mem("pub", ctx="@public")
    store.graph = _Graph([{"id": "priv"}, {"id": "pub"}])
    monkeypatch.setattr(store, "recall", lambda i: {"priv": priv, "pub": pub}.get(i))
    out = store.search_fused("alpha beta", limit=5, context_tag="@public")
    assert [m.id for m in out] == ["pub"]


class _Lex:
    def search_semantic(self, query, limit=10, **kw):
        return []

    def search_lexical(self, query, limit=10, layer=None, tags=None, source=None):
        return [_Mem("priv", ctx="@chef-only"), _Mem("pub", ctx="@public")]


def test_lexical_arm_cannot_leak_a_private_memory_to_a_public_reader(store):
    store.vector = _Lex()
    out = store.search_fused("alpha beta", limit=5, context_tag="@public")
    assert [m.id for m in out] == ["pub"]


def test_legacy_path_filters_context_tag_with_overfetch(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "off")
    seen = {}

    class V:
        def search_text(self, query, limit=10, layer=None, tags=None, source=None):
            seen["limit"] = limit
            return [_Mem("priv", ctx="@chef-only"), _Mem("pub", ctx="@public")]

    store.vector = V()
    out = store.search("alpha", limit=2, context_tag="@public")
    assert [m.id for m in out] == ["pub"]
    assert seen["limit"] == 6


# ------------------------------------------- review fixes: real signatures, []


def test_real_skgraph_backend_signature_accepts_the_filter_kwargs():
    import inspect

    from skmemory.backends.skgraph_backend import SKGraphBackend

    params = inspect.signature(SKGraphBackend.search_by_entity).parameters
    for name in ("layer", "tags", "context_tag", "source"):
        assert name in params


def test_fused_graph_arm_works_with_the_real_skgraph_signature(store, monkeypatch):
    """No **filters catch-all: a signature mismatch would TypeError the arm."""
    from skmemory.backends.skgraph_backend import SKGraphBackend

    g = SKGraphBackend.__new__(SKGraphBackend)
    monkeypatch.setattr(
        SKGraphBackend,
        "_search_by_structure",
        lambda self, tmpl, q, limit: [{"id": "stale", "title": "t", "layer": "short-term"}],
    )
    store.graph = g
    monkeypatch.setattr(store, "recall", lambda i: _Mem("stale", layer="short-term"))
    _, report = store.search_fused("alpha beta", limit=5, layer="long-term", return_report=True)
    arm = [a for a in report.arms if a.name == "graph"][0]
    assert arm.status != "failed"


@needs_psycopg
def test_pg_empty_context_tag_list_matches_nothing(monkeypatch):
    be, cur = _pg(monkeypatch)
    be.search_semantic("q", context_tag=[])
    sql, params = cur.executed[0]
    assert "= ANY(%s)" in sql and [] in params


def test_graph_empty_context_tag_list_matches_nothing():
    b = _Rec()
    b.search_by_entity("infra", context_tag=[])
    query, params = b.calls[0]
    assert "m.context_tag IN $context_tags" in query
    assert params["context_tags"] == []


def test_passes_filters_empty_list_rejects_everything():
    from skmemory.store import _passes_filters

    assert _passes_filters(_Mem("a", ctx="@public"), None, None, [], None) is False
    assert _passes_filters(_Mem("a", ctx="@public"), None, None, None, None) is True


def test_fused_lexical_arm_empty_allowed_set_returns_nothing(store):
    store.vector = _Lex()
    assert store.search_fused("alpha beta", limit=5, context_tag=[]) is None


def test_legacy_path_empty_allowed_set_returns_nothing(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "off")

    class V:
        def search_text(self, query, limit=10, layer=None, tags=None, source=None):
            return [_Mem("priv", ctx="@chef-only")]

    store.vector = V()
    assert store.search("alpha", limit=2, context_tag=[]) == []
