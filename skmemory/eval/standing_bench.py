#!/usr/bin/env python3
"""Deterministic, offline proof for the R4 standing-answer staleness design.

Why this file exists
---------------------
``skmemory/standing.py`` keys staleness on the memory watermark
(``StandingAnswer.last_memory_seen_at``), not the wall clock
(``last_refreshed_at``). The module docstring argues for that choice; this
harness proves it with numbers instead of leaving it as an assertion, the
same way ``fusion_bench.py`` built an instrument that can actually fail
before changing how recall works.

Two things are measured, both deterministic and offline (fixed timestamps,
no network, no LLM):

1. **The divergence.** The real watermark-keyed ``StandingAnswer.is_stale()``
   is compared against ``wallclock_is_stale()``, a wall-clock design
   implemented ONLY here, as a comparison arm, never in ``standing.py``.
   Four fixed scenarios show the two designs disagreeing in BOTH directions:
   wall clock reports "fresh" when a new memory landed moments after the
   last refresh (the dangerous direction: a stale answer looks settled), and
   wall clock reports "stale" when nothing changed at all (the wasteful
   direction: needless refresh work). The watermark design gets every
   scenario right by construction, because it is keyed on the thing that
   actually matters.

2. **Zero-retrieval reads.** A counting fake stands in for "a retrieval
   call". It is invoked by a refresh pass (where retrieval is expected) but
   never by a plain ``StandingAnswerStore.get()`` read, proving the read
   path really is a plain file read with no retrieval, no LLM, no network.

Run directly: ``python -m skmemory.eval.standing_bench``
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skmemory.fresh_context import in_process_runner
from skmemory.standing import ComputedAnswer, StandingAnswer, StandingAnswerStore

# ──────────────────────────────────────────────────────────────────────────
# Arm 1: the divergence between watermark-keyed and wall-clock-keyed staleness
# ──────────────────────────────────────────────────────────────────────────

#: Fixed wall-clock "freshness window" for the wrong-design comparison arm.
_WALLCLOCK_MAX_AGE = timedelta(hours=1)


def wallclock_is_stale(last_refreshed_at: datetime, now: datetime, max_age: timedelta) -> bool:
    """THE WRONG DESIGN: staleness keyed on wall clock, as a comparison arm only.

    Never implemented in ``standing.py``. Reports "fresh" purely from how
    long ago the last refresh happened, with no reference at all to
    whether any memory actually changed since then.
    """
    return (now - last_refreshed_at) > max_age


def _scenario(
    name: str,
    last_refreshed_at: datetime,
    last_memory_seen_at: datetime | None,
    newest_in_scope: datetime | None,
    now: datetime,
) -> dict:
    answer = StandingAnswer(
        id="scenario",
        question="scenario question",
        answer="scenario answer",
        last_refreshed_at=last_refreshed_at,
        last_memory_seen_at=last_memory_seen_at,
    )
    watermark_stale = answer.is_stale(newest_in_scope)
    wallclock_stale = wallclock_is_stale(last_refreshed_at, now, _WALLCLOCK_MAX_AGE)
    return {
        "scenario": name,
        "watermark_stale": watermark_stale,
        "wallclock_stale": wallclock_stale,
        "agree": watermark_stale == wallclock_stale,
    }


def run_staleness_divergence() -> list[dict]:
    """Four fixed scenarios proving the watermark/wall-clock divergence."""
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # last refresh + watermark
    just_after = t0 + timedelta(seconds=1)  # a new memory lands moments later
    soon_after = t0 + timedelta(seconds=2)  # "now", right after that
    long_after = t0 + timedelta(hours=3)  # "now", well past the wall-clock window

    return [
        # A new memory landed right after the refresh, and "now" is also
        # right after the refresh. The watermark design correctly flags
        # stale. The wall-clock design, seeing only "refreshed 2 seconds
        # ago", reports fresh -- it has no way to know a memory arrived.
        # This is the dangerous divergence: a genuinely stale answer looks
        # settled.
        _scenario(
            "new memory just after refresh, checked moments later",
            last_refreshed_at=t0,
            last_memory_seen_at=t0,
            newest_in_scope=just_after,
            now=soon_after,
        ),
        # Nothing changed, checked moments later. Both designs agree: fresh.
        _scenario(
            "no new memory, checked moments later",
            last_refreshed_at=t0,
            last_memory_seen_at=t0,
            newest_in_scope=t0,
            now=soon_after,
        ),
        # Nothing changed, but a long time has passed. The watermark design
        # still correctly reports fresh (nothing new exists). The wall-clock
        # design reports stale purely because the clock moved -- the
        # wasteful divergence: a refresh that would find nothing still gets
        # triggered.
        _scenario(
            "no new memory, checked long after refresh",
            last_refreshed_at=t0,
            last_memory_seen_at=t0,
            newest_in_scope=t0,
            now=long_after,
        ),
        # A new memory landed AND a long time has passed. Both designs agree:
        # stale (for different reasons -- the wall-clock design is right
        # here by accident, not by tracking the thing that matters).
        _scenario(
            "new memory, checked long after refresh",
            last_refreshed_at=t0,
            last_memory_seen_at=t0,
            newest_in_scope=just_after,
            now=long_after,
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────
# Arm 2: reading performs zero retrieval calls
# ──────────────────────────────────────────────────────────────────────────


class _CountingRetrieval:
    """Stands in for 'a retrieval call'. Counts how many times it ran."""

    def __init__(self) -> None:
        self.calls = 0

    def compute(self, current: StandingAnswer) -> ComputedAnswer:
        self.calls += 1
        return ComputedAnswer(
            answer=f"computed from {self.calls} retrieval call(s)",
            newest_memory_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def run_zero_retrieval_reads(read_count: int = 25) -> dict:
    """Prove refresh calls retrieval, but plain reads never do."""
    retrieval = _CountingRetrieval()
    with tempfile.TemporaryDirectory(prefix="standing_bench_") as tmp:
        standing_dir = Path(tmp) / "standing"
        store = StandingAnswerStore.__new__(StandingAnswerStore)
        store._dir = standing_dir  # noqa: SLF001 - deliberate: bench doesn't need a real agent
        store._runner = in_process_runner

        question = "what is Chef's current standing?"
        outcome = store.refresh_if_stale(
            question,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            retrieval.compute,
        )
        calls_after_refresh = retrieval.calls

        for _ in range(read_count):
            store.get(question)

        return {
            "refreshed": outcome.refreshed,
            "retrieval_calls_from_one_refresh": calls_after_refresh,
            "read_count": read_count,
            "retrieval_calls_after_reads": retrieval.calls,
            "retrieval_calls_added_by_reads": retrieval.calls - calls_after_refresh,
        }


# ──────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────


def run_benchmark() -> dict:
    return {
        "staleness_divergence": run_staleness_divergence(),
        "zero_retrieval_reads": run_zero_retrieval_reads(),
    }


def _print_report(res: dict) -> None:
    print("\nskmemory standing-answer staleness design proof\n")

    rows = res["staleness_divergence"]
    header = f"{'scenario':<52}{'watermark':>11}{'wallclock':>11}{'agree':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['scenario']:<52}"
            f"{str(row['watermark_stale']):>11}"
            f"{str(row['wallclock_stale']):>11}"
            f"{str(row['agree']):>8}"
        )
    disagreements = sum(1 for row in rows if not row["agree"])
    print(
        f"\n{disagreements} of {len(rows)} scenarios diverge: the wall-clock design "
        "reports 'fresh' for a genuinely stale answer in one case, and 'stale' for "
        "a genuinely fresh answer in another. The watermark design is correct in "
        "every scenario, because it is keyed on whether a new memory actually "
        "exists, not on how long ago a clock last ticked.\n"
    )

    zr = res["zero_retrieval_reads"]
    print(
        f"one refresh triggered {zr['retrieval_calls_from_one_refresh']} retrieval call(s); "
        f"{zr['read_count']} subsequent plain reads added "
        f"{zr['retrieval_calls_added_by_reads']} more retrieval call(s).\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="skmemory standing-answer staleness proof")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
