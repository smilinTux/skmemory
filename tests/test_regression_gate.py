"""Tests for the offline eval regression gate (skmemory.eval.regression_gate).

Proves the CI-facing contract the card requires, fully offline / deterministic:
  1. collect_metrics() returns the expected flat metric keys, is deterministic,
     and reports leak_count == 0 with a non-vacuous leak trap.
  2. compare() PASSES against the current run used as its own baseline (GREEN).
  3. compare() FAILS (RED) when any gated metric drops below baseline.
  4. compare() FAILS (RED) when leak_count > 0 (hard privacy constraint).
  5. compare() FAILS when a baseline metric is missing from the current run.
  6. the committed eval/baseline.json is a valid floor: the live run passes it.
  7. main() exits 0 on a matching baseline and 1 on a regressed baseline.
"""

from __future__ import annotations

import copy
import json

from skmemory.eval import regression_gate as gate

# ── metric collection ─────────────────────────────────────────────────────


def test_collect_metrics_shape_and_keys():
    cur = gate.collect_metrics()
    assert set(cur) == {"metrics", "leak_count", "leak_count_unfiltered"}
    m = cur["metrics"]
    # Every harness contributes its expected keys.
    for key in (
        "recall_bench.recall@1",
        "recall_bench.ndcg@5",
        "retrieval_bench.precision@1",
        "retrieval_bench.mrr",
        "groundedness.score",
    ):
        assert key in m, key
        assert isinstance(m[key], float)


def test_collect_metrics_deterministic():
    assert gate.collect_metrics() == gate.collect_metrics()


def test_leak_gate_is_non_vacuous():
    # 0 leaks after the audience filter, but the raw hybrid WOULD have leaked:
    # proves leak_count == 0 is meaningful, not because the trap is empty.
    cur = gate.collect_metrics()
    assert cur["leak_count"] == 0
    assert cur["leak_count_unfiltered"] > 0


# ── compare(): GREEN ──────────────────────────────────────────────────────


def test_compare_green_against_self():
    cur = gate.collect_metrics()
    baseline = {
        "metrics": cur["metrics"],
        "leak_count": cur["leak_count"],
        "leak_count_unfiltered": cur["leak_count_unfiltered"],
    }
    assert gate.compare(cur, baseline) == []


# ── compare(): RED on a worsened metric ───────────────────────────────────


def test_compare_red_on_lowered_metric():
    cur = gate.collect_metrics()
    baseline = copy.deepcopy({"metrics": cur["metrics"], "leak_count": 0})
    regressed = copy.deepcopy(cur)
    regressed["metrics"]["retrieval_bench.mrr"] -= 0.25  # simulate rank regression
    reg = gate.compare(regressed, baseline)
    assert reg, "a dropped metric must produce a regression"
    assert any("retrieval_bench.mrr" in line for line in reg)


def test_compare_red_on_groundedness_drop():
    cur = gate.collect_metrics()
    baseline = {"metrics": cur["metrics"], "leak_count": 0}
    regressed = copy.deepcopy(cur)
    regressed["metrics"]["groundedness.score"] = 0.0
    reg = gate.compare(regressed, baseline)
    assert any("groundedness.score" in line for line in reg)


def test_tiny_drop_below_epsilon_is_regression():
    cur = gate.collect_metrics()
    baseline = {"metrics": cur["metrics"], "leak_count": 0}
    regressed = copy.deepcopy(cur)
    regressed["metrics"]["recall_bench.recall@1"] -= 1e-6  # above epsilon noise floor
    assert gate.compare(regressed, baseline, epsilon=gate.DEFAULT_EPSILON)


def test_float_noise_within_epsilon_is_not_regression():
    cur = gate.collect_metrics()
    baseline = {"metrics": cur["metrics"], "leak_count": 0}
    jittered = copy.deepcopy(cur)
    jittered["metrics"]["recall_bench.recall@1"] -= 1e-12  # below epsilon
    assert gate.compare(jittered, baseline, epsilon=gate.DEFAULT_EPSILON) == []


# ── compare(): RED on a leak ──────────────────────────────────────────────


def test_compare_red_on_leak():
    cur = gate.collect_metrics()
    baseline = {"metrics": cur["metrics"], "leak_count": 0}
    leaked = copy.deepcopy(cur)
    leaked["leak_count"] = 1
    reg = gate.compare(leaked, baseline)
    assert any("leak_count" in line for line in reg)


# ── compare(): missing metric is a regression ─────────────────────────────


def test_compare_red_on_missing_metric():
    cur = gate.collect_metrics()
    baseline = {"metrics": dict(cur["metrics"]), "leak_count": 0}
    baseline["metrics"]["a_new_metric_that_vanished"] = 0.9
    reg = gate.compare(cur, baseline)
    assert any("MISSING" in line for line in reg)


# ── committed baseline is a valid floor ───────────────────────────────────


def test_committed_baseline_passes_live_run():
    baseline = gate.load_baseline()
    cur = gate.collect_metrics()
    assert gate.compare(cur, baseline) == [], "current HEAD must pass its own committed baseline"
    assert baseline["leak_count"] == 0


def test_baseline_file_is_valid_json_with_metrics():
    with open(gate.BASELINE_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert "metrics" in doc and doc["metrics"]
    assert doc["leak_count"] == 0


# ── main() exit codes ─────────────────────────────────────────────────────


def test_main_exits_zero_on_committed_baseline():
    assert gate.main([]) == 0


def test_main_exits_one_on_regressed_baseline(tmp_path):
    # Write a baseline with an impossibly-high floor so the live run regresses.
    bad = {
        "metrics": {"retrieval_bench.mrr": 2.0, "recall_bench.recall@1": 2.0},
        "leak_count": 0,
    }
    p = tmp_path / "bad_baseline.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    assert gate.main(["--baseline", str(p)]) == 1


def test_write_baseline_roundtrip(tmp_path):
    p = tmp_path / "baseline.json"
    assert gate.main(["--write-baseline", "--baseline", str(p)]) == 0
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert "metrics" in doc and doc["leak_count"] == 0
    # A freshly-written baseline must immediately pass the gate.
    assert gate.main(["--baseline", str(p)]) == 0
