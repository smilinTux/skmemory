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
