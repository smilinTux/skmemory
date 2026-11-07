"""The SKRSI adoption flattener turns real class_bench output into honest metrics (SKRSI-02)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "class_bench_metrics", ROOT / "skrsi" / "class_bench_metrics.py"
)
cbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbm)


def raw(leaks=0, recalls=(1.0, 0.5, None)):
    return {
        "leak_count": leaks,
        "by_class": {
            f"c{i}": {"queries": 4, "strategies": {"candidate": {"recall@5": r}}}
            for i, r in enumerate(recalls)
        },
    }


def test_flatten_takes_worst_class_and_abstain_classes_have_no_recall():
    r = raw(recalls=(1.0, 0.5))
    r["by_class"]["absent"] = {
        "queries": 4,
        "strategies": {"candidate": {"recall@5": None, "abstain_correct": 1.0}},
    }
    out = cbm.flatten(r)
    assert out["metrics"]["class_recall_at_5_per_class"]["value"] == 0.5
    assert out["cohort"]["sample_size"] == 12
    assert out["instrument"]["runs_production_code"] is True


def test_a_dead_class_makes_the_metric_missing_not_the_minimum_of_the_rest():
    out = cbm.flatten(
        raw(recalls=(1.0, 0.5, None))
    )  # null recall without abstain scoring is a dead arm
    assert out["metrics"]["class_recall_at_5_per_class"]["value"] is None


def test_no_measured_class_is_missing_not_zero():
    assert (
        cbm.flatten(raw(recalls=(None,)))["metrics"]["class_recall_at_5_per_class"]["value"]
        is None
    )


def test_leak_is_bounded_and_asserted_false():
    out = cbm.flatten(raw(leaks=2))
    assert out["metrics"]["leak_count"]["value"] == 2 and out["metrics"]["leak_count"]["max"] == 0
    assert out["invariants"] == {"leak_count stays 0 for a declared non-owner reader": False}


def test_metric_names_and_invariants_exist_in_the_target():
    target = json.loads((ROOT / "skrsi" / "targets" / "skmemory-retrieval.json").read_text())
    out = cbm.flatten(raw())
    assert set(out["invariants"]) <= set(target["invariants"])
    assert out["metrics"]["leak_count"]["stop_rule"] in {r["id"] for r in target["stop_rules"]}


def test_per_class_recall_and_rank_are_separate_metrics():
    r = raw(recalls=(1.0, 0.5))
    r["by_class"]["c0"]["strategies"]["candidate"]["mrr"] = 0.8
    m = cbm.flatten(r)["metrics"]
    assert m["class_recall_at_5.c1"]["value"] == 0.5
    assert m["class_mrr.c0"]["value"] == 0.8


def test_rank_drop_with_flat_recall_is_visible_as_on_pr_120():
    def bench(semantic_mrr):
        r = raw(recalls=(0.64, 0.75))
        r["by_class"]["c1"]["strategies"]["candidate"]["mrr"] = semantic_mrr
        return cbm.flatten(r)["metrics"]

    before, after = bench(0.622), bench(0.512)
    assert before["class_recall_at_5_per_class"] == after["class_recall_at_5_per_class"]
    assert before["class_mrr.c1"]["value"] > after["class_mrr.c1"]["value"]


def test_margin_is_worst_class_candidate_minus_bm25_and_unbounded_offline():
    r = raw(recalls=(1.0, 0.667))
    for name, bm25 in (("c0", 1.0), ("c1", 1.0)):
        r["by_class"][name]["strategies"]["bm25_only"] = {"recall@5": bm25, "mrr": bm25}
    r["by_class"]["c0"]["strategies"]["candidate"]["mrr"] = 0.5
    r["by_class"]["c1"]["strategies"]["candidate"]["mrr"] = 0.9
    m = cbm.flatten(r)["metrics"]
    assert round(m["class_recall_margin_vs_bm25"]["value"], 3) == -0.333
    assert m["class_mrr_margin_vs_bm25"]["value"] == -0.5
    assert "min" not in m["class_recall_margin_vs_bm25"]
    assert "stop_rule" not in m["class_recall_margin_vs_bm25"]


def test_a_dead_arm_makes_the_margin_missing():
    r = raw(recalls=(1.0,))
    r["by_class"]["c0"]["strategies"]["bm25_only"] = {"recall@5": None, "mrr": None}
    assert cbm.flatten(r)["metrics"]["class_recall_margin_vs_bm25"]["value"] is None


def test_target_names_every_metric_the_real_bench_emits_and_only_live_ones_are_extra():
    from skmemory.eval import class_bench

    target = json.loads((ROOT / "skrsi" / "targets" / "skmemory-retrieval.json").read_text())
    emitted = set(cbm.flatten(class_bench.run())["metrics"])
    assert emitted <= set(target["metrics"])
    assert {m for m in set(target["metrics"]) - emitted if not m.startswith("live_")} == set()


def test_cli_roundtrip(tmp_path):
    src, dst = tmp_path / "raw.json", tmp_path / "m.json"
    src.write_text(json.dumps(raw()))
    subprocess.run(
        [sys.executable, str(ROOT / "skrsi" / "class_bench_metrics.py"), str(src), str(dst)],
        check=True,
    )
    assert json.loads(dst.read_text())["metrics"]["leak_count"]["value"] == 0
