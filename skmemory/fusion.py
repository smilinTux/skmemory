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
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
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

#: ``disabled`` is a CONFIGURED-off arm (it never ran, on purpose) and ``timeout``
#: is an arm that did not answer before the search deadline. Neither is "empty":
#: a reader of the report must be able to tell "had nothing" from "was not
#: asked" from "was too slow".
ArmStatus = Literal["ok", "abstained", "unavailable", "failed", "disabled", "timeout"]

#: Pinned head of the default fusion: this many top lexical hits go first.
DEFAULT_PIN = 3
#: Lexical arms in pin-preference order. The first one that contributed is the
#: pinned arm (the ParadeDB BM25 arm when skmem-pg is attached, else local FTS).
LEXICAL_ARMS = ("lexical", "lexical_local")
#: Default total wall-clock budget for all arms of one search.
DEFAULT_DEADLINE_S = 4.0

STRATEGY_ENV = "SKMEMORY_FUSION_STRATEGY"
DEADLINE_ENV = "SKMEMORY_SEARCH_DEADLINE_S"


def fusion_strategy() -> str:
    """``pinned_rrf`` (default) or the previous ``interleave`` / ``rrf`` by env."""
    raw = os.environ.get(STRATEGY_ENV, "").strip().casefold().replace("-", "_")
    if raw in ("interleave", "rank_interleave", "old", "legacy"):
        return "interleave"
    if raw in ("rrf", "plain_rrf"):
        return "rrf"
    return "pinned_rrf"


def fusion_pin() -> int:
    """How many lexical hits the default fusion pins (``SKMEMORY_FUSION_PIN``, default 3)."""
    raw = os.environ.get("SKMEMORY_FUSION_PIN", "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_PIN
    except ValueError:
        logger.warning("ignoring non-integer SKMEMORY_FUSION_PIN=%r", raw)
        return DEFAULT_PIN


#: Identifier route head: lexical_local's (FTS5) own order is recall@5 0.899 /
#: MRR 0.820 live against 0.692 for pg BM25, so it is pinned FIRST even when
#: skmem-pg is attached (SKMEM-P1-09). Depth 3: depth 4 and 5 drop class_bench
#: semantic recall@5 0.75 -> 0.625 (q-semantic-08), the offline gate.
IDENTIFIER_PIN = 3
IDENTIFIER_PIN_ARMS = ("lexical_local", "lexical")

ROUTE_ENV = "SKMEMORY_FUSION_ROUTE"
VECTOR_PIN_ENV = "SKMEMORY_FUSION_VECTOR_PIN"
#: Natural route head (SKMEM-P1-09, second change): vector top 4 pinned, then ONE
#: slot for the best lexical hit the vector head does not already hold. Live
#: paraphrase set (182 queries, 180 natural): vector alone recall@5 0.198, the
#: depth-3 fused candidate 0.176, because the fill arms' champions took vector
#: ranks 4 and 5. Depth 4 plus a lexical slot keeps vector@4 whole in the top 5
#: and still lets lexical help where vector misses. Natural route only: the
#: identifier route is untouched (depth 4-5 there breaks q-semantic-08).
DEFAULT_VECTOR_PIN = 4
NATURAL_LEXICAL_SLOT_ARMS = ("lexical_local", "lexical")
#: A query token whose lexical-index document frequency is in ``1..RARE_DF_MAX``
#: is rare enough that an exact lexical hit is likely the answer.
RARE_DF_MAX = 3
VECTOR_ARM = "semantic"


def fusion_route_mode() -> str:
    """``auto`` (default), ``lexical`` or ``vector`` (``SKMEMORY_FUSION_ROUTE``)."""
    raw = os.environ.get(ROUTE_ENV, "auto").strip().casefold()
    if raw in ("lexical", "vector"):
        return raw
    if raw not in ("", "auto"):
        logger.warning("ignoring unknown %s=%r", ROUTE_ENV, raw)
    return "auto"


def fusion_vector_pin() -> int:
    """How many vector hits a natural-language route pins (default 4)."""
    raw = os.environ.get(VECTOR_PIN_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_VECTOR_PIN
    except ValueError:
        logger.warning("ignoring non-integer %s=%r", VECTOR_PIN_ENV, raw)
        return DEFAULT_VECTOR_PIN


_ROUTE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._\-]*", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$")
_DATE_TOKEN_RE = re.compile(r"^(?:20\d{2}|20\d{2}-\d{2}(?:-\d{2})?|\d{1,2}(?:st|nd|rd|th)?)$")
_QUESTION_LEADS = frozenset(
    [
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "did",
        "do",
        "does",
        "is",
        "are",
        "was",
        "were",
        "can",
        "could",
        "should",
        "would",
        "will",
        "have",
        "has",
        "had",
        "tell",
        "explain",
        "describe",
        "remind",
    ]
)
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "about",
        "from",
        "by",
        "and",
        "or",
        "but",
        "if",
        "then",
        "so",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "his",
        "her",
        "their",
        "be",
        "been",
        "being",
        "am",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "not",
        "no",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "who",
        "whom",
        "can",
        "could",
        "should",
        "would",
        "will",
        "just",
        "any",
        "some",
    ]
)


@dataclass(frozen=True)
class RouteDecision:
    """Which head the fusion pins, and the shape rule that decided it."""

    cls: Literal["identifier", "natural"]
    reason: str


def _route_tokens(query: str) -> list[str]:
    return [t.strip("._-").casefold() for t in _ROUTE_TOKEN_RE.findall(query) if t.strip("._-")]


def _strong_identifier(tok: str) -> bool:
    if _DATE_TOKEN_RE.match(tok):
        return False
    return (
        any(c.isdigit() for c in tok)
        or "_" in tok
        or bool(_HOST_RE.match(tok))
        or ("-" in tok and any(c.isdigit() for c in tok))
    )


def route_query(
    query: str,
    df: Callable[[str], int] | None = None,
    *,
    mode: str | None = None,
) -> RouteDecision:
    """Classify ``query`` as identifier-ish or natural-language. No LLM, deterministic.

    ``identifier`` keeps the lexical pin; ``natural`` pins the vector head.
    Rules, in order (shape only, plus an optional corpus document frequency
    ``df(token) -> int`` from the lexical index):

    1. a token with a digit, an underscore or a host shape (``a.b.c``), dates
       excluded -> identifier;
    2. a token that is rare in the corpus (``1 <= df <= RARE_DF_MAX``) -> identifier;
    3. a question lead word or a trailing ``?`` -> natural;
    4. at least four tokens with stopwords making up a third or more -> natural;
    5. otherwise a keyword-style query -> identifier (the long-standing default).
    ``mode`` (default ``SKMEMORY_FUSION_ROUTE``) of ``lexical`` or ``vector`` forces it.
    """
    mode = mode or fusion_route_mode()
    if mode == "lexical":
        return RouteDecision("identifier", "forced")
    if mode == "vector":
        return RouteDecision("natural", "forced")
    toks = _route_tokens(query)
    if not toks:
        return RouteDecision("identifier", "empty")
    for tok in toks:
        if _strong_identifier(tok):
            return RouteDecision("identifier", "id-shape")
    content = [t for t in toks if t not in _STOPWORDS]
    if df is not None:
        for tok in content[:8]:
            try:
                n = df(tok)
            except Exception:  # noqa: BLE001 - df is advisory, never fatal
                n = 0
            if 1 <= n <= RARE_DF_MAX:
                return RouteDecision("identifier", "rare-token")
    if toks[0] in _QUESTION_LEADS or query.rstrip().endswith("?"):
        return RouteDecision("natural", "question")
    stop = sum(1 for t in toks if t in _STOPWORDS)
    if len(toks) >= 4 and stop / len(toks) >= 1 / 3:
        return RouteDecision("natural", "stopwords")
    return RouteDecision("identifier", "keywords")


def search_deadline_s() -> float:
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_DEADLINE_S
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", DEADLINE_ENV, raw)
        value = DEFAULT_DEADLINE_S
    return value if value > 0 else DEFAULT_DEADLINE_S


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
    #: wall-clock the arm took (0.0 when it never ran or the time is unknown)
    elapsed_s: float = 0.0

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
    #: wall-clock the arms took and the deadline they were given (0 when unknown)
    elapsed_s: float = 0.0
    deadline_s: float = 0.0
    #: route class ("identifier" / "natural") the fusion pinned by, None when unrouted
    route: str | None = None
    #: index of the embed endpoint that answered the semantic arm (never its URL)
    embed_ep: int | None = None
    #: index of the embed endpoint in flight when the semantic arm timed out
    embed_ep_inflight: int | None = None

    @property
    def contributing_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.contributed]

    @property
    def failed_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.status == "failed"]

    @property
    def timed_out_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.status == "timeout"]

    @property
    def all_attempted_timed_out(self) -> bool:
        """Every arm that was supposed to run timed out (none answered, none failed)."""
        attempted = [a for a in self.arms if a.status not in ("disabled", "unavailable")]
        return bool(attempted) and all(a.status == "timeout" for a in attempted)

    @property
    def disabled_arms(self) -> list[str]:
        return [a.name for a in self.arms if a.status == "disabled"]

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
    disabled: str | None = None,
) -> ArmOutcome:
    """Execute one arm and classify the outcome instead of swallowing it.

    ``fn`` of ``None`` means the backing store is not attached (``unavailable``),
    an empty return means the arm ran and had nothing to say (``abstained``),
    and a raise is caught, logged, and recorded (``failed``) so one dead backend
    cannot take down the whole search. The caller can tell all three apart.
    ``disabled`` (a reason string) marks an arm switched off by configuration:
    it is not called and is reported as ``disabled``, never as dead or empty.
    """
    if disabled is not None:
        return ArmOutcome(name=name, status="disabled", detail=disabled)
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


def pinned_rrf(
    arm_results: dict[str, Sequence[str]],
    k: int = RRF_K,
    *,
    pin: int | None = None,
    pin_arms: Sequence[str] = LEXICAL_ARMS,
    slot_arms: Sequence[str] = (),
) -> list[tuple[str, float, dict[str, int]]]:
    """Pin the lexical arm's top ``pin`` hits, then fill (interleave by default, RRF by env).

    Why: with :func:`rank_interleave` every arm's champion takes a head slot, so
    on a self-retrieval run the lexical rank 2-5 hit was pushed past rank 5 by
    the top picks of the vector, graph and temporal arms (20 of 20 traced
    cases, recall@5 0.601 fused against 0.729 for BM25 alone). Pinning keeps
    the strongest single arm's head intact; the RRF fill still lets the other
    arms add what lexical missed and lets agreement order the rest.

    The pinned arm is the first of ``pin_arms`` that contributed. When none did
    this is plain RRF.

    ``slot_arms`` (natural route) reserves ONE slot straight after the pinned
    head for the first hit of the first of those arms that contributed and is
    not already in the head.
    """
    if pin is None:
        pin = fusion_pin()
    fused = reciprocal_rank_fusion(arm_results, k)
    # Fill after the pinned head by rank-depth interleaving, which protects every
    # other arm's champion. Chosen on the live bench (recall@5 0.737 vs 0.702 for
    # RRF fill, p95 881 ms vs 1211 ms). ``SKMEMORY_FUSION_FILL=rrf`` selects RRF fill.
    if os.environ.get("SKMEMORY_FUSION_FILL", "interleave").strip().casefold() != "rrf":
        fused = rank_interleave(arm_results, k)
    by_id = {doc_id: (score, prov) for doc_id, score, prov in fused}
    head: list[str] = []
    for name in pin_arms:
        ids = arm_results.get(name)
        if ids:
            head = list(ids[:pin])
            break
    for name in slot_arms:
        extra = next((d for d in arm_results.get(name) or () if d not in head), None)
        if extra is not None:
            head.append(extra)
            break
    pinned = set(head)
    ordered = head + [doc_id for doc_id, _, _ in fused if doc_id not in pinned]
    return [(d, by_id[d][0], by_id[d][1]) for d in ordered]


class ArmGuard:
    """Keeps one hung backend from consuming the shared worker pool.

    * At most ``max_inflight`` calls of one arm may be running at once. Python
      cannot kill a thread, so a hung arm keeps its worker until the backend
      gives up; without a cap every search would park another one.
    * After a timeout the arm is "cooling down" for ``cooldown_s``: it is not
      called, and is reported as ``timeout`` saying so, so searches stay fast
      while the backend is down. A call that finishes normally clears it.
    """

    def __init__(self, max_inflight: int | None = None, cooldown_s: float | None = None):
        self.max_inflight = (
            max_inflight if max_inflight is not None else _env_int("SKMEMORY_ARM_MAX_INFLIGHT", 2)
        )
        self.cooldown_s = (
            cooldown_s if cooldown_s is not None else _env_float("SKMEMORY_ARM_COOLDOWN_S", 10.0)
        )
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}
        self._down_until: dict[str, float] = {}

    def admit(self, name: str) -> str | None:
        """None to run, else the reason the arm is being skipped."""
        with self._lock:
            left = self._down_until.get(name, 0.0) - time.monotonic()
            if left > 0:
                return f"cooling down after a timeout ({left:.1f}s left)"
            if self._inflight.get(name, 0) >= self.max_inflight:
                return f"{self._inflight[name]} earlier call(s) still running (cap {self.max_inflight})"
            self._inflight[name] = self._inflight.get(name, 0) + 1
            return None

    def finished(self, name: str, *, completed: bool = True) -> None:
        with self._lock:
            self._inflight[name] = max(0, self._inflight.get(name, 0) - 1)
            if completed:
                self._down_until.pop(name, None)

    def timed_out(self, name: str) -> None:
        with self._lock:
            self._down_until[name] = time.monotonic() + self.cooldown_s


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _timed_arm(name, fn, cap) -> ArmOutcome:
    t0 = time.monotonic()
    return replace(run_arm(name, fn, cap=cap), elapsed_s=time.monotonic() - t0)


def run_arms_concurrent(
    specs: Sequence[tuple[str, Callable[[], Sequence[str]] | None, str | None]],
    *,
    deadline_s: float,
    cap: int = DEFAULT_ARM_CAP,
    executor: ThreadPoolExecutor,
    guard: ArmGuard | None = None,
) -> list[ArmOutcome]:
    """Run every arm at once and wait at most ``deadline_s`` TOTAL.

    ``specs`` is ``(name, fn, disabled_reason)`` in the order the outcomes are
    returned. A disabled or unattached arm is classified without a thread. An
    arm still running at the deadline is reported as ``timeout`` (with how long
    it was given), NOT as ``abstained``: the ranking was produced without it
    and the report says so. The abandoned thread is left to finish on its own
    (Python cannot kill it); its result is discarded.
    """
    outcomes: dict[str, ArmOutcome] = {}
    futures = {}
    started = time.monotonic()
    for name, fn, disabled in specs:
        if disabled is not None or fn is None:
            outcomes[name] = run_arm(name, fn, cap=cap, disabled=disabled)
            continue
        if guard is not None:
            refused = guard.admit(name)
            if refused is not None:
                outcomes[name] = ArmOutcome(name=name, status="timeout", detail=refused)
                continue

            def call(name=name, fn=fn):
                try:
                    return _timed_arm(name, fn, cap)
                finally:
                    guard.finished(name)

            futures[name] = executor.submit(call)
        else:
            futures[name] = executor.submit(_timed_arm, name, fn, cap)
    if futures:
        wait(list(futures.values()), timeout=deadline_s)
    for name, fut in futures.items():
        if fut.done():
            outcomes[name] = fut.result()
        else:
            was_queued = fut.cancel()  # True: it never started, so it will never release
            if guard is not None:
                if was_queued:
                    guard.finished(name, completed=False)
                guard.timed_out(name)
            outcomes[name] = ArmOutcome(
                name=name,
                status="timeout",
                detail=f"no answer within {deadline_s:.2f}s deadline "
                f"({time.monotonic() - started:.2f}s elapsed)",
                elapsed_s=time.monotonic() - started,
            )
            logger.warning("retrieval arm %r timed out after %.2fs", name, deadline_s)
    return [outcomes[name] for name, _, _ in specs]


_POOL: ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def arm_executor() -> ThreadPoolExecutor:
    """Process-wide arm pool, so per-thread DB connections are reused across searches."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="skmem-arm")
        return _POOL


def fuse(
    arms: Sequence[ArmOutcome],
    *,
    limit: int = 10,
    k: int = RRF_K,
    strategy: str = "interleave",
    pin_arms: Sequence[str] = LEXICAL_ARMS,
    route: RouteDecision | None = None,
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

    pin: int | None = None
    slot_arms: Sequence[str] = ()
    if (
        route is not None
        and route.cls == "identifier"
        and any(n in contributing for n in IDENTIFIER_PIN_ARMS)
    ):
        # Identifier route: rank like the best lexical arm, whichever pg
        # BM25 would otherwise be preferred by the caller's pin order.
        pin_arms = IDENTIFIER_PIN_ARMS
        pin = max(fusion_pin(), IDENTIFIER_PIN)
    if route is not None and route.cls == "natural" and VECTOR_ARM in contributing:
        # Natural-language route: pin the vector head. When the vector arm did
        # not contribute the lexical pin stands, so a dead vector arm degrades
        # to the previous behaviour rather than to no pin.
        pin_arms = (VECTOR_ARM,)
        pin = fusion_vector_pin()
        slot_arms = NATURAL_LEXICAL_SLOT_ARMS

    if strategy == "pinned_rrf":

        def merge(results, kk):
            return pinned_rrf(results, kk, pin=pin, pin_arms=pin_arms, slot_arms=slot_arms)

    elif strategy == "interleave":
        merge = rank_interleave
    else:
        merge = reciprocal_rank_fusion
    ranked = merge(contributing, k)
    return FusionResult(
        ids=[doc_id for doc_id, _, _ in ranked[:limit]],
        arms=list(arms),
        provenance={doc_id: prov for doc_id, _, prov in ranked[:limit]},
        route=route.cls if route else None,
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

_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_ISO_MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_WORD_RE = re.compile(r"[a-z]+")

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUM = r"(\d{1,3}|" + "|".join(_NUM_WORDS) + ")"
_UNIT = r"(day|week|month)s?"
_AGO_RE = re.compile(rf"\b{_NUM}\s+{_UNIT}\s+ago\b")
_LAST_N_RE = re.compile(rf"\b(?:in\s+the\s+|over\s+the\s+)?(?:last|past)\s+{_NUM}\s+{_UNIT}\b")
_LAST_WEEKDAY_RE = re.compile(r"\blast\s+(" + "|".join(_WEEKDAYS) + r")\b")

# Month words that are also ordinary English. They only count as a month when
# the query makes the intent unambiguous (see _month_match).
_AMBIGUOUS_MONTHS = {"may", "march"}
_MONTH_PREPS = r"(?:in|during|since|until|from|of)"
# A bare capitalized "March" at query start is a month only with a date cue
# (day or year) or when the next word is an event noun ("March outage").
_MARCH_EVENT_NOUNS = {
    "outage",
    "incident",
    "review",
    "report",
    "deploy",
    "meeting",
    "notes",
    "update",
}
_MODAL_SUBJECTS = {"i", "we", "you", "they", "it", "he", "she", "people", "who"}
_MODAL_FOLLOWERS = {
    "times", "time", "miles", "mile", "minutes", "minute", "hours", "hour",
    "days", "day", "weeks", "week", "go", "be",
}  # fmt: skip
_PAST_UNIT_RE = re.compile(
    r"\bpast\s+(day|week|month)\b"
    r"(?=\s*(?:[.,;:!?]|$)|\s+(?:or|and|to|on|in|for|about|with|but|from|we|i)\b)"
)
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _span(start: datetime, end_exclusive: datetime) -> tuple[str, str]:
    """Half-open local span -> inclusive ``(after, before)`` UTC ISO strings."""
    return _utc_z(start), _utc_z(end_exclusive - timedelta(microseconds=1))


def _local_midnight(day, tz) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _day_span(day, tz) -> tuple[str, str]:
    start = _local_midnight(day, tz)
    return _span(start, _local_midnight(day + timedelta(days=1), tz))


def _week_span(day, tz) -> tuple[str, str]:
    """ISO week (Monday 00:00 to Sunday 24:00, local) containing *day*."""
    monday = day - timedelta(days=day.weekday())
    return _span(_local_midnight(monday, tz), _local_midnight(monday + timedelta(days=7), tz))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_bounds(year: int, month: int, tz=timezone.utc) -> tuple[str, str]:
    ny, nm = _shift_month(year, month, 1)
    return _span(datetime(year, month, 1, tzinfo=tz), datetime(ny, nm, 1, tzinfo=tz))


def _count(token: str) -> int:
    return int(token) if token.isdigit() else _NUM_WORDS[token]


def _month_match(query: str, text: str):
    """Find a month reference. Returns ``(month, day_or_None, year_or_None)``.

    Unambiguous names (june, oct, ...) match as whole words in any case.
    "may" and "march" are also ordinary English ("the rollout may halt"), so
    they count only when capitalized (not as the first word, where "May we"
    is a modal), directly attached to a day or year number, or preceded by a
    month preposition ("in may").
    """
    for m in _WORD_RE.finditer(text):
        name = m.group(0)
        month = _MONTHS.get(name)
        if month is None:
            continue
        a, b = m.start(), m.end()
        # Whole-word only: skip if flanked by letters is already guaranteed by
        # _WORD_RE, but digits-adjacent like "5may" are fine.
        before, after = text[:a], text[b:]
        day = None
        d = re.match(rf"\s*,?\s*{_DAY}\b(?!\s*:)", after)
        d2 = re.search(rf"{_DAY}\s+(?:of\s+)?$", before)
        if d and not re.match(r"\s*,?\s*20\d{2}\b", after):
            if name in _AMBIGUOUS_MONTHS:
                prev = re.search(r"(\w+)\s+$", before)
                nxt = re.match(r"\s*([a-z]+)", after[d.end() :])
                if (prev and prev.group(1) in _MODAL_SUBJECTS) or (
                    nxt and nxt.group(1) in _MODAL_FOLLOWERS
                ):
                    continue  # "we may 3 times", "people march 4 hours"
            day = int(d.group(1))
        elif d2:
            # "step 3 may fail": a bare number before an ambiguous word is not
            # a day unless the date reading is closed off ("5 may", "5 may
            # 2026", "5th of may", "5 may,").
            date_only = re.fullmatch(r"[\s.,!?]*\d{1,2}\s+[a-z]+[\s.,!?]*", text)
            year_next = re.match(r"\s*,?\s*20\d{2}\b", after)
            ordinal = re.search(r"\d(?:st|nd|rd|th)\s+(?:of\s+)?$", before)
            if name not in _AMBIGUOUS_MONTHS or date_only or year_next or ordinal:
                day = int(d2.group(1))
        y = re.match(r"\s*,?\s*(20\d{2})\b", after)
        has_number = day is not None or y is not None
        if name in _AMBIGUOUS_MONTHS and not has_number:
            at_start = query[:a].strip() == ""
            capitalized = query[a : a + 1].isupper() and not at_start
            if name == "march" and at_start and query[a : a + 1].isupper():
                nxt = _WORD_RE.match(text, b + 1) if text[b : b + 1] == " " else None
                capitalized = nxt is not None and nxt.group(0).rstrip("s") in _MARCH_EVENT_NOUNS
            prep = re.search(rf"\b{_MONTH_PREPS}\s+$", before)
            if not (capitalized or prep):
                continue
        return month, day, int(y.group(1)) if y else None
    return None


# A calendar date (ISO day or month, a month name, a bare year) is only TIME
# INTENT when something in the query asks about that time: a time preposition
# directly before it ("what did we decide in june", "since 2025-10", "on may 5").
# The same tokens inside a title ("Postmortem 2026-10-16 sqlite orphans",
# "Q3 2026 roadmap", "June 5 notes") are part of the thing being named, and
# firing the temporal arm on them listed the wrong month's memories into the
# fused pool: measured on a live self-retrieval run the arm fired on 125 of 397
# title-derived queries, none of which asked about a time.
_DATE_EXPR = (
    r"(?:20\d{2}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?|20\d{2}|"
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\b"
)
_DATE_CUE_RE = re.compile(
    r"\b(?:in|during|since|until|till|before|after|from|on|by|around|throughout|"
    r"between|as of|back in|early|late|mid)[\s-]+(?:the\s+)?(?:(?:month|year)\s+of\s+)?"
    r"(?:(?:early|late|mid)[\s-]+)?" + _DATE_EXPR
)
# "October 2025": a month directly followed by a year is a period, not a title word.
_MONTH_YEAR_RE = re.compile(
    rf"\b(?:{'|'.join(sorted(_MONTHS, key=len, reverse=True))})\s+20\d{{2}}\b"
)
# A capitalized, unambiguous month that is not the first word ("the June outage").
_CAP_MONTH_RE = re.compile(
    r"(?<!^)(?<![.!?]\s)\b(?:January|February|April|June|July|August|September|October|"
    r"November|December|Jan|Feb|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\b"
)


# "week of Oct 5", "end of June": "of" counts before a MONTH, never before a bare
# year ("Top 10 of 2026").
_OF_MONTH_RE = re.compile(
    rf"\bof\s+(?:the\s+)?(?:{'|'.join(sorted(_MONTHS, key=len, reverse=True))})\b"
)


def _has_date_cue(query: str, text: str) -> bool:
    if _DATE_CUE_RE.search(text) or _MONTH_YEAR_RE.search(text) or _OF_MONTH_RE.search(text):
        return True
    if _QUESTION_RE.search(text) and _MONTH_DAY_RE.search(text):
        return True
    return _CAP_MONTH_RE.search(query.strip()) is not None


# "what did we decide june 5": no preposition, but a question word plus a month
# with a day number is unmistakably a date question. A title like "June 5 notes"
# has no question word and stays a title.
_QUESTION_RE = re.compile(
    r"\b(?:what|when|which|who|how|why|did|do|does|was|were|is|are|show|list|find|recall|"
    r"remind|tell)\b"
)
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DAY_RE = re.compile(
    rf"\b(?:(?:{_MONTH_NAMES})\s+\d{{1,2}}(?:st|nd|rd|th)?|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES}))\b"
)


def has_time_intent(query: str, *, now: datetime | None = None) -> bool:
    """True when *query* asks about a time rather than merely containing a date token."""
    return parse_time_window(query, now=now, strict=True) is not None


def parse_time_window(
    query: str, *, now: datetime | None = None, strict: bool = False
) -> tuple[str, str] | None:
    """Extract an explicit time window from *query*, or ``None``.

    Returns ``(created_after, created_before)`` as inclusive ISO 8601 UTC
    strings, ready to hand to ``list_memories()``.

    Time zone: every calendar boundary (today, yesterday, weekday, week,
    month) is computed in the time zone of the ``now`` argument, then
    converted to UTC for the output. A naive ``now`` is taken as UTC. The wall
    clock is only consulted when ``now`` is omitted. Weeks run Monday 00:00 to
    Sunday 24:00 (ISO).

    Recognised, in precedence order:

    * ``YYYY-MM-DD`` (that day), ``YYYY-MM`` (that month)
    * ``yesterday``, ``today``
    * ``N days/weeks/months ago``: that calendar day, ISO week, or calendar
      month (``N`` is a number or one..twelve / a / an)
    * ``last/past N days/weeks/months``: rolling window from ``now - N units``
      up to ``now``
    * ``last <weekday>``: the most recent such day strictly before today
    * ``last week`` / ``this week`` (ISO week), ``last month`` / ``this month``
    * a named month, with an optional day (``may 5``) and year; with no year,
      the most recent occurrence not in the future. "may" and "march" need a
      capital letter, a day or year number, or a preposition ("in may"), so
      "the rollout may halt" is not a date.
    * a bare year

    ``strict=True`` (what the temporal ARM uses) keeps every relative phrase
    (yesterday, N ago, last/past N, this/last week or month) but accepts the
    calendar forms (ISO date or month, a month name, a bare year) only with a
    time preposition directly before them, see :data:`_DATE_CUE_RE`.

    Anything else returns ``None`` so the temporal arm ABSTAINS rather than
    guessing. An arm that always has an opinion adds noise to every fusion,
    and a recency sort masquerading as a date filter is how ``synthesize_*``
    silently returned empty narratives for historical dates (postmortem
    §3.11).
    """
    if not query:
        return None
    text = query.casefold()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz = now.tzinfo
    today = now.date()

    # Calendar forms need a cue under strict; relative phrases never do.
    dated_ok = not strict or _has_date_cue(query, text)

    iso_day = _ISO_DATE_RE.search(text) if dated_ok else None
    if iso_day:
        y, mo, d = (int(g) for g in iso_day.groups())
        try:
            return _day_span(today.replace(year=y, month=mo, day=d), tz)
        except ValueError:
            return None
    iso = _ISO_MONTH_RE.search(text) if dated_ok else None
    if iso:
        return _month_bounds(int(iso.group(1)), int(iso.group(2)), tz)

    if re.search(r"\byesterday\b", text):
        return _day_span(today - timedelta(days=1), tz)
    if re.search(r"\btoday\b", text):
        return _day_span(today, tz)

    m = _AGO_RE.search(text)
    if m:
        n, unit = _count(m.group(1)), m.group(2)
        if unit == "day":
            return _day_span(today - timedelta(days=n), tz)
        if unit == "week":
            return _week_span(today - timedelta(days=7 * n), tz)
        y, mo = _shift_month(today.year, today.month, -n)
        return _month_bounds(y, mo, tz)

    m = _LAST_N_RE.search(text)
    pm = None if m else _PAST_UNIT_RE.search(text)  # "the past month" == last 1 month
    if m or pm:
        n, unit = (_count(m.group(1)), m.group(2)) if m else (1, pm.group(1))
        if unit == "month":
            y, mo = _shift_month(today.year, today.month, -n)
            try:
                start = now.replace(year=y, month=mo)
            except ValueError:  # e.g. Mar 31 minus one month: clamp to month end
                ny, nm = _shift_month(y, mo, 1)
                start = now.replace(year=ny, month=nm, day=1) - timedelta(days=1)
                start = start.replace(hour=now.hour, minute=now.minute, second=now.second)
        else:
            start = now - timedelta(days=n * (7 if unit == "week" else 1))
        return _utc_z(start), _utc_z(now)

    m = _LAST_WEEKDAY_RE.search(text)
    if m:
        back = (today.weekday() - _WEEKDAYS[m.group(1)]) % 7 or 7
        return _day_span(today - timedelta(days=back), tz)

    if re.search(r"\bthis\s+week\b(?!\s+of\b)", text):
        return _week_span(today, tz)
    if re.search(r"\blast\s+week\b(?!\s+of\b)", text):
        return _week_span(today - timedelta(days=7), tz)
    if re.search(r"\bthis\s+month\b", text):
        return _month_bounds(today.year, today.month, tz)
    if re.search(r"\blast\s+month\b", text):
        y, mo = _shift_month(today.year, today.month, -1)
        return _month_bounds(y, mo, tz)

    if not dated_ok:
        return None

    hit = _month_match(query, text)
    year_match = _YEAR_RE.search(text)
    if hit is not None:
        month, day, year = hit
        if year is None:
            # No year given: the most recent occurrence that is not in the future.
            year = (
                int(year_match.group(1))
                if year_match
                else (today.year if month <= today.month else today.year - 1)
            )
        if day is not None:
            try:
                return _day_span(today.replace(year=year, month=month, day=day), tz)
            except ValueError:
                pass
        return _month_bounds(year, month, tz)

    if year_match:
        year = int(year_match.group(1))
        return _span(datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz))

    return None
