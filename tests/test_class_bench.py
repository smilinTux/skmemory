"""SKMEM-P1-02: per-class baseline runner and per-query gate."""

import copy

import pytest

from skmemory.eval import class_bench, regression_gate


@pytest.fixture(scope="module")
def res():
    return class_bench.run()


def test_all_strategies_reported_per_class(res):
    for entry in res["by_class"].values():
        assert set(entry["strategies"]) == set(class_bench.ALL_STRATEGIES)
    assert {
        "lexical",
        "semantic",
        "entity",
        "temporal",
        "conflict",
        "multihop",
        "absent",
        "affect",
        "procedural",
        "privacy",
    } <= set(res["by_class"])


def test_deterministic(res):
    assert class_bench.flat_metrics(class_bench.run()) == class_bench.flat_metrics(res)


def test_full_context_oracle_has_cost_and_full_recall(res):
    assert res["corpus_tokens"] > 0
    for entry in res["by_class"].values():
        r = entry["strategies"][class_bench.ORACLE]["recall@5"]
        assert r in (None, 1.0)


def test_privacy_leak_zero_and_trap_live(res):
    assert res["leak_count"] == 0
    assert res["leak_count_unfiltered"] > 0


def test_absent_scored_by_abstain(res):
    cand = res["by_class"]["absent"]["strategies"][class_bench.CANDIDATE]
    assert cand["abstain_correct"] is not None
    assert cand["recall@5"] is None


def test_as_of_queries_marked_expected_fail_not_skipped(res):
    asof = [r for r in res["rows"] if r["as_of"] and r["class"] in ("temporal", "conflict")]
    assert asof and all(r["expected_fail"] for r in asof)
    for f in res["failures"]:
        if f["id"] in {r["id"] for r in asof}:
            assert f["expected_fail"]


def test_abstain_threshold_env(monkeypatch):
    monkeypatch.setenv("SKMEMORY_ABSTAIN", "0.99")
    assert class_bench.abstain_threshold() == 0.99
    monkeypatch.setenv("SKMEMORY_ABSTAIN", "junk")
    assert class_bench.abstain_threshold() == class_bench.DEFAULT_ABSTAIN


def test_gate_passes_against_committed_baseline():
    cur = regression_gate.collect_metrics()
    assert regression_gate.compare(cur, regression_gate.load_baseline()) == []


def test_gate_fails_on_per_query_regression_hidden_by_aggregate():
    cur = regression_gate.collect_metrics()
    base = regression_gate.load_baseline()
    worse = copy.deepcopy(cur)
    key = "class_bench.candidate.q.q-lexical-01.recall@5"
    worse["metrics"][key] = 0.0
    assert any(key in r for r in regression_gate.compare(worse, base))


def test_gate_fails_on_unrecorded_below_gate_query():
    cur = regression_gate.collect_metrics()
    base = copy.deepcopy(regression_gate.load_baseline())
    base["class_gate_failures"] = []
    msgs = regression_gate.compare(cur, base)
    assert any("not a recorded known failure" in m for m in msgs)
