#!/usr/bin/env python3
"""CI regression gate over the offline skmemory eval harnesses.

Runs the three fully-offline, deterministic eval harnesses in this package,
collects their key metrics into a flat machine-readable dict, and COMPARES that
against a committed baseline (``eval/baseline.json``). A pull request that
WORSENS any gated score (or leaks a private item) makes this exit non-zero, so
CI can turn the PR RED. Unrelated PRs never reach this job (the workflow path
filter skips it).

What it runs (all in-process, NO network, NO skmem-pg, NO embed endpoint):

    recall_benchmark.run_benchmark()   -> recall@k / ndcg@k (SQLite keyword path)
    retrieval_bench.run_benchmark()    -> precision@k / recall@k / mrr + leak count
                                          (hybrid BM25 + deterministic hashed
                                          vector, real audience privacy filter)
    groundedness_scorer                -> groundedness score over a fixed
                                          (answer, citations) fixture, judged by
                                          the OFFLINE deterministic
                                          ``lexical_overlap_judge`` (NOT the live
                                          sk-default judge, so no network).

Gate semantics
--------------
* Every metric in the baseline is "higher is better": a run REGRESSES if the
  current value drops below ``baseline - epsilon``. ``epsilon`` absorbs float
  noise only (the harnesses are deterministic, so a genuine drop always fails).
* ``leak_count`` is a HARD constraint: it must be 0. Any private item that
  surfaces for a ``@public`` reader is a regression regardless of the baseline.
* ``leak_count_unfiltered`` is recorded for context (it proves the leak trap is
  non-vacuous) but is not itself gated.
* A metric that is present in the baseline but missing from the current run is
  treated as a regression (the harness lost coverage).

Run:
    python -m skmemory.eval.regression_gate                 # gate vs baseline
    python -m skmemory.eval.regression_gate --write-baseline # (re)generate floor
    python -m skmemory.eval.regression_gate --output cur.json --no-compare
Import:
    from skmemory.eval.regression_gate import (
        collect_metrics, compare, load_baseline, DEFAULT_EPSILON,
    )
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from skmemory.eval.groundedness_scorer import (
    lexical_overlap_judge,
    score_groundedness,
)
from skmemory.eval.recall_benchmark import run_benchmark as run_recall_benchmark
from skmemory.eval.retrieval_bench import run_benchmark as run_retrieval_benchmark

# Default location of the committed baseline (this file lives in skmemory/eval/).
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

# Float tolerance: absorbs IEEE noise only. The harnesses are deterministic, so
# any real metric decrease exceeds this and fails the gate.
DEFAULT_EPSILON = 1e-9

# =============================================================================
# GROUNDEDNESS FIXTURE - a fixed (answer, citations) pair scored by the OFFLINE
# deterministic lexical-overlap judge. Two supported claims, one hallucinated
# claim (the model "trained overnight" line has no citation support), so the
# offline judge yields a stable, meaningful sub-1.0 floor.
# =============================================================================

GROUNDEDNESS_CITATIONS: list[str] = [
    "The mxbai-embed-large embedding server runs on 192.168.0.100 port 11434.",
    "skmem-pg is a Postgres image with pgvector and BM25 via pg_search.",
    "The dedup cosine similarity threshold for mxbai-embed-large was tuned to 0.73.",
]
GROUNDEDNESS_ANSWER: str = (
    "The mxbai-embed-large embedding server runs on port 11434. "
    "skmem-pg is a Postgres image with pgvector and BM25 via pg_search. "
    "The dedup cosine similarity threshold was tuned to 0.73. "
    "It also automatically trains a brand new embedding model every single night."
)


def _groundedness_score() -> float:
    """Deterministic offline groundedness score over the fixed fixture."""
    result = score_groundedness(
        GROUNDEDNESS_ANSWER,
        GROUNDEDNESS_CITATIONS,
        judge_fn=lexical_overlap_judge,
    )
    return result.score


# =============================================================================
# METRIC COLLECTION
# =============================================================================


def collect_metrics() -> dict:
    """Run all three offline harnesses and flatten their key metrics.

    Returns a dict of the shape::

        {
            "metrics": { "<harness>.<metric>": float, ... },  # higher is better
            "leak_count": int,               # HARD gate: must be 0
            "leak_count_unfiltered": int,    # context only, not gated
        }

    Fully deterministic and offline - no network, no services.
    """
    recall = run_recall_benchmark()
    retrieval = run_retrieval_benchmark()

    metrics: dict[str, float] = {}

    for key, value in recall["aggregate"].items():
        metrics[f"recall_bench.{key}"] = float(value)

    for key, value in retrieval["aggregate"].items():
        metrics[f"retrieval_bench.{key}"] = float(value)

    metrics["groundedness.score"] = float(_groundedness_score())

    return {
        "metrics": metrics,
        "leak_count": int(retrieval["leak_count"]),
        "leak_count_unfiltered": int(retrieval["leak_count_unfiltered"]),
    }


# =============================================================================
# COMPARISON
# =============================================================================


def compare(
    current: Mapping,
    baseline: Mapping,
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> list[str]:
    """Compare a *current* metric dict against a *baseline*; list regressions.

    A regression is any of:

      * a gated metric dropping below ``baseline - epsilon``;
      * a baseline metric missing from the current run;
      * ``leak_count`` being greater than 0 (hard privacy constraint).

    Args:
        current: dict from :func:`collect_metrics` (or the same shape).
        baseline: the committed baseline, same shape.
        epsilon: float tolerance for the "higher is better" comparison.

    Returns:
        A list of human-readable regression descriptions. EMPTY means PASS.
    """
    regressions: list[str] = []

    base_metrics = baseline.get("metrics", {})
    cur_metrics = current.get("metrics", {})

    for name, base_val in base_metrics.items():
        if name not in cur_metrics:
            regressions.append(f"{name}: MISSING from current run (baseline {base_val:.6f})")
            continue
        cur_val = cur_metrics[name]
        if cur_val < base_val - epsilon:
            regressions.append(
                f"{name}: {cur_val:.6f} < baseline {base_val:.6f} (drop {base_val - cur_val:.6f})"
            )

    # HARD privacy gate: any leak is a regression, independent of the baseline.
    leak = current.get("leak_count", 0)
    if leak and leak > 0:
        regressions.append(f"leak_count: {leak} > 0 (private item surfaced for @public reader)")

    return regressions


# =============================================================================
# IO
# =============================================================================


def load_baseline(path: str | os.PathLike = BASELINE_PATH) -> dict:
    """Load a baseline JSON file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _baseline_document(metrics: dict) -> dict:
    """Wrap collected metrics with a small note for the committed baseline."""
    return {
        "_note": (
            "Regression-gate baseline for skmemory eval harnesses. Regenerate "
            "with: python -m skmemory.eval.regression_gate --write-baseline. "
            "Every metric is higher-is-better; leak_count must stay 0."
        ),
        "metrics": metrics["metrics"],
        "leak_count": metrics["leak_count"],
        "leak_count_unfiltered": metrics["leak_count_unfiltered"],
    }


def write_json(data: dict, path: str | os.PathLike) -> None:
    """Write *data* as pretty, stable JSON (sorted keys, trailing newline)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


# =============================================================================
# REPORTING
# =============================================================================


def _print_metrics(current: dict) -> None:
    print("skmemory regression gate - collected offline metrics\n")
    for name in sorted(current["metrics"]):
        print(f"  {name:<34} {current['metrics'][name]:.6f}")
    print(f"  {'leak_count':<34} {current['leak_count']}   (must be 0)")
    print(
        f"  {'leak_count_unfiltered':<34} {current['leak_count_unfiltered']}   "
        f"(context: private items the raw hybrid would surface)"
    )


# =============================================================================
# CLI
# =============================================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m skmemory.eval.regression_gate",
        description="Offline eval regression gate for skmemory (CI-friendly).",
    )
    parser.add_argument(
        "--baseline",
        default=str(BASELINE_PATH),
        help="Path to the baseline JSON (default: eval/baseline.json).",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Run the harnesses and (over)write the baseline, then exit 0.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Also write the current run's metrics JSON to this path.",
    )
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help="Collect + print (and optionally --output) but do not gate.",
    )
    parser.add_argument(
        "--epsilon",
        type=float,
        default=DEFAULT_EPSILON,
        help=f"Float tolerance for the higher-is-better check (default {DEFAULT_EPSILON}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code (0 = pass, 1 = regression)."""
    args = _build_parser().parse_args(argv)

    current = collect_metrics()
    _print_metrics(current)

    if args.output:
        write_json(current, args.output)
        print(f"\nwrote current metrics -> {args.output}")

    if args.write_baseline:
        write_json(_baseline_document(current), args.baseline)
        print(f"\nwrote baseline -> {args.baseline}")
        return 0

    if args.no_compare:
        return 0

    try:
        baseline = load_baseline(args.baseline)
    except FileNotFoundError:
        print(
            f"\nERROR: baseline not found at {args.baseline}. Generate it with --write-baseline.",
            file=sys.stderr,
        )
        return 2

    regressions = compare(current, baseline, epsilon=args.epsilon)

    print()
    if regressions:
        print(f"REGRESSION GATE: FAIL ({len(regressions)} regression(s))")
        for line in regressions:
            print(f"  x {line}")
        return 1

    print("REGRESSION GATE: PASS (no metric dropped below baseline; leak_count 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
