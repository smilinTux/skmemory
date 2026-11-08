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
    assert set(cur) == {
        "metrics",
        "leak_count",
        "leak_count_unfiltered",
        "class_gate_failures",
        "sacred_leaks_by_path",
        "sacred_vacuous",
    }
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
        "class_gate_failures": cur["class_gate_failures"],
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
    baseline = {
        "metrics": cur["metrics"],
        "leak_count": 0,
        "class_gate_failures": cur["class_gate_failures"],
    }
    regressed = copy.deepcopy(cur)
    regressed["metrics"]["groundedness.score"] = 0.0
    reg = gate.compare(regressed, baseline)
    assert any("groundedness.score" in line for line in reg)


def test_tiny_drop_below_epsilon_is_regression():
    cur = gate.collect_metrics()
    baseline = {
        "metrics": cur["metrics"],
        "leak_count": 0,
        "class_gate_failures": cur["class_gate_failures"],
    }
    regressed = copy.deepcopy(cur)
    regressed["metrics"]["recall_bench.recall@1"] -= 1e-6  # above epsilon noise floor
    assert gate.compare(regressed, baseline, epsilon=gate.DEFAULT_EPSILON)


def test_float_noise_within_epsilon_is_not_regression():
    cur = gate.collect_metrics()
    baseline = {
        "metrics": cur["metrics"],
        "leak_count": 0,
        "class_gate_failures": cur["class_gate_failures"],
    }
    jittered = copy.deepcopy(cur)
    jittered["metrics"]["recall_bench.recall@1"] -= 1e-12  # below epsilon
    assert gate.compare(jittered, baseline, epsilon=gate.DEFAULT_EPSILON) == []


# ── compare(): RED on a leak ──────────────────────────────────────────────


def test_compare_red_on_leak():
    cur = gate.collect_metrics()
    baseline = {
        "metrics": cur["metrics"],
        "leak_count": 0,
        "class_gate_failures": cur["class_gate_failures"],
    }
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


# --- markdown report (SKMEM-P1-06B) -------------------------------------------


def _report_fixture():
    base = {
        "metrics": {
            "class_bench.candidate.semantic.recall@5": 0.8,
            "class_bench.candidate.entity.recall@5": 1.0,
            "class_bench.candidate.q.q-entity-01.recall@5": 1.0,
            "fusion_bench.interleave_rerank.semantic.recall@5": 0.9,
            "groundedness.score": 0.75,
        }
    }
    cur = {
        "metrics": {
            "class_bench.candidate.semantic.recall@5": 0.6,
            "class_bench.candidate.entity.recall@5": 1.0,
            "class_bench.candidate.q.q-entity-01.recall@5": 1.0,
            "fusion_bench.interleave_rerank.semantic.recall@5": 0.9,
            "groundedness.score": 0.75,
        },
        "leak_count": 0,
    }
    return cur, base


def test_report_marker_first_and_table_content():
    from skmemory.eval import gate_report

    cur, base = _report_fixture()
    md = gate_report.render_markdown(cur, base, gate.compare(cur, base))
    assert md.startswith(gate_report.MARKER)
    assert md.count(gate_report.MARKER) == 1
    assert "gate: FAIL" in md
    assert "| semantic | class_bench.recall@5 | 0.800 | 0.600 | -0.200 | REGRESSED |" in md
    assert "| entity | class_bench.recall@5 | 1.000 | 1.000 | 0 | ok |" in md
    assert "| groundedness.score | 0.750 | 0.750 | 0 | ok |" in md
    assert "Per-query floors checked: 1" in md
    assert "#### Regressions" in md


def test_report_pass_has_no_regression_section_and_is_stable():
    from skmemory.eval import gate_report

    cur, base = _report_fixture()
    cur["metrics"]["class_bench.candidate.semantic.recall@5"] = 0.8
    md = gate_report.render_markdown(cur, base, [])
    assert "gate: PASS" in md
    assert "#### Regressions" not in md
    rev = {"metrics": dict(reversed(list(cur["metrics"].items()))), "leak_count": 0}
    rbase = {"metrics": dict(reversed(list(base["metrics"].items())))}
    assert gate_report.render_markdown(rev, rbase, []) == md
    rows = [ln.split("|")[1].strip() for ln in md.splitlines() if ln.startswith("| ")]
    assert rows.index("entity") < rows.index("semantic")


def test_report_missing_metric_flagged():
    from skmemory.eval import gate_report

    cur, base = _report_fixture()
    del cur["metrics"]["class_bench.candidate.entity.recall@5"]
    md = gate_report.render_markdown(cur, base, gate.compare(cur, base))
    assert "| entity | class_bench.recall@5 | 1.000 | - | - | MISSING |" in md


def test_main_writes_report_even_when_gate_fails(tmp_path, monkeypatch):
    out = tmp_path / "r.md"
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    cur = gate.collect_metrics()
    bad = {"metrics": {k: v + 1.0 for k, v in cur["metrics"].items()}, "leak_count": 0}
    bp = tmp_path / "b.json"
    gate.write_json(bad, bp)
    assert gate.main(["--baseline", str(bp), "--report-md", str(out)]) == 1
    assert "gate: FAIL" in out.read_text()
    assert "gate: FAIL" in summary.read_text()


def test_ci_never_regenerates_baseline():
    """Baseline regeneration is explicit-only: no workflow may pass --write-baseline."""
    import pathlib

    wf = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
    for f in wf.glob("*.yml"):
        for ln in f.read_text().splitlines():
            if "write-baseline" in ln:
                assert ln.lstrip().startswith("#"), f"{f.name}: {ln}"
