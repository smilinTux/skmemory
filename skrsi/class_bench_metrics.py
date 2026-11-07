#!/usr/bin/env python3
"""Flatten `python -m skmemory.eval.class_bench --json` into an SKRSI metrics file.

usage: class_bench_metrics.py RAW_BENCH_JSON OUT_METRICS_JSON

Stdlib only. Emits what an offline, synthetic bench can honestly produce. The live_* metrics
are left out so SKRSI reports them as missing, never as zero.

One metric per class, recall and rank both: a worst-class scalar alone read flat on PR 120 while
mean reciprocal rank moved in five classes. The margins measure the target's objective, candidate
against bm25_only per class. They carry no bound and no stop rule here: fusion-below-bm25 is
decided on live evidence, and this offline margin is already negative, so a bound would blame
every PR. They still show when a PR makes the gap worse.
"""

import json
import sys

BASELINES = ["bm25_only", "grep_substring", "vector_only", "fallback_chain"]
CANDIDATE = "candidate"
REFERENCE = "bm25_only"


def _metric(value, unit="ratio"):
    return {"value": value, "unit": unit, "direction": "higher"}


def _worst(values):
    """None when any value is None: a dead class is never dropped from a minimum."""
    return None if not values or None in values else min(values)


def _margin(classes, key):
    gaps = []
    for entry in classes.values():
        cand, ref = entry["strategies"][CANDIDATE], entry["strategies"].get(REFERENCE, {})
        if "abstain_correct" in cand:  # abstain classes have no recall or rank by design
            continue
        both = cand.get(key) is not None and ref.get(key) is not None
        gaps.append(cand[key] - ref[key] if both else None)
    return _worst(gaps)


def flatten(raw: dict) -> dict:
    classes = raw["by_class"]
    metrics = {}
    recalls = []
    for name, entry in sorted(classes.items()):
        cand = entry["strategies"][CANDIDATE]
        if "abstain_correct" in cand:
            metrics[f"class_abstain_correct.{name}"] = _metric(cand["abstain_correct"])
            continue
        recalls.append(cand.get("recall@5"))
        metrics[f"class_recall_at_5.{name}"] = _metric(cand.get("recall@5"))
        metrics[f"class_mrr.{name}"] = _metric(cand.get("mrr"))
    metrics["class_recall_at_5_per_class"] = _metric(_worst(recalls))
    metrics["class_recall_margin_vs_bm25"] = _metric(_margin(classes, "recall@5"))
    metrics["class_mrr_margin_vs_bm25"] = _metric(_margin(classes, "mrr"))
    metrics["leak_count"] = {
        "value": raw["leak_count"],
        "unit": "items",
        "direction": "lower",
        "max": 0,
        "stop_rule": "privacy-regression",
    }
    return {
        "cohort": {
            "id": "class-bench-v1",
            "sample_size": sum(c["queries"] for c in classes.values()),
            "window_start": "1970-01-01T00:00:00Z",
            "window_end": "1970-01-01T00:00:00Z",
        },
        # candidate arm runs production MemoryStore.search; tests/test_class_bench.py proves a sabotaged
        # fusion turns the gate red. In-process arms cannot die, so dead_arms_reported stays false.
        "instrument": {
            "runs_production_code": True,
            "baselines_named": BASELINES,
            "sabotage_tested": True,
            "dead_arms_reported": False,
        },
        "invariants": {
            "leak_count stays 0 for a declared non-owner reader": raw["leak_count"] == 0
        },
        "metrics": metrics,
    }


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        out = flatten(json.load(fh))
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
