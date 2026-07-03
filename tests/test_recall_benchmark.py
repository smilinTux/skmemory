"""Tests for the ported recall/NDCG scoring math + the fixture-corpus
benchmark harness (skmemory.eval.recall_benchmark).

The scoring function tests are pure math against hand-computed expected
values — no I/O, no network. The run_benchmark() test drives skmemory's
REAL MemoryStore/SQLiteBackend API but does so entirely locally (a temp-dir
SQLiteBackend with no vector backend attached), so it also requires no
network or live embedding server.
"""

from __future__ import annotations

import math

from skmemory.eval.recall_benchmark import (
    FIXTURE_MEMORIES,
    FIXTURE_QUERIES,
    dcg,
    ndcg_at_k,
    recall_at_k,
    run_benchmark,
)


class TestDCG:
    def test_hand_computed(self):
        # dcg([1, 0, 1], 3) = 1/log2(2) + 0/log2(3) + 1/log2(4) = 1 + 0 + 0.5
        result = dcg([1.0, 0.0, 1.0], 3)
        assert math.isclose(result, 1.5, rel_tol=1e-9)

    def test_truncates_to_k(self):
        # Only the first k relevances count.
        full = dcg([1.0, 1.0, 1.0, 1.0], 4)
        truncated = dcg([1.0, 1.0, 1.0, 1.0], 2)
        assert truncated < full
        assert math.isclose(truncated, 1.0 + 1.0 / math.log2(3), rel_tol=1e-9)

    def test_empty_relevances(self):
        assert dcg([], 5) == 0.0

    def test_all_zero_relevances(self):
        assert dcg([0.0, 0.0, 0.0], 3) == 0.0


class TestRecallAtK:
    def test_spec_example(self):
        # recall@2 of [a, b, c] against relevant {b, x} = 0.5
        assert recall_at_k(["a", "b", "c"], {"b", "x"}, 2) == 0.5

    def test_full_recall(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_zero_recall(self):
        assert recall_at_k(["a", "b", "c"], {"z"}, 3) == 0.0

    def test_k_smaller_than_relevant_set(self):
        # Only 1 of 2 relevant ids fits within top-1.
        assert recall_at_k(["b", "a", "x"], {"a", "b"}, 1) == 0.5

    def test_no_relevant_ids_returns_zero(self):
        assert recall_at_k(["a", "b"], set(), 5) == 0.0

    def test_accepts_list_for_relevant_ids(self):
        assert recall_at_k(["a", "b", "c"], ["b", "x"], 2) == 0.5


class TestNdcgAtK:
    def test_hand_computed_ranking(self):
        # retrieved = [a, b, c, d], relevant = {a, c}
        # relevances@4 = [1, 0, 1, 0]
        # dcg = 1/log2(2) + 1/log2(4) = 1 + 0.5 = 1.5
        # ideal = [1, 1, 0, 0] -> idcg = 1/log2(2) + 1/log2(3) = 1 + 0.6309...
        result = ndcg_at_k(["a", "b", "c", "d"], {"a", "c"}, 4)
        idcg = 1.0 + 1.0 / math.log2(3)
        expected = 1.5 / idcg
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_perfect_ranking_is_one(self):
        # All relevant ids occupy the top ranks -> NDCG == 1.0
        result = ndcg_at_k(["a", "b", "c"], {"a", "b"}, 3)
        assert math.isclose(result, 1.0, rel_tol=1e-9)

    def test_no_hits_is_zero(self):
        assert ndcg_at_k(["a", "b", "c"], {"z"}, 3) == 0.0

    def test_no_relevant_ids_is_zero(self):
        assert ndcg_at_k(["a", "b", "c"], set(), 3) == 0.0

    def test_worse_ranking_scores_lower_than_better_ranking(self):
        # Same single hit, worse position -> lower NDCG.
        best = ndcg_at_k(["a", "b", "c"], {"a"}, 3)
        worst = ndcg_at_k(["b", "c", "a"], {"a"}, 3)
        assert best == 1.0
        assert worst < best


class TestFixtureCorpus:
    def test_every_query_references_a_known_label(self):
        labels = {item["label"] for item in FIXTURE_MEMORIES}
        for _query, relevant_labels in FIXTURE_QUERIES:
            for label in relevant_labels:
                assert label in labels, f"query references unknown fixture label {label!r}"

    def test_labels_are_unique(self):
        labels = [item["label"] for item in FIXTURE_MEMORIES]
        assert len(labels) == len(set(labels))


class TestRunBenchmarkOffline:
    """run_benchmark() with no store argument builds a throwaway local
    SQLiteBackend-only MemoryStore (no vector backend, no network) — so this
    exercises the real store.snapshot()/search() API without live services.
    """

    def test_returns_expected_shape(self):
        result = run_benchmark(k_values=(1, 3, 5))

        assert result["k_values"] == [1, 3, 5]
        assert result["num_queries"] == len(FIXTURE_QUERIES)
        assert result["num_fixture_memories"] == len(FIXTURE_MEMORIES)
        assert len(result["per_query"]) == len(FIXTURE_QUERIES)

        for row in result["per_query"]:
            for k in (1, 3, 5):
                assert 0.0 <= row[f"recall@{k}"] <= 1.0
                assert 0.0 <= row[f"ndcg@{k}"] <= 1.0

        for k in (1, 3, 5):
            assert 0.0 <= result["aggregate"][f"recall@{k}"] <= 1.0
            assert 0.0 <= result["aggregate"][f"ndcg@{k}"] <= 1.0

    def test_keyword_search_finds_known_items(self):
        # Every fixture query is built from vocabulary unique to exactly one
        # fixture memory, so the offline keyword (LIKE AND/OR) search should
        # reliably surface it near the top. This is the regression signal:
        # if sanitizer/extractor changes break retrieval, this recall drops.
        result = run_benchmark(k_values=(1, 3, 5))
        assert result["aggregate"]["recall@5"] >= 0.75
        assert result["aggregate"]["recall@1"] >= 0.5
