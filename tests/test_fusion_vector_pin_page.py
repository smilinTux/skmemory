"""SKMEM-P1-10: why the fused page differs from the vector_only arm's top 5.

Live paraphrase seed 20261106: with SKMEMORY_FUSION_VECTOR_PIN=5 on the natural
route the fused top 5 should equal the semantic arm's top 5, yet candidate
recall@5 (0.181) sat below vector_only (0.198). The pin is applied inside
``fuse()``; ``MemoryStore.search_fused`` and ``MemoryStore.search`` then run
four more stages over the fused pool, and each can move a vector hit out of the
top 5:

  1. ``RareTermReranker`` (on by default): reorders the WHOLE fused pool by
     rank/5 for any identifier-shaped query term (a digit or a 12+ letter word),
     ignoring the pin.
  2. ``_demote_superseded``: a closed vector hit drops below its open head.
  3. ``apply_tier_cap``: web/self-generated hits are capped at 4 of 10.
  4. ``_apply_decay`` (only when SKMEMORY_DECAY is set; off by default).

Everything is hermetic: a tmp SQLite primary and a fake vector backend.
Tests marked EXPECTED-TO-CHANGE document today's behaviour; a fix that makes the
pinned vector head authoritative should flip their assertions.
"""

from __future__ import annotations

import pytest

from skmemory.store import MemoryStore

QUERY = "why does the sync keep falling behind"
PIN = 5


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    monkeypatch.setenv("SKMEMORY_FUSION_ROUTE", "vector")  # force the natural route
    monkeypatch.setenv("SKMEMORY_FUSION_VECTOR_PIN", str(PIN))
    monkeypatch.setenv("SKMEMORY_FUSION_STRATEGY", "pinned_rrf")
    monkeypatch.delenv("SKMEMORY_SEARCH_RERANK", raising=False)
    monkeypatch.delenv("SKMEMORY_TIER_CAP", raising=False)
    monkeypatch.delenv("SKMEMORY_DECAY", raising=False)


class FakeVector:
    """Vector backend returning a fixed ranked list of Memory objects."""

    def __init__(self, mems):
        self.mems = list(mems)
        self.calls = []

    def search_semantic(self, query, limit=10, **filters):
        self.calls.append((query, limit))
        return self.mems[:limit]


def _build(tmp_path, *, extra_text=""):
    store = MemoryStore()
    vec = []
    for n in range(1, 15):
        vec.append(
            store.snapshot(
                title=f"vec {n}", content=f"note {n} about replication lag and queues", tags=["t"]
            )
        )
    lex = store.snapshot(
        title="lex only",
        content=f"the sync keep falling behind {extra_text}".strip(),
        tags=["t"],
    )
    store.vector = FakeVector(vec)
    return store, vec, lex


def _ids(mems):
    return [m.id for m in mems]


def test_control_pin_makes_page_head_equal_vector_head(tmp_path):
    """With nothing downstream acting, the pin does hold: this is the baseline."""
    store, vec, lex = _build(tmp_path)
    page = store.search(QUERY, limit=10)
    assert _ids(page)[:5] == _ids(vec)[:5]
    # search() fetches a 3x pool (30) and search_fused asks each arm for 3x that (90); vector_only asks 10.
    assert store.vector.calls[0][1] == 90


def test_reranker_moves_vector_hit_out_of_top5(tmp_path):
    """EXPECTED-TO-CHANGE: an identifier-shaped term (12+ letters) boosts a
    lexical-only hit past pinned vector ranks 2 to 5, so v5 leaves the page head."""
    store, vec, lex = _build(tmp_path, extra_text="reconfiguration")
    q = QUERY + " after the reconfiguration"
    page = _ids(store.search(q, limit=10))
    assert page[:5] != _ids(vec)[:5]
    assert vec[4].id not in page[:5]
    assert lex.id in page[:5]


def test_reranker_off_restores_the_pin(tmp_path, monkeypatch):
    """Same query as above: turning the reranker off keeps the vector head whole."""
    monkeypatch.setenv("SKMEMORY_SEARCH_RERANK", "off")
    store, vec, lex = _build(tmp_path, extra_text="reconfiguration")
    page = _ids(store.search(QUERY + " after the reconfiguration", limit=10))
    assert page[:5] == _ids(vec)[:5]


def test_supersession_demotion_moves_closed_vector_hit(tmp_path):
    """EXPECTED-TO-CHANGE: a closed vector hit is ranked directly below its open
    head, and the head here is the lexical slot at rank 6, so v2 leaves the top 5."""
    store, vec, lex = _build(tmp_path)
    closed = vec[1].model_copy(
        update={
            "valid_to": "2020-01-01T00:00:00+00:00",
            "metadata": {"superseded_by": {"id": lex.id}},
        }
    )
    store.vector.mems[1] = closed
    page = _ids(store.search(QUERY, limit=10))
    assert closed.id not in page[:5]
    assert page.index(closed.id) == page.index(lex.id) + 1


def test_tier_cap_pushes_fifth_capped_vector_hit_down(tmp_path):
    """EXPECTED-TO-CHANGE: web/self-generated hits are capped at 4 of 10, so a
    page whose vector top 5 are all self-generated loses the fifth to an
    uncapped hit even though the pin ranked it fifth."""
    store, vec, lex = _build(tmp_path)
    for i in range(5):
        store.vector.mems[i] = vec[i].model_copy(
            update={"source_tier": "self-generated", "schema_version": 2}
        )
    page = _ids(store.search(QUERY, limit=10))
    assert vec[4].id not in page[:5]


def test_tier_cap_off_keeps_the_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_TIER_CAP", "off")
    store, vec, lex = _build(tmp_path)
    for i in range(5):
        store.vector.mems[i] = vec[i].model_copy(
            update={"source_tier": "self-generated", "schema_version": 2}
        )
    assert _ids(store.search(QUERY, limit=10))[:5] == _ids(vec)[:5]


def test_sanitizer_changes_query_only_for_long_input(tmp_path):
    """The semantic arm sees sanitize_query(query); live_bench's vector_only sees
    the raw string. Identical for queries <= 200 chars, different above."""
    store, vec, lex = _build(tmp_path)
    store.search(QUERY, limit=10)
    assert store.vector.calls[-1][0] == QUERY
    long_q = ("Some preamble sentence. " * 12) + "Why does the sync keep falling behind?"
    store.search(long_q, limit=10)
    assert store.vector.calls[-1][0] == "Why does the sync keep falling behind?"
