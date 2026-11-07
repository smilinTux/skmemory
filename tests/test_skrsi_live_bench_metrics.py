"""skrsi/live_bench_metrics.py: live bench rows and the pinned baseline become SKRSI metrics."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "live_bench_metrics", ROOT / "skrsi" / "live_bench_metrics.py"
)
lbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lbm)


def _rows(kinds, guards=None, p95=19.04):
    by_kind = {k: {"recall@5": 0.5, "mrr": 0.4, "n": 2} for k in kinds}
    rows = []
    meta = {"seed": 7}
    if guards is not None:
        meta["read_only_guards"] = guards
    return {
        "evidence_class": "live_self_retrieval",
        "queries": 20,
        "meta": meta,
        "rows": rows,
        "arms": {"candidate": {"by_kind": by_kind, "p95_ms": p95}},
    }


def test_baseline_file_maps_candidate_arm():
    raw = json.loads(
        (ROOT / "skrsi" / "baselines" / "skmemory-retrieval-live-20261106.json").read_text()
    )
    m = lbm.flatten(raw)
    assert m["metrics"]["live_recall_at_5_all"]["value"] == raw["recall_at_5"]["candidate"]["all"]
    assert m["metrics"]["live_p95_ms"]["value"] == raw["latency_ms"]["candidate"]["p95"]
    assert m["invariants"] == {}
    assert m["cohort"]["sample_size"] == raw["sample"]["queries"]


def test_unmeasured_kind_is_null_not_zero():
    m = lbm.flatten(_rows(["all", "how"], guards={"pg": "write refused"}))
    assert m["metrics"]["live_recall_at_5_title"]["value"] is None
    assert m["metrics"]["live_recall_at_5_rare_token"]["value"] is None


def test_p95_is_the_bench_own_value():
    assert lbm.flatten(_rows(["all"]))["metrics"]["live_p95_ms"]["value"] == 19.0
    assert lbm.flatten(_rows(["all"], p95=None))["metrics"]["live_p95_ms"]["value"] is None


def test_write_guard_invariant_only_from_evidence():
    assert lbm.flatten(_rows(["all"]))["invariants"] == {}
    ok = lbm.flatten(_rows(["all"], guards={"pg": "write refused", "sqlite": "write refused"}))
    assert ok["invariants"] == {lbm.WROTE_NOTHING: True}
    bad = lbm.flatten(_rows(["all"], guards={"pg": "write refused", "sqlite": "not armed"}))
    assert bad["invariants"] == {lbm.WROTE_NOTHING: False}


def test_sabotage_not_claimed():
    assert lbm.flatten(_rows(["all"]))["instrument"]["sabotage_tested"] is False


def test_noise_floor_declared_on_every_metric():
    m = lbm.flatten(_rows(["all"]))["metrics"]
    assert all(spec["noise"] > 0 for spec in m.values())
    assert m["live_p95_ms"]["noise"] == lbm.NOISE_P95_MS


def _margin_rows(cand, ref, ref_status="alive"):
    def arm(kinds, status="alive"):
        return {
            "status": status,
            "by_kind": {k: {"recall@5": v, "mrr": 0.1, "n": 5} for k, v in kinds.items()},
        }

    raw = _rows(["all"], guards={"pg": "write refused"})
    raw["arms"] = {"candidate": arm(cand), "bm25_only": arm(ref, ref_status)}
    return raw


def _mv(raw):
    return lbm.flatten(raw)["metrics"]["live_recall_margin_vs_bm25"]


def test_margin_is_worst_kind_candidate_minus_bm25_and_skips_all():
    raw = _margin_rows(
        {"all": 0.9, "title": 0.455, "rare_token": 0.747},
        {"all": 0.1, "title": 0.625, "rare_token": 0.5},
    )
    m = _mv(raw)
    assert m["value"] == -0.17
    assert m["min"] == 0 and m["stop_rule"] == "fusion-below-bm25"


def test_margin_null_when_arm_dead_or_kind_missing():
    ok = {"title": 0.5, "rare_token": 0.5}
    assert _mv(_margin_rows(ok, ok, ref_status="dead"))["value"] is None
    assert _mv(_margin_rows(ok, {"title": 0.5}))["value"] is None
    assert _mv(_margin_rows({"title": 0.5}, ok))["value"] is None
    assert _mv(_margin_rows(ok, {"title": None, "rare_token": 0.5}))["value"] is None
    assert _mv(_margin_rows({"all": 0.5}, {"all": 0.5}))["value"] is None
    assert _mv(_rows(["all"]))["value"] is None  # no bm25_only arm at all


def test_margin_from_baseline_file():
    raw = json.loads(
        (ROOT / "skrsi" / "baselines" / "skmemory-retrieval-live-20261106.json").read_text()
    )
    assert _mv(raw)["value"] == round(0.455 - 0.625, 4)
    raw["recall_at_5"]["bm25_only"] = None
    assert _mv(raw)["value"] is None


def test_live_metrics_and_stop_rules_exist_in_the_target():
    target = json.loads((ROOT / "skrsi" / "targets" / "skmemory-retrieval.json").read_text())
    m = lbm.flatten(_rows(["all"]))["metrics"]
    assert set(m) <= set(target["metrics"])
    rules = {r["id"] for r in target["stop_rules"]}
    assert {s["stop_rule"] for s in m.values() if "stop_rule" in s} <= rules
    assert target["revision"] == 3
