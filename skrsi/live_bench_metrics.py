#!/usr/bin/env python3
"""Turn a live bench result, or the pinned live baseline, into an SKRSI metrics file.

usage: live_bench_metrics.py INPUT_JSON OUT_METRICS_JSON

INPUT_JSON is either the ``.rows.json`` that ``skmemory.eval.live_bench`` writes, or a
``skmemory.skrsi.baseline.v1`` file such as ``baselines/skmemory-retrieval-live-20261106.json``.
Both carry the candidate arm per query kind; this emits the live_* metrics of target
``skmemory-retrieval`` for that arm. Stdlib only.

A kind the run did not measure (a paraphrase set has no ``title`` kind) is null, so SKRSI
reports it as missing, never as zero. ``sabotage_tested`` is false because no test proves a
sabotaged arm turns the live bench red; SKRSI then caps the outcome, which is the honest result.
"""

import json
import sys

BASELINES = ["bm25_only", "vector_only", "sqlite_like", "lexical_local", "graph_only", "fallback_chain"]


def _p95(latencies: list) -> float | None:
    if not latencies:
        return None
    xs = sorted(latencies)
    return round(xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))], 1)


def _from_rows(raw: dict) -> tuple[dict, dict, float | None, int, str]:
    by_kind = raw["arms"]["candidate"]["by_kind"]
    recall = {k: v.get("recall@5") for k, v in by_kind.items()}
    mrr = {k: v.get("mrr") for k, v in by_kind.items()}
    lat = [r["per"]["candidate"]["ms"] for r in raw["rows"] if not r["per"]["candidate"].get("error")]
    return recall, mrr, _p95(lat), int(raw["queries"]), f"live-{raw['evidence_class']}-seed{raw['meta'].get('seed')}"


def _from_baseline(raw: dict) -> tuple[dict, dict, float | None, int, str]:
    return (raw["recall_at_5"]["candidate"], raw["mrr"]["candidate"], raw["latency_ms"]["candidate"]["p95"],
            int(raw["sample"]["queries"]), f"live-{raw['kind']}-seed{raw['sample']['seed']}")


WROTE_NOTHING = "a live measurement writes nothing to the store it measures"


def _invariants(raw: dict) -> dict:
    """Only what the run itself proves. The bench arms a write guard per backend and records each
    as "write refused"; a run without that record asserts nothing."""
    guards = raw.get("meta", {}).get("read_only_guards")
    if not isinstance(guards, dict) or not guards:
        return {}
    return {WROTE_NOTHING: all(v == "write refused" for v in guards.values())}


def flatten(raw: dict) -> dict:
    is_base = raw.get("schema") == "skmemory.skrsi.baseline.v1"
    recall, mrr, p95, n, cohort = (_from_baseline if is_base else _from_rows)(raw)

    def ratio(v):
        return {"value": v, "unit": "ratio", "direction": "higher", "tolerance": 0.02}

    return {
        "cohort": {"id": cohort, "sample_size": n,
                   "window_start": "1970-01-01T00:00:00Z", "window_end": "1970-01-01T00:00:00Z"},
        "instrument": {"runs_production_code": True, "baselines_named": BASELINES,
                       "sabotage_tested": False, "dead_arms_reported": True},
        "invariants": {} if is_base else _invariants(raw),
        "metrics": {
            "live_recall_at_5_all": ratio(recall.get("all")),
            "live_recall_at_5_title": ratio(recall.get("title")),
            "live_recall_at_5_rare_token": ratio(recall.get("rare_token")),
            "live_mrr_all": ratio(mrr.get("all")),
            "live_p95_ms": {"value": p95, "unit": "ms", "direction": "lower", "tolerance": 0.2,
                            "stop_rule": "latency-regression"},
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = flatten(json.load(fh))
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
