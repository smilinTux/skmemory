"""memory_auto_context ranks by fused relevance, intensity is only a tiebreak (SKMEM-P0-06)."""

from __future__ import annotations

import asyncio
import json

import skmemory.mcp_server as mcp_server
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer


def _mem(mid: str, intensity: float) -> Memory:
    return Memory(
        id=mid,
        title=f"t-{mid}",
        content=f"content {mid}",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=intensity),
    )


class FakeStore:
    def __init__(self, by_query: dict[str, list[Memory]]):
        self.by_query = by_query
        self.calls: list[dict] = []

    def search(self, query, limit=10, **kw):
        self.calls.append({"query": query, **kw})
        return list(self.by_query.get(query, []))


def _run(store, args):
    mcp_server._store = store
    try:
        out = asyncio.run(mcp_server.call_tool("memory_auto_context", args))
    finally:
        mcp_server._store = None
    return json.loads(out[0].text)


def test_relevance_beats_intensity():
    # "hi" is the best hit for the query but has low intensity; "lo" is a weak
    # hit with maximal intensity. Old behaviour put "lo" first.
    store = FakeStore({"alpha": [_mem("hi", 0.1), _mem("lo", 1.0)]})
    res = _run(store, {"keywords": "alpha"})
    assert [r["id"] for r in res["results"]] == ["hi", "lo"]


def test_multi_keyword_agreement_outranks_single_top_hit():
    a = [_mem("both", 0.2), _mem("only_a", 0.9)]
    b = [_mem("only_b", 0.9), _mem("both", 0.2)]
    store = FakeStore({"a": a, "b": b})
    res = _run(store, {"keywords": "a b"})
    assert res["results"][0]["id"] == "both"


def test_intensity_breaks_ties():
    store = FakeStore({"a": [_mem("x", 0.2)], "b": [_mem("y", 0.8)]})
    res = _run(store, {"keywords": "a b"})
    assert [r["id"] for r in res["results"]] == ["y", "x"]


def test_reader_forwarded_and_no_access_recording():
    store = FakeStore({"a": [_mem("x", 0.5)]})
    _run(store, {"keywords": "a", "reader": "@public"})
    assert store.calls[0]["reader"] == "@public"
    assert not store.calls[0].get("record_access", False)


def test_description_states_actual_ranking():
    tool = next(t for t in asyncio.run(mcp_server.list_tools()) if t.name == "memory_auto_context")
    assert "emotional intensity" in tool.description
    assert "tiebreak" in tool.description or "tie-break" in tool.description
