#!/usr/bin/env python3
"""Flatten `python -m skmemory.eval.class_bench --json` into an SKRSI metrics file.

usage: class_bench_metrics.py RAW_BENCH_JSON OUT_METRICS_JSON

Stdlib only. Emits the two metrics of target `skmemory-retrieval` that an offline,
synthetic bench can honestly produce. Everything else (the live_* metrics) is left out
so SKRSI reports it as missing, never as zero.
"""

import json
import sys

BASELINES = ["bm25_only", "grep_substring", "vector_only", "fallback_chain"]


def flatten(raw: dict) -> dict:
    classes = raw["by_class"]
    # worst class for the production candidate: a mean would hide a collapsed class
    recalls = [
        c["strategies"]["candidate"]["recall@5"]
        for c in classes.values()
        if c["strategies"]["candidate"].get("recall@5") is not None
    ]
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
        "metrics": {
            "class_recall_at_5_per_class": {
                "value": min(recalls) if recalls else None,
                "unit": "ratio",
                "direction": "higher",
            },
            "leak_count": {
                "value": raw["leak_count"],
                "unit": "items",
                "direction": "lower",
                "max": 0,
                "stop_rule": "privacy-regression",
            },
        },
    }


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        out = flatten(json.load(fh))
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
