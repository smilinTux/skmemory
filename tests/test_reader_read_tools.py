"""Reader ceiling on the remaining read tools (SKMEM-P0-20).

Recall, list, context, export, graph, health, check_duplicate and the owner-only
tools must honour SKMEMORY_READER / a per-call reader. Tmp store and fakes only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skmemory import mcp_server
from skmemory.audience import READER_ENV, AudienceResolver
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


class FakeGraph:
    """pub1 -- priv -- pub2, plus a private hub; edges are undirected."""

    EDGES = {"pub1": ["priv"], "priv": ["pub1", "pub2", "hub"], "pub2": ["priv"], "hub": ["priv"]}

    def _row(self, i, d):
        return {"id": i, "title": i, "layer": "mid-term", "intensity": 1.0, "distance": d}

    def get_related(self, memory_id, depth=2):
        seen, frontier, out = {memory_id}, [memory_id], []
        for d in range(1, depth + 1):
            nxt = []
            for n in frontier:
                for m in self.EDGES.get(n, []):
                    if m not in seen:
                        seen.add(m)
                        out.append(self._row(m, d))
                        nxt.append(m)
            frontier = nxt
        return out

    def get_lineage(self, memory_id):
        return [{"id": "priv", "title": "priv", "layer": "mid-term", "depth": 1}]

    def find_clusters(self, min_size=3):
        return [{"id": "priv", "title": "priv", "layer": "mid-term", "connections": 3}]

    def related_claims_by_entity(self, query, limit=20):
        return [
            {
                "claim": "secret claim",
                "matched_value": query,
                "support_count": 1,
                "memory_ids": ["priv"],
                "memory_titles": ["priv"],
            },
            {
                "claim": "mixed claim",
                "matched_value": query,
                "support_count": 2,
                "memory_ids": ["priv", "pub1"],
                "memory_titles": ["priv", "pub1"],
            },
        ]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    s = MemoryStore()
    for mid, tag in (
        ("pub1", "@public"),
        ("pub2", "@public"),
        ("priv", "@chef-only"),
        ("hub", "@chef-only"),
    ):
        s.primary.save(
            Memory(
                id=mid,
                title=mid,
                content=f"{mid} body",
                layer=MemoryLayer.MID,
                context_tag=tag,
                tags=["seed"] if mid == "pub2" else [],
            )
        )
    s.graph = FakeGraph()
    return s


def call(store, monkeypatch, name, **args):
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    out = asyncio.run(mcp_server.call_tool(name, args))
    return out[0].text


def test_recall_hidden_is_not_found_same_as_missing(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    hidden = call(store, monkeypatch, "memory_recall", memory_id="priv")
    missing = call(store, monkeypatch, "memory_recall", memory_id="nope")
    assert "not found" in hidden.lower()
    assert hidden.replace("priv", "X") == missing.replace("nope", "X")
    assert "pub1" in call(store, monkeypatch, "memory_recall", memory_id="pub1")


def test_owner_recall_unchanged(store, monkeypatch):
    assert "priv body" in call(store, monkeypatch, "memory_recall", memory_id="priv")


def test_per_call_reader_narrows_owner(store, monkeypatch):
    out = call(store, monkeypatch, "memory_recall", memory_id="priv", reader="@public")
    assert "not found" in out.lower()


def test_list_filters_and_fills_page(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    ids = {m["id"] for m in json.loads(call(store, monkeypatch, "memory_list", limit=2))}
    assert ids == {"pub1", "pub2"}


def test_context_has_no_private_titles_or_counts(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    ctx = json.loads(call(store, monkeypatch, "memory_context"))
    blob = json.dumps(ctx)
    assert "priv" not in blob and "hub" not in blob
    assert ctx["stats"]["total_memories"] == 2


def test_export_is_owner_only(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    assert "owner-only" in call(store, monkeypatch, "memory_export")
    with pytest.raises(PermissionError):
        store.export_backup()


@pytest.mark.parametrize(
    "tool",
    ["memory_verify", "memory_audit", "memory_synthesize_daily", "memory_synthesize_dreams"],
)
def test_owner_only_tools_refuse_reader(store, monkeypatch, tool):
    monkeypatch.setenv(READER_ENV, "@public")
    assert "owner-only" in call(store, monkeypatch, tool)


def test_graph_does_not_traverse_through_private_node(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    out = json.loads(call(store, monkeypatch, "memory_graph", action="traverse", memory_id="pub1"))
    assert out == []  # pub2 only reachable through priv
    monkeypatch.delenv(READER_ENV)
    owner_ids = {r["id"] for r in store.graph_related("pub1")}
    assert owner_ids == {"priv", "pub2", "hub"}


def test_graph_hidden_start_is_empty(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    assert (
        json.loads(call(store, monkeypatch, "memory_graph", action="around", memory_id="priv"))
        == []
    )
    assert (
        json.loads(call(store, monkeypatch, "memory_graph", action="lineage", memory_id="pub1"))
        == []
    )
    assert json.loads(call(store, monkeypatch, "memory_graph", action="clusters")) == []


def test_graph_related_claims_hide_private_support(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    out = json.loads(
        call(
            store,
            monkeypatch,
            "memory_graph",
            action="related_claims",
            pivot_type="entity",
            query="x",
        )
    )
    assert [r["claim"] for r in out] == ["mixed claim"]
    assert out[0]["memory_ids"] == ["pub1"] and out[0]["support_count"] == 1


def test_health_is_minimal_for_reader(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    h = json.loads(call(store, monkeypatch, "memory_health"))
    assert set(h) == {"status", "embed_ok", "vector_ok", "graph_ok"}


def test_promote_and_forget_hidden_are_not_found(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    assert "not found" in call(store, monkeypatch, "memory_forget", memory_id="priv").lower()
    assert (
        "not found"
        in call(store, monkeypatch, "memory_promote", memory_id="priv", target="long-term").lower()
    )
    assert store.primary.load("priv") is not None


def test_check_duplicate_drops_hidden(store, monkeypatch):
    class Vec:
        def find_similar(self, content, k=5):
            return [
                {"id": "priv", "content_preview": "secret", "similarity": 0.9},
                {"id": "pub1", "content_preview": "ok", "similarity": 0.9},
            ]

    store.vector = Vec()
    monkeypatch.setenv(READER_ENV, "@public")
    out = json.loads(call(store, monkeypatch, "memory_check_duplicate", content="x"))
    assert [m["id"] for m in out["duplicate_candidates"]] == ["pub1"]


def test_ritual_unknown_channel_is_public(tmp_path):
    cfg = tmp_path / "a.json"
    cfg.write_text('{"channels": {"c": {"name": "c", "members": []}}, "people": {}}')
    r = AudienceResolver(config_path=cfg)
    for cid in ("nope", "c"):
        prof = r.resolve_audience(cid)
        assert prof.min_trust.name == "PUBLIC" and prof.context_tag == "@public"
        assert r.is_memory_allowed("@public", prof)
        assert not r.is_memory_allowed("@work-circle", prof)
        assert not r.is_memory_allowed("@chef-only", prof)


def test_channelless_ritual_refused_under_reader_env(store, monkeypatch):
    from skmemory.ritual import perform_ritual

    monkeypatch.setenv(READER_ENV, "@public")
    with pytest.raises(PermissionError):
        perform_ritual(store=store)


def _registered():
    return [t.name for t in asyncio.run(mcp_server.list_tools())]


def test_every_tool_has_exactly_one_policy():
    names = set(_registered())
    assert names == set(mcp_server.TOOL_READER_POLICY), names ^ set(mcp_server.TOOL_READER_POLICY)
    for tool, (cls, why) in mcp_server.TOOL_READER_POLICY.items():
        assert cls in {"filtered", "owner-only", "exempt"}, tool
        if cls == "exempt":
            assert why, f"{tool} exempt without a reason"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("memory_import", {"backup_path": "/nonexistent.json"}),
        ("memory_consolidate", {"session_id": "s", "summary": "x"}),
    ],
)
def test_import_and_consolidate_owner_only(store, monkeypatch, tool, args):
    monkeypatch.setenv(READER_ENV, "@public")
    assert "owner-only" in call(store, monkeypatch, tool, **args)


def test_owner_only_policy_tools_all_refuse(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    for tool, (cls, _) in mcp_server.TOOL_READER_POLICY.items():
        if cls == "owner-only":
            out = call(store, monkeypatch, tool, backup_path="x", session_id="s", summary="x")
            assert "owner-only" in out, tool


def test_consolidate_does_not_copy_private_ids(store, monkeypatch):
    store.primary.save(
        Memory(
            id="privS",
            title="p",
            content="c",
            layer=MemoryLayer.SHORT,
            tags=["session:S", "secret-tag"],
            context_tag="@chef-only",
        )
    )
    monkeypatch.setenv(READER_ENV, "@public")
    call(store, monkeypatch, "memory_consolidate", session_id="S", summary="x")
    assert not [m for m in store.primary.list_memories(limit=50) if "consolidated" in m.tags]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("memory_store", {"title": "t", "content": "priv body"}),
        ("memory_save_session", {"conversation": "We decided to use priv body. " * 10}),
        ("memory_pre_compact", {"context": "priv body"}),
    ],
)
def test_writes_do_not_dedup_against_hidden(store, monkeypatch, tool, args):
    monkeypatch.setenv(READER_ENV, "@public")
    before = store.primary.load("priv").model_dump()
    out = json.loads(call(store, monkeypatch, tool, **args))
    assert '"priv"' not in json.dumps(out)
    assert store.primary.load("priv").model_dump() == before


def test_memory_store_hash_match_hidden_writes_fresh(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    out = json.loads(call(store, monkeypatch, "memory_store", title="t", content="priv body"))
    assert out["memory_id"] != "priv" and out["stored"] is True
    assert store.primary.load(out["memory_id"]) is not None


def test_owner_dedup_unchanged(store, monkeypatch):
    out = json.loads(call(store, monkeypatch, "memory_store", title="t", content="priv body"))
    assert out["memory_id"] == "priv"
