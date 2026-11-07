#!/usr/bin/env python3
"""Flatten live retrieval evidence into an SKRSI metrics file, aggregates only.

usage: live_bench_metrics.py LIVE_JSON OUT_METRICS_JSON [MEASURED_AT]

LIVE_JSON is `skmemory.eval.live_bench` rows output, or a pinned baseline under skrsi/baselines/.
Stdlib only. Run it where the corpus is, then feed the result to the skrsi builder: CI cannot reach
a private corpus, and per-query rows must not leave the box. See docs/ADOPTER-GUIDE.md in skrsi.

live_recall_margin_vs_bm25 makes the fusion-below-bm25 stop rule machine-checked: the worst of
candidate minus bm25_only across query types, bounded at 0. A dead arm makes it missing.

sabotage_tested is false because the live runner has no sabotage test yet, so an evaluation from
here cannot reach pass. dead_arms_reported is true: live_bench reports a dead arm as dead.
"""

import json
import sys
from datetime import datetime, timezone

CANDIDATE = "candidate"
REFERENCE = "bm25_only"
READ_ONLY = "a live measurement writes nothing to the store it measures"


def _shape(raw: dict) -> dict:
    """Both inputs as {arm: {kind: {"recall": x, "mrr": x}}, plus p95 and sample size}."""
    if raw.get("schema") == "skmemory.skrsi.baseline.v1":
        arms = {
            arm: {
                kind: {"recall": value, "mrr": raw["mrr"].get(arm, {}).get(kind)}
                for kind, value in kinds.items()
            }
            for arm, kinds in raw["recall_at_5"].items()
        }
        p95 = raw["latency_ms"].get(CANDIDATE, {}).get("p95")
        return {
            "arms": arms,
            "p95": p95,
            "n": raw["sample"]["queries"],
            "at": raw["measured_at"],
            "kind": raw["kind"],
        }
    arms = {}
    for arm, entry in raw["arms"].items():
        if entry["status"] != "alive":  # dead is missing, never zero
            continue
        arms[arm] = {
            kind: {"recall": v.get("recall@5"), "mrr": v.get("mrr")}
            for kind, v in entry["by_kind"].items()
        }
    p95 = raw["arms"].get(CANDIDATE, {}).get("p95_ms") if CANDIDATE in arms else None
    meta = raw.get("meta", {})
    kind = raw.get("evidence_class") or meta.get("evidence_class") or "live_self_retrieval"
    return {"arms": arms, "p95": p95, "n": raw["queries"], "at": None, "kind": kind}


def flatten(raw: dict, measured_at: str | None = None) -> dict:
    s = _shape(raw)
    at = s["at"] or measured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cand, ref = s["arms"].get(CANDIDATE, {}), s["arms"].get(REFERENCE, {})

    def value(arm, kind, key):
        return arm.get(kind, {}).get(key)

    def ratio(v):
        return {"value": v, "unit": "ratio", "direction": "higher", "tolerance": 0.03}

    gaps = []
    for kind in sorted((set(cand) | set(ref)) - {"all"}):
        c, r = value(cand, kind, "recall"), value(ref, kind, "recall")
        gaps.append(None if c is None or r is None else round(c - r, 6))
    metrics = {
        f"live_recall_at_5_{k}": ratio(value(cand, k, "recall"))
        for k in ("all", "title", "rare_token")
    }
    metrics["live_mrr_all"] = ratio(value(cand, "all", "mrr"))
    metrics["live_p95_ms"] = {
        "value": s["p95"],
        "unit": "ms",
        "direction": "lower",
        "tolerance": 0.2,
        "stop_rule": "latency-regression",
    }
    metrics["live_recall_margin_vs_bm25"] = {
        "value": None if not gaps or None in gaps else min(gaps),
        "unit": "ratio",
        "direction": "higher",
        "min": 0,
        "stop_rule": "fusion-below-bm25",
    }
    return {
        "cohort": {"id": s["kind"], "sample_size": s["n"], "window_start": at, "window_end": at},
        "instrument": {
            "runs_production_code": True,
            "baselines_named": sorted(a for a in s["arms"] if a != CANDIDATE),
            "sabotage_tested": False,
            "dead_arms_reported": True,
        },
        # Asserted only from the guards the run recorded. A pinned baseline records none.
        "invariants": {READ_ONLY: True} if raw.get("meta", {}).get("read_only_guards") else {},
        "metrics": metrics,
    }


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        out = flatten(json.load(fh), sys.argv[3] if len(sys.argv) > 3 else None)
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
