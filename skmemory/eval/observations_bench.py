#!/usr/bin/env python3
"""Deterministic measurement harness for the observations layer (R3).

Why this file exists
---------------------
``skmemory.observations`` makes three load-bearing claims:

1. ``trend`` is computed by pure arithmetic over evidence timestamps, with
   no LLM and no judgment call -- so it has to actually land on the right
   one of the five values for hand-built distributions that are each
   unambiguously one trend and not another.
2. A fabricated citation -- a quote that is NOT an exact substring of the
   memory it claims to be from -- must be CAUGHT, not silently trusted.
   This is the specific failure mode the whole "evidence" design exists to
   rule out: a belief that LOOKS grounded but isn't is worse than a belief
   with no evidence at all, because nothing about the Observation object
   itself tells you it's lying.
3. Merging the same evidence into an Observation twice must be a no-op the
   second time (idempotent), so a job that re-derives observations from the
   same memories on every run never inflates ``proof_count``.

Each of these is exactly the shape of defect the 2026-10-16 postmortem
catalogued under "a wrong answer that looks exactly like a right one"
(``docs/postmortems/2026-10-16-sqlite-only-orphans.md``): a trend that's
silently wrong reads as a plausible trend, an unverified quote reads as a
verified one, and a merge that silently double-counts reads as a correct
proof count. So this harness doesn't just exercise the code -- it prints
the specific number each claim depends on, so a regression shows up as a
number that moved, not as a vibe.

Offline and deterministic
--------------------------
No network, no DB, no LLM, no wall-clock dependence where it matters: every
fixture either passes an explicit ``now`` to :func:`compute_trend` or
builds timestamps relative to a fixed epoch, so results never depend on
when this file happens to run.

Run:
    python -m skmemory.eval.observations_bench            # the report
    python -m skmemory.eval.observations_bench --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from skmemory.observations import (
    DEFAULT_TREND_WINDOW,
    Observation,
    ObservationEvidence,
    compute_trend,
    merge_evidence,
)

# =============================================================================
# FIXED REFERENCE TIME -- every fixture is built relative to this, never to
# the real wall clock, so the trend section of the report never flips.
# =============================================================================

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


def _days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


# =============================================================================
# 1. TREND CLASSIFICATION -- one unambiguous fixture per trend value
# =============================================================================

#: (trend_name, timestamps, expected_value). Each fixture is built to be
#: unambiguous under the rules documented on ``compute_trend``: only one of
#: the five outcomes is reachable from its timestamp distribution.
TREND_FIXTURES: list[tuple[str, list[datetime], str]] = [
    (
        "new (fresh belief, no prior history)",
        [_days_ago(1), _days_ago(3), _days_ago(5)],
        "new",
    ),
    (
        "stale (had history, nothing in the last window)",
        [_days_ago(60), _days_ago(75), _days_ago(90)],
        "stale",
    ),
    (
        "strengthening (recent rate well above prior rate)",
        [_days_ago(d) for d in range(100, 40, -10)] + [_days_ago(d) for d in range(1, 10)],
        "strengthening",
    ),
    (
        "weakening (recent rate well below prior rate)",
        [_days_ago(d) for d in range(20, 100)] + [_days_ago(1)],
        "weakening",
    ),
    (
        "stable (recent rate close to prior rate)",
        [_days_ago(d) for d in range(20, 100, 15)] + [_days_ago(1)],
        "stable",
    ),
]


def run_trend_classification() -> dict:
    """Score every trend fixture. Returns pass/fail detail plus a summary count."""
    results = []
    for label, timestamps, expected in TREND_FIXTURES:
        got = compute_trend(timestamps, now=NOW, window=DEFAULT_TREND_WINDOW)
        results.append(
            {
                "fixture": label,
                "expected": expected,
                "got": got,
                "pass": got == expected,
                "n_timestamps": len(timestamps),
            }
        )
    passed = sum(1 for r in results if r["pass"])
    return {"results": results, "passed": passed, "total": len(results)}


# =============================================================================
# 2. QUOTE VERIFICATION -- a fabricated quote must be caught
# =============================================================================


def run_quote_verification() -> dict:
    """Build one observation with a genuine quote and one fabricated quote.

    The genuine quote is an exact substring of its cited memory's content;
    the fabricated one is plausible-looking prose that was never actually
    in that memory. ``find_unverifiable_evidence`` must flag exactly the
    fabricated one and nothing else.
    """
    from skmemory.observations import find_unverifiable_evidence

    memory_contents = {
        "mem-genuine": "The skgateway pool ceiling was raised to 32 workers after the audit.",
        "mem-fabricated": "The nightly backup job runs at 2am and rotates seven days of snapshots.",
    }

    genuine = ObservationEvidence(
        memory_id="mem-genuine",
        quote="The skgateway pool ceiling was raised to 32 workers",
        timestamp=_days_ago(2),
    )
    fabricated = ObservationEvidence(
        memory_id="mem-fabricated",
        # Plausible, but NOT a substring of mem-fabricated's real content.
        quote="The nightly backup job runs at 3am and keeps thirty days of snapshots.",
        timestamp=_days_ago(1),
    )
    observation = Observation(
        statement="skgateway capacity and backup cadence were both adjusted recently.",
        evidence=[genuine, fabricated],
    )

    unverifiable = find_unverifiable_evidence(observation, memory_contents)
    caught_fabricated = any(ev.memory_id == "mem-fabricated" for ev in unverifiable)
    false_flagged_genuine = any(ev.memory_id == "mem-genuine" for ev in unverifiable)

    return {
        "total_evidence": observation.proof_count,
        "unverifiable_count": len(unverifiable),
        "caught_the_fabricated_quote": caught_fabricated,
        "false_flagged_the_genuine_quote": false_flagged_genuine,
        "pass": caught_fabricated and not false_flagged_genuine and len(unverifiable) == 1,
    }


# =============================================================================
# 3. MERGE IDEMPOTENCY -- applying the same evidence twice must not double-count
# =============================================================================


def run_merge_idempotency() -> dict:
    """Merge one evidence batch into an empty observation once, then again.

    ``proof_count`` after the second (redundant) merge must equal
    ``proof_count`` after the first.
    """
    base = Observation(statement="chiap08 keeps coming up in beelama incident notes.")
    batch = [
        ObservationEvidence(
            memory_id="mem-1", quote="chiap08 hosts the beelama endpoint.", timestamp=_days_ago(10)
        ),
        ObservationEvidence(
            memory_id="mem-2",
            quote="beelama on chiap08 degraded again overnight.",
            timestamp=_days_ago(3),
        ),
    ]

    once = merge_evidence(base, batch)
    twice = merge_evidence(once, batch)
    # Also check applying the identical starting batch directly to `once` a
    # third time by rebuilding from `base` fresh, to rule out any ordering
    # dependency rather than only testing "merge into the already-merged one".
    thrice_from_scratch = merge_evidence(merge_evidence(base, batch), batch)

    return {
        "proof_count_after_one_merge": once.proof_count,
        "proof_count_after_two_merges": twice.proof_count,
        "proof_count_after_merge_from_scratch_twice": thrice_from_scratch.proof_count,
        "pass": (
            once.proof_count == 2
            and twice.proof_count == once.proof_count
            and thrice_from_scratch.proof_count == once.proof_count
        ),
    }


# =============================================================================
# RUNNER
# =============================================================================


def run_benchmark() -> dict:
    trend = run_trend_classification()
    quotes = run_quote_verification()
    merge = run_merge_idempotency()
    overall_pass = trend["passed"] == trend["total"] and quotes["pass"] and merge["pass"]
    return {
        "trend_classification": trend,
        "quote_verification": quotes,
        "merge_idempotency": merge,
        "overall_pass": overall_pass,
    }


def _print_report(res: dict) -> None:
    print("\nskmemory observations bench\n")

    trend = res["trend_classification"]
    print(f"1. Trend classification ({trend['passed']}/{trend['total']} fixtures correct)")
    header = f"{'fixture':<55}{'expected':>14}{'got':>14}{'pass':>7}"
    print(header)
    print("-" * len(header))
    for r in trend["results"]:
        print(f"{r['fixture']:<55}{r['expected']:>14}{r['got']:>14}{str(r['pass']):>7}")

    quotes = res["quote_verification"]
    print(
        f"\n2. Quote verification: {quotes['total_evidence']} evidence items, "
        f"{quotes['unverifiable_count']} flagged unverifiable "
        f"(caught fabricated = {quotes['caught_the_fabricated_quote']}, "
        f"false-flagged genuine = {quotes['false_flagged_the_genuine_quote']}) "
        f"-> {'PASS' if quotes['pass'] else 'FAIL'}"
    )

    merge = res["merge_idempotency"]
    print(
        f"\n3. Merge idempotency: proof_count after 1 merge = "
        f"{merge['proof_count_after_one_merge']}, after 2 merges = "
        f"{merge['proof_count_after_two_merges']}, after merge-from-scratch "
        f"twice = {merge['proof_count_after_merge_from_scratch_twice']} "
        f"-> {'PASS' if merge['pass'] else 'FAIL'}"
    )

    print(f"\nOVERALL: {'PASS' if res['overall_pass'] else 'FAIL'}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="skmemory observations layer bench")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
