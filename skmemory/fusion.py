"""Multi-arm retrieval fusion for :meth:`MemoryStore.search`.

What this replaces
------------------
``MemoryStore.search()`` has been a FALLBACK CHAIN, not a fused search::

    if self.vector:
        results = self.vector.search_text(...)
        if results:
            return results
    return self.primary.search_text(...)

and ``PGVectorBackend.search_text()`` is a second fallback chain nested inside
the first, by its own docstring: "mxbai semantic vector (cosine) ... falling
back to BM25 full-text then ILIKE when the query can't be embedded or vector
returns nothing".

The consequence is structural, not hypothetical: the lexical leg is consulted
only when the dense leg returns an EMPTY list. When the dense leg returns
something confident and wrong, which is the normal failure mode for a query
carrying a rare identifier (``prb-6f069c5e``, a hostname, an error code), BM25
holds the exact match at rank 1 and is never asked. Measured on
``skmemory.eval.fusion_bench``, the chain scores recall@5 of 0.000 on that
query class.

That ``if results:`` is also the house's most-repeated defect, the one ranked
three times in ``docs/postmortems/2026-10-16-sqlite-only-orphans.md``: an empty
result is swallowed and turned into a different code path, so a caller cannot
tell "the dense arm found nothing" from "the dense arm was never reachable".
Here, every arm reports its own outcome and nothing is swallowed.

The design
----------
Four arms run independently, each capped before the merge, and their ranked
outputs are fused by rank-depth interleaving with Reciprocal Rank Fusion as the
within-depth tiebreak (:func:`rank_interleave`).

Plain RRF is deliberately NOT the final ranker. Measured on the same harness,
equal-weight RRF regresses the paraphrase class from 0.833 to 0.000: a decoy
placing mid-pack in two arms outscores the true answer placing rank 1 in one
arm, which with four arms (two of which routinely abstain) is the common case
rather than an edge case. Interleaving by rank depth protects each arm's
champion while still letting multi-arm agreement win its depth.

Measured, recall@5 by query class (see ``skmemory.eval.fusion_bench``):

                      lexical  semantic  temporal  entity  OVERALL   MRR
    fallback (today)    0.000     0.833     0.500   0.000    0.333  0.302
    plain RRF           0.667     0.000     1.000   1.000    0.667  0.570
    rank interleave     0.667     0.833     1.000   1.000    0.875  0.662

No class regresses against the current behaviour.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

logger = logging.getLogger(__name__)

#: RRF constant from Cormack et al. Named, because it sets the usable dynamic
#: range of the fused score: across a 300-candidate window the score spans only
#: 1/61 to 1/360, a factor of under 6. Any later weighting larger than that
#: spread stops being a weight and silently turns the sort lexicographic. That
#: is not theoretical: Hindsight shipped a weighted-RRF boost of w=7, above the
#: spread, and measured recall@20 collapse from 0.97 to 0.40 because the boosted
#: arm then filled the entire reranker candidate budget. Boost in RANK space
#: (see R2), never by scaling this score.
RRF_K = 60

#: Per-arm candidate cap applied BEFORE the merge, so one over-expanding arm
#: (BM25 on a common term, a graph arm on a hub entity) cannot crowd the others
#: out of the merged pool.
DEFAULT_ARM_CAP = 25

ArmStatus = Literal["ok", "abstained", "unavailable", "failed"]


@dataclass(frozen=True)
class ArmOutcome:
    """What one arm actually did.

    The distinction between these four statuses is the whole point. The code
    being replaced could only express "truthy or falsy", so a backend that was
    never attached, an arm that legitimately had nothing to say, and an arm that
    raised were all indistinguishable at the call site, and all three silently
    became "use the other path".
    """

    name: str
    ids: tuple[str, ...] = ()
    status: ArmStatus = "ok"
    detail: str = ""

    @property
    def contributed(self) -> bool:
        return self.status == "ok" and bool(self.ids)


@dataclass
class FusionResult:
    """Fused ranking plus the provenance needed to explain it."""

    ids: list[str] = field(default_factory=list)
    arms: list[ArmOutcome] = field(default_factory=list)
    #: doc id -> {arm name: rank within that arm}
    provenance: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def contributing_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.contributed]

    @property
    def failed_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.status == "failed"]

    def summary(self) -> str:
        """One line fit for a log or a debug header."""
        parts = [f"{a.name}={a.status}:{len(a.ids)}" for a in self.arms]
        return f"fused {len(self.ids)} from [{', '.join(parts)}]"


def cap_per_source(ids: Sequence[str], cap: int) -> list[str]:
    """Truncate one arm's ranked output to its best ``cap`` entries."""
    if cap <= 0 or len(ids) <= cap:
        return list(ids)
    return list(ids[:cap])


def run_arm(
    name: str,
    fn: Callable[[], Sequence[str]] | None,
    *,
    cap: int = DEFAULT_ARM_CAP,
) -> ArmOutcome:
    """Execute one arm and classify the outcome instead of swallowing it.

    ``fn`` of ``None`` means the backing store is not attached (``unavailable``),
    an empty return means the arm ran and had nothing to say (``abstained``),
    and a raise is caught, logged, and recorded (``failed``) so one dead backend
    cannot take down the whole search. The caller can tell all three apart.
    """
    if fn is None:
        return ArmOutcome(name=name, status="unavailable", detail="backend not attached")
    try:
        ids = [str(i) for i in fn() if i]
    except Exception as exc:  # one dead arm must not kill the search
        logger.warning("retrieval arm %r failed: %s", name, exc)
        return ArmOutcome(name=name, status="failed", detail=str(exc))
    if not ids:
        return ArmOutcome(name=name, status="abstained")
    # De-duplicate within an arm while preserving its ranking.
    seen: set[str] = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    return ArmOutcome(name=name, ids=tuple(cap_per_source(ordered, cap)), status="ok")


def reciprocal_rank_fusion(
    arm_results: dict[str, Sequence[str]], k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """Classic RRF: ``score(d) = sum over arms of 1 / (k + rank(d))``, 1-based.

    Returns ``(doc_id, score, {arm: rank})`` best first, with a deterministic
    id tiebreak so the same inputs always produce the same order.
    """
    scores: dict[str, float] = {}
    provenance: dict[str, dict[str, int]] = {}
    for arm_name, ids in arm_results.items():
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            provenance.setdefault(doc_id, {})[arm_name] = rank
    out = [(doc_id, score, provenance[doc_id]) for doc_id, score in scores.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def rank_interleave(
    arm_results: dict[str, Sequence[str]], k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """RRF ordering with each arm's champion protected.

    Sorts by best-rank-in-any-single-arm first and RRF score second, so every
    arm's rank-1 candidate precedes every arm's rank-2 candidate, and RRF only
    decides order within a depth. An arm that alone found the answer still
    places it at the head; agreement across arms still wins its depth.

    See the module docstring for the measurement that motivated this over plain
    RRF (paraphrase recall@5: 0.000 with plain RRF, 0.833 with interleaving).
    """
    fused = reciprocal_rank_fusion(arm_results, k)
    scored = {doc_id: (score, prov) for doc_id, score, prov in fused}
    best_depth: dict[str, int] = {}
    for ids in arm_results.values():
        for rank, doc_id in enumerate(ids, start=1):
            if doc_id not in best_depth or rank < best_depth[doc_id]:
                best_depth[doc_id] = rank
    out = [(doc_id, scored[doc_id][0], scored[doc_id][1]) for doc_id in scored]
    out.sort(key=lambda t: (best_depth[t[0]], -t[1], t[0]))
    return out


def fuse(
    arms: Sequence[ArmOutcome],
    *,
    limit: int = 10,
    k: int = RRF_K,
    strategy: str = "interleave",
) -> FusionResult:
    """Merge executed arms into one ranking.

    Only arms with ``status == "ok"`` vote. An ``abstained`` arm is a real
    signal (one fewer voter), not a reason to re-run anything, and a ``failed``
    arm is carried through on the result so the caller can see that the ranking
    it got was produced with an arm missing.
    """
    contributing = {a.name: a.ids for a in arms if a.contributed}
    if not contributing:
        return FusionResult(ids=[], arms=list(arms), provenance={})

    merge = rank_interleave if strategy == "interleave" else reciprocal_rank_fusion
    ranked = merge(contributing, k)
    return FusionResult(
        ids=[doc_id for doc_id, _, _ in ranked[:limit]],
        arms=list(arms),
        provenance={doc_id: prov for doc_id, _, prov in ranked[:limit]},
    )


# =============================================================================
# TEMPORAL WINDOW PARSING
# =============================================================================
# The temporal arm exists because a date is a PREDICATE, not a word to match.
# "what did we decide in June" against a store holding one decision per month
# cannot be answered by text similarity: every candidate has near-identical
# text and the month often appears only in the timestamp. Measured on
# fusion_bench's temporal class, text-only arms score recall@5 0.500 (they are
# guessing among identical documents) against 1.000 once this predicate runs.

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ISO_MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_WORD_RE = re.compile(r"[a-z]+")


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    return (
        start.isoformat().replace("+00:00", "Z"),
        (end - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
    )


def parse_time_window(query: str, *, now: datetime | None = None) -> tuple[str, str] | None:
    """Extract an explicit time window from *query*, or ``None``.

    Returns ``(created_after, created_before)`` as ISO 8601 UTC strings, ready
    to hand to ``list_memories()``.

    Deliberately conservative: it recognises a named month (with an optional
    year, defaulting to the most recent occurrence not in the future), an
    ``YYYY-MM``, a bare year, and "today" / "yesterday". Anything else returns
    ``None`` so the temporal arm ABSTAINS rather than guessing. An arm that
    always has an opinion adds noise to every fusion, and a recency sort
    masquerading as a date filter is how ``synthesize_*`` silently returned
    empty narratives for historical dates (postmortem §3.11).
    """
    if not query:
        return None
    text = query.casefold()
    now = now or datetime.now(timezone.utc)

    iso = _ISO_MONTH_RE.search(text)
    if iso:
        return _month_bounds(int(iso.group(1)), int(iso.group(2)))

    if "yesterday" in text:
        day = (now - timedelta(days=1)).date()
    elif "today" in text:
        day = now.date()
    else:
        day = None
    if day is not None:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        return (
            start.isoformat().replace("+00:00", "Z"),
            end.isoformat().replace("+00:00", "Z"),
        )

    # Match month names on whole words only, so "march" matches but the "may"
    # inside "maybe" does not.
    words = set(_WORD_RE.findall(text))
    month = next((num for name, num in _MONTHS.items() if name in words), None)
    year_match = _YEAR_RE.search(text)

    if month is not None:
        if year_match:
            return _month_bounds(int(year_match.group(1)), month)
        # No year given: the most recent occurrence that is not in the future.
        year = now.year if month <= now.month else now.year - 1
        return _month_bounds(year, month)

    if year_match:
        year = int(year_match.group(1))
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)
        return (
            start.isoformat().replace("+00:00", "Z"),
            end.isoformat().replace("+00:00", "Z"),
        )

    return None
