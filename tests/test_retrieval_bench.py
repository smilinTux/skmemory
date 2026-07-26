"""Regression tests for the hybrid-retrieval benchmark harness.

Covers three things the card requires:
  1. precision@k / recall@k / MRR compute correctly and DISCRIMINATE a good
     ranking from a bad one (a broken ranker must score lower).
  2. the LEAK COUNT catches a private item that leaks (>0 unfiltered) and is 0
     once the @public audience filter is applied.
  3. run_benchmark()'s aggregate metrics stay above a floor AND leak_count == 0.
"""

from __future__ import annotations

from skmemory.audience import AudienceLevel, AudienceProfile, AudienceResolver
from skmemory.eval.retrieval_bench import (
    CORPUS,
    PRIVATE_MEMORIES,
    _public_audience,
    count_leaks,
    hybrid_search,
    mean_reciprocal_rank,
    precision_at_k,
    reciprocal_rank,
    run_benchmark,
)
from skmemory.eval.recall_benchmark import recall_at_k


# ── scoring math ──────────────────────────────────────────────────────────


def test_precision_at_k_known_values():
    # top-2 = {a, b}; 1 of 2 relevant → 0.5
    assert precision_at_k(["a", "b", "c"], {"a", "x"}, 2) == 0.5
    # perfect top-2
    assert precision_at_k(["a", "b", "c"], {"a", "b"}, 2) == 1.0
    # nothing relevant
    assert precision_at_k(["a", "b", "c"], {"z"}, 3) == 0.0
    # k=0 guarded
    assert precision_at_k(["a"], {"a"}, 0) == 0.0


def test_reciprocal_rank_known_values():
    assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0
    assert reciprocal_rank(["x", "a", "b"], {"a"}) == 0.5
    assert reciprocal_rank(["x", "y", "a"], {"a"}) == 1.0 / 3
    assert reciprocal_rank(["x", "y", "z"], {"a"}) == 0.0


def test_mean_reciprocal_rank():
    assert mean_reciprocal_rank([1.0, 0.5, 0.0]) == 0.5
    assert mean_reciprocal_rank([]) == 0.0


def test_metrics_discriminate_good_from_bad_ranking():
    """A ranking with the relevant id first must beat one with it last."""
    relevant = {"good"}
    good = ["good", "x", "y", "z"]
    bad = ["x", "y", "z", "good"]

    assert precision_at_k(good, relevant, 1) > precision_at_k(bad, relevant, 1)
    assert reciprocal_rank(good, relevant) > reciprocal_rank(bad, relevant)
    assert recall_at_k(good, relevant, 1) > recall_at_k(bad, relevant, 1)


# ── hybrid search ranking ─────────────────────────────────────────────────


def test_hybrid_search_ranks_relevant_first():
    """Distinctive-vocab query should put its target item at rank 1."""
    results = hybrid_search(CORPUS, "mxbai embedding server port location", k=5)
    assert results, "hybrid_search returned nothing"
    assert results[0]["id"] == "mxbai_server"


def test_hybrid_search_is_deterministic():
    q = "skmem-pg Postgres pgvector BM25 graph"
    a = [r["id"] for r in hybrid_search(CORPUS, q, k=5)]
    b = [r["id"] for r in hybrid_search(CORPUS, q, k=5)]
    assert a == b


# ── LEAK COUNT ────────────────────────────────────────────────────────────


def test_private_item_leaks_without_filter():
    """The trap query must actually surface a private @chef-only item when NO
    audience filter is applied - otherwise leak_count==0 would be vacuous."""
    public = _public_audience()
    unfiltered = hybrid_search(
        CORPUS, "cloud nine emotional continuity depth trust love", k=5
    )
    assert count_leaks(unfiltered, public) > 0


def test_filter_blocks_the_leak():
    """With the @public audience filter, the same query leaks nothing."""
    public = _public_audience()
    resolver = AudienceResolver()
    filtered = hybrid_search(
        CORPUS,
        "cloud nine emotional continuity depth trust love",
        k=5,
        audience=public,
        resolver=resolver,
    )
    assert count_leaks(filtered, public) == 0
    # and every returned item is genuinely public-safe
    private_ids = {m["id"] for m in PRIVATE_MEMORIES}
    assert all(r["id"] not in private_ids for r in filtered)


def test_count_leaks_uses_real_audience_gate():
    """count_leaks must agree with skmemory.audience for a mixed result set."""
    public = _public_audience()
    mixed = [
        {"id": "pub", "context_tag": "@public", "tags": []},
        {"id": "priv", "context_tag": "@chef-only", "tags": ["@chef-only"]},
    ]
    assert count_leaks(mixed, public) == 1
    # a privileged (@chef-only) reader sees no leaks
    chef = AudienceProfile(channel_id="c", min_trust=AudienceLevel.CHEF_ONLY)
    assert count_leaks(mixed, chef) == 0


# ── end-to-end benchmark regression (floors + zero-leak) ──────────────────


def test_run_benchmark_metrics_above_floor_and_no_leak():
    result = run_benchmark()
    agg = result["aggregate"]

    # Relevance floors - the known-item corpus should be easily retrieved.
    assert agg["precision@1"] >= 0.9
    assert agg["recall@5"] >= 0.9
    assert agg["mrr"] >= 0.9

    # Privacy is the hard gate: ZERO private items may survive the filter,
    # and the trap must have been non-trivial (unfiltered leaks > 0).
    assert result["leak_count"] == 0
    assert result["leak_count_unfiltered"] > 0
