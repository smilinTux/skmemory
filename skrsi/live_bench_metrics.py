#!/usr/bin/env python3
"""Turn a live bench result, or the pinned live baseline, into an SKRSI metrics file.

usage: live_bench_metrics.py INPUT_JSON OUT_METRICS_JSON

INPUT_JSON is either the ``.rows.json`` that ``skmemory.eval.live_bench`` writes, or a
``skmemory.skrsi.baseline.v1`` file such as ``baselines/skmemory-retrieval-live-20261106.json``.
Both carry the candidate arm per query kind and its p95; this emits the live_* metrics of target
``skmemory-retrieval`` for that arm. Stdlib only.

A kind the run did not measure (a paraphrase set has no ``title`` kind) is null, so SKRSI
reports it as missing, never as zero. ``sabotage_tested`` is false because no test proves a
sabotaged arm turns the live bench red; SKRSI then caps the outcome, which is the honest result.
"""

import json
import sys

BASELINES = [
    "bm25_only",
    "vector_only",
    "sqlite_like",
    "lexical_local",
    "graph_only",
    "fallback_chain",
]


def _from_rows(raw: dict) -> tuple[dict, dict, float | None, int, str]:
    by_kind = raw["arms"]["candidate"]["by_kind"]
    recall = {k: v.get("recall@5") for k, v in by_kind.items()}
    mrr = {k: v.get("mrr") for k, v in by_kind.items()}
    # the bench's own p95 (nearest rank over calls that did not error), so this file and the table agree
    p95 = raw["arms"]["candidate"].get("p95_ms")
    p95 = None if p95 is None else round(p95, 1)
    return (
        recall,
        mrr,
        p95,
        int(raw["queries"]),
        f"live-{raw['evidence_class']}-seed{raw['meta'].get('seed')}",
    )


def _from_baseline(raw: dict) -> tuple[dict, dict, float | None, int, str]:
    return (
        raw["recall_at_5"]["candidate"],
        raw["mrr"]["candidate"],
        raw["latency_ms"]["candidate"]["p95"],
        int(raw["sample"]["queries"]),
        f"live-{raw['kind']}-seed{raw['sample']['seed']}",
    )


def _margin(arms: dict) -> float | None:
    """Worst query kind, candidate recall@5 minus bm25_only recall@5 (the target's objective).

    ``arms`` maps arm name to {kind: recall@5 or None}. The aggregate ``all`` is not a kind. None
    when either arm is absent or dead (no recall at all), or a kind is missing or null on either
    side: a dead arm or an unmeasured kind is never dropped from the minimum, so it can neither
    pass nor fail the stop rule.
    """
    cand, ref = arms.get("candidate"), arms.get("bm25_only")
    if not cand or not ref:
        return None
    kinds = (set(cand) | set(ref)) - {"all"}
    if not kinds:
        return None
    gaps = []
    for k in sorted(kinds):
        c, r = cand.get(k), ref.get(k)
        if c is None or r is None:
            return None
        gaps.append(c - r)
    return round(min(gaps), 4)


def _arms_from_rows(raw: dict) -> dict:
    out = {}
    for name in ("candidate", "bm25_only"):
        arm = raw.get("arms", {}).get(name) or {}
        if arm.get("status", "alive") != "alive":
            continue
        out[name] = {k: v.get("recall@5") for k, v in (arm.get("by_kind") or {}).items()}
    return out


def _arms_from_baseline(raw: dict) -> dict:
    rec = raw.get("recall_at_5", {})
    return {n: rec[n] for n in ("candidate", "bm25_only") if isinstance(rec.get(n), dict)}


WROTE_NOTHING = "a live measurement writes nothing to the store it measures"


def _invariants(raw: dict) -> dict:
    """Only what the run itself proves. The bench arms a write guard per backend and records each
    as "write refused"; a run without that record asserts nothing."""
    guards = raw.get("meta", {}).get("read_only_guards")
    if not isinstance(guards, dict) or not guards:
        return {}
    return {WROTE_NOTHING: all(v == "write refused" for v in guards.values())}


# Run-to-run spread of the live bench on this store (2026-11-07, seed 20261106, 182 to 398
# queries): recall and MRR moved by up to about 0.02 between runs of equivalent code, and p95 by
# about 300 ms with box load between 3 and 12. One query is 1/N (0.0025 to 0.0055). A delta
# inside these is "flat" in SKRSI (skrsi PR 8). Estimated from cross-run spread, not from a
# controlled repeat of one commit: replace with measured values when one is run.
NOISE_RATIO = 0.02
NOISE_P95_MS = 300.0


def flatten(raw: dict) -> dict:
    is_base = raw.get("schema") == "skmemory.skrsi.baseline.v1"
    recall, mrr, p95, n, cohort = (_from_baseline if is_base else _from_rows)(raw)
    margin = _margin((_arms_from_baseline if is_base else _arms_from_rows)(raw))

    def ratio(v):
        return {
            "value": v,
            "unit": "ratio",
            "direction": "higher",
            "tolerance": 0.02,
            "noise": NOISE_RATIO,
        }

    return {
        "cohort": {
            "id": cohort,
            "sample_size": n,
            "window_start": "1970-01-01T00:00:00Z",
            "window_end": "1970-01-01T00:00:00Z",
        },
        "instrument": {
            "runs_production_code": True,
            "baselines_named": BASELINES,
            "sabotage_tested": False,
            "dead_arms_reported": True,
        },
        "invariants": {} if is_base else _invariants(raw),
        "metrics": {
            "live_recall_at_5_all": ratio(recall.get("all")),
            "live_recall_at_5_title": ratio(recall.get("title")),
            "live_recall_at_5_rare_token": ratio(recall.get("rare_token")),
            "live_mrr_all": ratio(mrr.get("all")),
            "live_recall_margin_vs_bm25": {
                "value": margin,
                "unit": "ratio",
                "direction": "higher",
                "min": 0,
                "noise": NOISE_RATIO,
                "stop_rule": "fusion-below-bm25",
            },
            "live_p95_ms": {
                "value": p95,
                "unit": "ms",
                "direction": "lower",
                "tolerance": 0.2,
                "noise": NOISE_P95_MS,
                "stop_rule": "latency-regression",
            },
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
