"""The live flattener turns pinned or fresh live evidence into honest SKRSI metrics."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "live_bench_metrics", ROOT / "skrsi" / "live_bench_metrics.py"
)
lbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lbm)
PINNED = ROOT / "skrsi" / "baselines" / "skmemory-retrieval-live-20261106.json"


def rows(candidate_status="alive", bm25_status="alive"):
    def arm(status, title, rare):
        return {
            "status": status,
            "p95_ms": 900.0,
            "by_kind": {
                "all": {"n": 2, "recall@5": (title + rare) / 2, "mrr": 0.5},
                "title": {"n": 1, "recall@5": title, "mrr": 0.5},
                "rare_token": {"n": 1, "recall@5": rare, "mrr": 0.5},
            },
        }

    return {
        "queries": 398,
        "kinds": ["rare_token", "title"],
        "arms": {
            "candidate": arm(candidate_status, 0.7, 0.9),
            "bm25_only": arm(bm25_status, 0.6, 0.95),
        },
        "rows": [{"id": "private", "kind": "title", "per": {}}],
    }


def test_pinned_baseline_shows_the_recorded_fusion_gap():
    m = lbm.flatten(json.loads(PINNED.read_text()))
    assert m["metrics"]["live_recall_at_5_all"]["value"] == 0.601
    assert m["metrics"]["live_p95_ms"]["value"] == 4952
    margin = m["metrics"]["live_recall_margin_vs_bm25"]
    assert margin["value"] == -0.17  # title: 0.455 - 0.625
    assert margin["min"] == 0 and margin["stop_rule"] == "fusion-below-bm25"
    assert m["cohort"]["sample_size"] == 398
    assert m["cohort"]["id"] == "live_self_retrieval"


def test_margin_is_the_worst_query_type():
    assert lbm.flatten(rows())["metrics"]["live_recall_margin_vs_bm25"]["value"] == -0.05


def test_a_dead_candidate_is_missing_not_zero():
    m = lbm.flatten(rows(candidate_status="dead"))["metrics"]
    assert m["live_recall_at_5_all"]["value"] is None
    assert m["live_p95_ms"]["value"] is None
    assert m["live_recall_margin_vs_bm25"]["value"] is None


def test_a_dead_reference_arm_makes_the_margin_missing_not_a_win():
    m = lbm.flatten(rows(bm25_status="dead"))
    assert m["metrics"]["live_recall_margin_vs_bm25"]["value"] is None
    assert "bm25_only" not in m["instrument"]["baselines_named"]


def test_no_per_query_rows_reach_the_output():
    assert "private" not in json.dumps(lbm.flatten(rows()))


def test_no_sabotage_claim_until_one_exists():
    assert lbm.flatten(rows())["instrument"]["sabotage_tested"] is False


def test_read_only_is_asserted_only_from_recorded_guards():
    assert lbm.flatten(rows())["invariants"] == {}
    assert lbm.flatten(json.loads(PINNED.read_text()))["invariants"] == {}
    guarded = {**rows(), "meta": {"read_only_guards": {"sqlite": "write refused"}}}
    assert lbm.flatten(guarded)["invariants"] == {lbm.READ_ONLY: True}


def test_names_exist_in_the_target():
    target = json.loads((ROOT / "skrsi" / "targets" / "skmemory-retrieval.json").read_text())
    m = lbm.flatten({**rows(), "meta": {"read_only_guards": {"sqlite": "write refused"}}})
    assert set(m["metrics"]) <= set(target["metrics"])
    assert set(m["invariants"]) <= set(target["invariants"])
    rules = {r["id"] for r in target["stop_rules"]}
    assert all(s["stop_rule"] in rules for s in m["metrics"].values() if "stop_rule" in s)
