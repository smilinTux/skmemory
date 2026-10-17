"""Cheap typed-decision triage before the expensive LLM (R5).

What this is for
-----------------
Every candidate memory that wants to be stored has to be weighed against
whatever skmemory already knows. Today that weighing either does not happen
(everything gets written) or happens by calling an LLM on every single
candidate, which is slow and not free. Most candidates do not need that: a
greeting is obviously chatter, a near-identical restatement is obviously a
restatement, and a brand-new fact with nothing like it in memory is obviously
new. Only a genuine correction, reversal, or status update needs an expensive
LLM rewrite to fold it into the existing memory correctly. This module is the
cheap gate in front of that expensive step, modelled on Hindsight's
(vectorize-io) consolidation triage and adapted to skmemory's own primitives.

The four outcomes
------------------
``restates``
    An existing memory already says this, even in other words. Nothing new
    should be stored; the existing memory is strengthened / gets this as
    corroborating evidence.
``new``
    The candidate says something no existing memory says. Store it as-is.
``changes``
    The candidate touches what an existing memory says: corrects it,
    reverses it, retires it, updates a count or status, or makes a vague
    part specific. This is the ONLY outcome that needs the expensive LLM
    rewrite, because folding an update into an existing memory correctly
    requires understanding both texts together.
``chatter``
    Passing noise (a greeting, a momentary state) with no lasting knowledge.
    Nothing should be stored.

Two tiers, and why the split is where it is
--------------------------------------------
The deterministic tier is the DEFAULT and is zero-dependency: regex plus a
hand-rolled token-overlap score, both standard library, no network, no model.
It is what runs unless an LLM tier is explicitly turned on. The LLM tier is
opt-in (``use_llm=True`` or ``SKMEMORY_TRIAGE_USE_LLM``) and routes through
skgateway's OpenAI-compatible endpoint (model ``sk-default``, an auto-router;
never a hardcoded vendor model). If the gateway cannot be reached, triage
falls back to the deterministic tier and the returned result says so in
``detail`` and in ``attempts`` instead of silently returning a plausible
looking verdict from a tier that never actually ran.

Agreement, not confidence
--------------------------
Hindsight tried per-answer confidence thresholds for this kind of
classification and abandoned them: a threshold tuned for one model has to be
retuned for the next one, so it is not really a fixed decision rule at all,
it is a hidden per-model parameter. The replacement used here is
cross-question agreement: every decision point in this module asks at least
two independently reasoned questions and only accepts a settled verdict when
they agree. When they disagree, the result is ``changes`` with
``status="abstained"``: the only outcome that forwards a candidate to the
expensive tier downstream, which is the safe place to put something nobody
inside this cheap tier was willing to commit to.

Typed results, never a bare string
------------------------------------
``skmemory/fusion.py`` already carries this house's answer to "what happens
when a step degrades": ``ArmOutcome`` reports ``ok`` / ``abstained`` /
``unavailable`` / ``failed`` plus a ``detail``, so a caller can tell "this
step had nothing to say" apart from "this step was never reachable" apart
from "this step raised". ``TierAttempt`` and ``TriageResult`` below copy that
shape for the same reason: this is the house's most-repeated defect class
(see ``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` §3.2, §3.3,
§3.11), where a degraded path produced output indistinguishable from a
healthy one. A ``TriageResult`` that silently fell back to the deterministic
tier must not look like one the LLM actually decided.

Reusing check_duplicate instead of re-deriving it
----------------------------------------------------
``MemoryStore.check_duplicate()`` already computes embedding-cosine
similarity against existing memories (threshold 0.73, tuned for
mxbai-embed-large) but only returns it as advice; nothing acts on it. This
module turns that into an actual routing signal: pass its return shape
(``[{"id", "content_preview", "similarity"}, ...]``) in as
``duplicate_matches`` and the best match's own similarity score is used
directly, at the SAME 0.73 ceiling ``check_duplicate`` already uses. No
embedding math is re-implemented here. When no vector backend is attached
(or the caller has none to offer), the deterministic tier falls back to a
stdlib-only lexical overlap score over the raw text of ``existing``, which is
what makes the default path genuinely zero-dependency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from skmemory.extractor import extract_memories

logger = logging.getLogger(__name__)

TriageVerdict = Literal["restates", "new", "changes", "chatter"]
TriageTier = Literal["deterministic", "llm"]
TriageStatus = Literal["ok", "abstained", "unavailable", "failed"]

#: skgateway's OpenAI-compatible endpoint. Never hardcode a vendor model:
#: "sk-default" is an auto-router, resolved on the gateway side.
DEFAULT_GATEWAY_URL = "http://localhost:18780/v1"
DEFAULT_GATEWAY_MODEL = "sk-default"
DEFAULT_GATEWAY_TIMEOUT = 20.0

#: check_duplicate()'s own tuned operating point (store.py, mxbai-embed-large):
#: near-duplicates measured 0.76-0.94, distinct content 0.27-0.70, gap
#: 0.703-0.763. Reused verbatim as the "this is a restatement" ceiling rather
#: than re-deriving a separate number, so a candidate that check_duplicate
#: would already flag as near-duplicate is treated consistently here.
EMBEDDING_RESTATE_CEILING = 0.73

#: Below this, a candidate is confidently about a different topic than the
#: best match. This is a DESIGN CHOICE, not a measured constant: it sits
#: inside check_duplicate's own documented "distinct content" band
#: (0.27-0.70) on purpose, biased low, so that anything with real topical
#: overlap lands in the 0.50-0.73 "related, not yet a restatement" band and
#: has to be settled by the change-cue signal instead of by score alone.
EMBEDDING_RELATED_FLOOR = 0.50

#: Lexical (token-Jaccard) equivalents of the two embedding thresholds above,
#: for the zero-dependency path (no vector backend attached, no
#: duplicate_matches supplied). A first draft of this used
#: difflib.SequenceMatcher's character-run ratio, which turned out to be a
#: poor proxy for TOPICAL relatedness: two sentences sharing nothing but
#: ordinary function words and letter sequences ("the", "is", "and", common
#: short substrings) scored 0.3-0.4 even when about completely unrelated
#: subjects, which would have forced the floor above several genuine
#: "changes" fixtures. Token-set Jaccard over content words (length >= 3,
#: stopwords dropped) separates the two classes cleanly on the fixture set
#: in skmemory.eval.triage_bench: unrelated pairs score exactly 0.0, genuine
#: related-but-different-claim pairs score 0.2-0.6, and close rewordings
#: score 0.85-1.0. These two values were picked from that gap, not tuned
#: against a live embedding distribution, and the bench reports the
#: resulting precision/recall honestly rather than asserting them as
#: universal constants.
LEXICAL_RESTATE_CEILING = 0.72
LEXICAL_RELATED_FLOOR = 0.15

_TRUE_ENV = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in _TRUE_ENV


# =============================================================================
# TYPED RESULTS
# =============================================================================


@dataclass(frozen=True)
class TierAttempt:
    """What one tier actually did, for a tier that was tried but is NOT the
    one whose verdict is reported in the final :class:`TriageResult`.

    Mirrors ``fusion.ArmOutcome``'s four-way status so a degraded tier is
    never indistinguishable from one that ran cleanly: ``unavailable`` means
    the tier was never even attempted (no gateway configured), ``failed``
    means it was attempted and raised or timed out, ``ok``/``abstained`` mean
    it ran and produced an answer (``verdict`` is set) that the caller chose
    not to use as final (only possible today for an LLM attempt kept for
    provenance after a later tier superseded it, which this module does not
    currently do, but the shape allows it without a redesign).
    """

    tier: TriageTier
    status: TriageStatus
    detail: str = ""
    verdict: TriageVerdict | None = None


@dataclass(frozen=True)
class TriageResult:
    """Typed outcome of triaging one candidate against its related memories.

    ``verdict`` is never handed back bare. ``tier`` and ``status`` say WHICH
    tier produced it and in what state (``ok`` = the tier's own signals
    agreed, ``abstained`` = they disagreed and ``verdict`` was forced to
    ``"changes"`` as the safe escalation default). ``detail`` is a
    human-readable reason fit for a log line. ``signals`` carries the
    individual questions that were asked and what each answered, for
    debugging and for the benchmark's confusion matrix. ``attempts`` carries
    any higher tier that was tried first and could not produce a usable
    answer, so a caller can tell "the LLM tier decided this" apart from "the
    LLM tier was unreachable and the deterministic tier decided this".
    """

    verdict: TriageVerdict
    tier: TriageTier
    status: TriageStatus
    detail: str = ""
    signals: tuple[tuple[str, str], ...] = ()
    attempts: tuple[TierAttempt, ...] = ()

    @property
    def escalated(self) -> bool:
        """True when ``verdict`` was forced to ``"changes"`` by disagreement
        between this tier's own signals, rather than settled by agreement."""
        return self.status == "abstained"

    @property
    def used_fallback(self) -> bool:
        """True when a higher tier was attempted first and could not run or
        decide, so a lower tier produced the final verdict."""
        return any(a.status in ("failed", "unavailable") for a in self.attempts)

    def summary(self) -> str:
        """One line fit for a log line or a debug header."""
        base = f"triage={self.verdict} tier={self.tier}:{self.status}"
        if self.attempts:
            prior = ", ".join(f"{a.tier}:{a.status}" for a in self.attempts)
            base += f" (after [{prior}])"
        return base


# =============================================================================
# DETERMINISTIC TIER
# =============================================================================
# Zero dependency: regex plus a hand-rolled token-Jaccard, both stdlib. No
# network call is reachable from anything in this section.

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Ordinary function words, dropped before comparing two texts so that two
#: unrelated sentences do not score as "similar" just because they both
#: contain "the" and "is". Deliberately small and unglamorous; it is not a
#: real stopword list, only the handful of words common enough in everyday
#: skmemory-style notes to otherwise swamp the signal.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "it",
        "its",
        "this",
        "that",
        "with",
        "as",
        "at",
        "by",
        "from",
        "be",
        "has",
        "have",
        "had",
        "no",
        "not",
        "so",
        "but",
        "if",
        "than",
        "then",
        "too",
        "also",
        "he",
        "she",
        "they",
        "we",
        "you",
        "do",
        "does",
    }
)


def _content_tokens(text: str) -> frozenset[str]:
    """Casefolded content words, length >= 3, stopwords dropped."""
    return frozenset(
        w
        for w in _TOKEN_RE.findall((text or "").casefold())
        if len(w) >= 3 and w not in _STOPWORDS
    )


def _lexical_similarity(a: str, b: str) -> float:
    """Token-set Jaccard overlap of content words, stdlib only.

    This is the fallback used when no embedding similarity is available
    (``duplicate_matches`` not supplied). Not a semantic measure: two
    sentences about the same thing but with fully disjoint vocabulary will
    score low here even though an embedding model would place them close
    together. See the module docstring and :data:`LEXICAL_RELATED_FLOOR` for
    why this replaced a plain character-run ratio.
    """
    tokens_a, tokens_b = _content_tokens(a), _content_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


#: Two-part chatter test. A candidate is chatter only when BOTH agree: it
#: opens with a social/filler phrase, AND extractor.extract_memories() (the
#: house's existing regex classifier for lasting-knowledge content: decision,
#: preference, milestone, problem, emotional) finds nothing in it. Either
#: condition alone is not enough: "hey, we decided to ship v2" opens with a
#: greeting but IS a decision, and a quiet non-greeting sentence that still
#: trips no substance pattern ("the weather today") should still read as
#: chatter even without a greeting word, which is why the social pattern
#: below also covers bare momentary-state remarks, not just greetings.
_CHATTER_RE = re.compile(
    r"^\s*(?:"
    r"hi|hey|hello|yo|sup|howdy|"
    r"good\s+(?:morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|thx|"
    r"ok|okay|k|cool|nice|cute|neat|great|"
    r"got\s+it|sounds\s+good|no\s+worries|np|"
    r"lol|haha|lmao|brb|gtg|ttyl|"
    r"bye|see\s+you|take\s+care|"
    r"how\s+are\s+you|what'?s\s+up|"
    r"just\s+(?:checking\s+in|saying\s+hi|browsing|looking)|"
    r"i'?m\s+(?:bored|tired|hungry|sleepy|back|here)|"
    r"(?:it'?s|its)\s+(?:raining|sunny|cold|hot|nice)\s+(?:today|out|outside)"
    r")\b",
    re.IGNORECASE,
)

#: Correction / reversal / update language. Independent of how similar the
#: candidate's wording is to the best match: this fires on the LANGUAGE
#: itself, which is what lets "same topic, different count" or "same topic,
#: reversed" register as a change even when the two texts overlap heavily
#: (a correction often repeats most of the original sentence) or barely at
#: all (a terse status flip, "card prb-123 is now closed").
_CHANGE_CUE_RE = re.compile(
    r"\b("
    r"actually|correction|correct(?:ed|ion)?|retract(?:ed|ion)?|"
    r"no\s+longer|not\s+anymore|not\s+any\s+more|"
    r"instead(?:\s+of)?|reversed?|revers(?:al|ing)|"
    r"revised?|updated?|renamed?|replaced?|superse(?:de|ded)|"
    r"now\s+(?:is|at|closed|open|active|inactive|done|complete)|"
    r"used\s+to|previously|formerly|"
    r"deprecat(?:ed|ion)|retir(?:ed|ement)|"
    r"increased?|decreased?|changed\s+(?:to|from)|moved\s+to|"
    r"re-?opened|re-?closed"
    r")\b",
    re.IGNORECASE,
)


def _best_match(
    candidate: str,
    existing: Sequence[str],
    duplicate_matches: Sequence[Mapping[str, object]] | None,
) -> tuple[str | None, float, bool]:
    """Pick the single best-matching existing memory and its similarity.

    If ``duplicate_matches`` (the exact return shape of
    ``MemoryStore.check_duplicate()``) is given, its embedding-cosine scores
    are used AS GIVEN: this is the "turn check_duplicate from advisory into a
    routing decision" path, and no similarity math is redone. Otherwise falls
    back to a stdlib lexical ratio against each text in ``existing``, which
    is the path exercised when no vector backend is attached.

    Returns ``(best_text_or_None, score, embedding_based)``. ``score`` is
    ``0.0`` and ``best_text`` is ``None`` when there is nothing to compare
    against.
    """
    if duplicate_matches:
        best = max(duplicate_matches, key=lambda m: float(m.get("similarity", 0.0) or 0.0))
        text = best.get("content_preview")
        return (str(text) if text else None), float(best.get("similarity", 0.0) or 0.0), True

    candidates = [text for text in existing if text]
    if not candidates:
        return None, 0.0, False

    scored = [(text, _lexical_similarity(candidate, text)) for text in candidates]
    best_text, best_score = max(scored, key=lambda pair: pair[1])
    return best_text, best_score, False


def _overlap_signal(score: float, *, floor: float, ceiling: float) -> TriageVerdict:
    """Question 1: how similar is the candidate to its best match, by score
    alone? Three-way, always answers: below ``floor`` is confidently a
    different topic, at or above ``ceiling`` is confidently the same claim,
    the band between is "related but not confirmed identical"."""
    if score < floor:
        return "new"
    if score >= ceiling:
        return "restates"
    return "changes"


def _change_cue_signal(candidate: str, *, has_related_match: bool) -> TriageVerdict:
    """Question 2: independent of the score, does the candidate's own
    LANGUAGE carry a correction/update marker, given that something related
    exists to be corrected? If nothing related exists, there is nothing to
    change, so this agrees with the "new" answer regardless of wording."""
    if not has_related_match:
        return "new"
    return "changes" if _CHANGE_CUE_RE.search(candidate) else "restates"


def _is_chatter(candidate: str) -> tuple[bool, tuple[tuple[str, str], ...]]:
    social = bool(_CHATTER_RE.search(candidate))
    substantive = bool(extract_memories(candidate, min_length=0))
    signals = (
        ("social_pattern", "chatter" if social else "substantive"),
        ("substance_pattern", "substantive" if substantive else "chatter"),
    )
    return (social and not substantive), signals


def _deterministic_tier(
    candidate: str,
    existing: Sequence[str],
    duplicate_matches: Sequence[Mapping[str, object]] | None,
) -> TriageResult:
    """The default, zero-dependency tier. Always produces a verdict: there is
    nothing in here that can be ``unavailable`` or ``failed``, by design."""
    is_chatter, chatter_signals = _is_chatter(candidate)
    if is_chatter:
        return TriageResult(
            verdict="chatter",
            tier="deterministic",
            status="ok",
            detail="matches a social/filler pattern and no lasting-knowledge pattern",
            signals=chatter_signals,
        )

    best_text, score, embedding_based = _best_match(candidate, existing, duplicate_matches)
    floor = EMBEDDING_RELATED_FLOOR if embedding_based else LEXICAL_RELATED_FLOOR
    ceiling = EMBEDDING_RESTATE_CEILING if embedding_based else LEXICAL_RESTATE_CEILING
    has_related_match = best_text is not None and score >= floor

    overlap_vote = _overlap_signal(score, floor=floor, ceiling=ceiling)
    change_vote = _change_cue_signal(candidate, has_related_match=has_related_match)
    signals = chatter_signals + (
        ("overlap_tier", overlap_vote),
        ("change_cue", change_vote),
    )

    basis = "embedding similarity (check_duplicate)" if embedding_based else "lexical ratio"
    if overlap_vote == change_vote:
        if best_text is None:
            detail = f"no related memory found; both signals agree on {overlap_vote!r}"
        else:
            detail = f"{basis} score {score:.2f} and the change-cue scan agree on {overlap_vote!r}"
        return TriageResult(
            verdict=overlap_vote,
            tier="deterministic",
            status="ok",
            detail=detail,
            signals=signals,
        )

    return TriageResult(
        verdict="changes",
        tier="deterministic",
        status="abstained",
        detail=(
            f"signals disagreed (overlap_tier={overlap_vote!r} at {basis} score {score:.2f}, "
            f"change_cue={change_vote!r}); escalating to 'changes'"
        ),
        signals=signals,
    )


# =============================================================================
# LLM TIER (opt-in)
# =============================================================================
# Routes through skgateway's OpenAI-compatible endpoint. Stdlib urllib only,
# no added dependency, and nothing in this section is reachable unless the
# caller explicitly opts in (use_llm=True or SKMEMORY_TRIAGE_USE_LLM set).

_LLM_SYSTEM = (
    "You are a memory-triage classifier for a personal memory system. Decide how "
    "a CANDIDATE note relates to a set of EXISTING memories, then answer with "
    "exactly one word on the first line: RESTATES, NEW, CHANGES, or CHATTER.\n"
    "RESTATES: an existing memory already says this, even in other words.\n"
    "NEW: the candidate says something no existing memory says.\n"
    "CHANGES: the candidate corrects, reverses, retires, or updates something an "
    "existing memory says (a count, a status, a vague claim made specific).\n"
    "CHATTER: the candidate is passing noise, a greeting or a momentary state, "
    "with no lasting knowledge worth keeping."
)

_VERDICT_RE = re.compile(r"\b(restates|new|changes|chatter)\b", re.IGNORECASE)


def _format_context(
    existing: Sequence[str], duplicate_matches: Sequence[Mapping[str, object]] | None
) -> str:
    texts: list[str] = []
    if duplicate_matches:
        texts = [str(m["content_preview"]) for m in duplicate_matches if m.get("content_preview")]
    elif existing:
        texts = [text for text in existing if text]
    if not texts:
        return "(no existing related memories)"
    return "\n".join(f"- {text}" for text in texts[:5])


def _build_prompt(candidate: str, context: str, variant: str) -> str:
    question = (
        "Which single label best fits the CANDIDATE?"
        if variant == "a"
        else "Read the EXISTING memories first, then decide which one label the "
        "CANDIDATE deserves relative to them."
    )
    return f"EXISTING:\n{context}\n\nCANDIDATE: {candidate}\n\n{question}"


def _parse_verdict(text: str) -> TriageVerdict | None:
    if not text:
        return None
    match = _VERDICT_RE.search(text)
    if not match:
        return None
    word = match.group(1).casefold()
    if word in ("restates", "new", "changes", "chatter"):
        return word  # type: ignore[return-value]
    return None


def _gateway_complete(prompt: str, *, base_url: str, model: str, timeout: float) -> str:
    """One OpenAI-compatible chat completion call against skgateway.

    Stdlib urllib only. Raises on any transport or parse problem; the caller
    (``_llm_tier``) is the one that decides what a raised exception MEANS for
    the overall triage result, so this function does not swallow anything.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    choices = body.get("choices") or [{}]
    return choices[0].get("message", {}).get("content", "") or ""


def _llm_tier(
    candidate: str,
    existing: Sequence[str],
    duplicate_matches: Sequence[Mapping[str, object]] | None,
    *,
    base_url: str,
    model: str,
    llm_call: Callable[[str], str],
) -> TierAttempt:
    """Ask the gateway two independently phrased questions and only accept a
    settled verdict when they agree, per the module's agreement-over-
    confidence policy. Disagreement escalates to ``changes`` (``abstained``),
    same as the deterministic tier; only an unreachable gateway produces
    ``failed`` or ``unavailable``."""
    if not base_url or not model:
        return TierAttempt(
            tier="llm", status="unavailable", detail="no gateway url/model configured"
        )

    context = _format_context(existing, duplicate_matches)
    try:
        reply_a = llm_call(_build_prompt(candidate, context, "a"))
        reply_b = llm_call(_build_prompt(candidate, context, "b"))
    except Exception as exc:  # noqa: BLE001 - one dead gateway must not kill triage
        logger.warning("triage llm tier: gateway call failed: %s", exc)
        return TierAttempt(tier="llm", status="failed", detail=f"gateway call failed: {exc}")

    verdict_a = _parse_verdict(reply_a)
    verdict_b = _parse_verdict(reply_b)
    if verdict_a is not None and verdict_a == verdict_b:
        return TierAttempt(
            tier="llm",
            status="ok",
            detail=f"two independently phrased questions agreed on {verdict_a!r}",
            verdict=verdict_a,
        )
    return TierAttempt(
        tier="llm",
        status="abstained",
        detail=(
            f"the two questions disagreed (a={verdict_a!r}, b={verdict_b!r}); "
            "escalating to 'changes'"
        ),
        verdict="changes",
    )


def _default_llm_call(base_url: str, model: str, timeout: float) -> Callable[[str], str]:
    def call(prompt: str) -> str:
        return _gateway_complete(prompt, base_url=base_url, model=model, timeout=timeout)

    return call


# =============================================================================
# PUBLIC ENTRY POINT
# =============================================================================


def triage(
    candidate: str,
    existing: Sequence[str] = (),
    *,
    duplicate_matches: Sequence[Mapping[str, object]] | None = None,
    use_llm: bool | None = None,
    gateway_url: str | None = None,
    gateway_model: str | None = None,
    timeout: float | None = None,
    llm_call: Callable[[str], str] | None = None,
) -> TriageResult:
    """Classify ``candidate`` against its related existing memories.

    Args:
        candidate: the new content being considered for storage.
        existing: text of related existing memories, already retrieved by
            the caller (e.g. via ``MemoryStore.search()``). Ignored when
            ``duplicate_matches`` is given.
        duplicate_matches: the exact return shape of
            ``MemoryStore.check_duplicate()``
            (``[{"id", "content_preview", "similarity"}, ...]``). When given,
            its embedding-cosine similarity is used directly instead of the
            lexical fallback, which is how this module turns check_duplicate
            from advisory into a routing decision.
        use_llm: force the LLM tier on or off. ``None`` (default) reads
            ``SKMEMORY_TRIAGE_USE_LLM`` (falls back to off). The deterministic
            tier is ALWAYS the one that runs unless this resolves truthy.
        gateway_url: override the skgateway base URL (default
            ``SKMEMORY_TRIAGE_GATEWAY_URL`` env or
            :data:`DEFAULT_GATEWAY_URL`).
        gateway_model: override the model name (default
            ``SKMEMORY_TRIAGE_GATEWAY_MODEL`` env or
            :data:`DEFAULT_GATEWAY_MODEL`; never hardcode a vendor model).
        timeout: override the gateway request timeout in seconds.
        llm_call: injectable ``(prompt: str) -> str`` replacing the real
            gateway HTTP call. Tests use this to simulate an unreachable
            gateway without touching the network, and to prove that nothing
            in the default (``use_llm`` falsy) path ever calls it.

    Returns:
        A :class:`TriageResult`. Never raises for a down gateway: that
        becomes ``attempts=(TierAttempt(status="failed", ...),)`` with the
        deterministic tier's verdict as the final answer instead.
    """
    enabled = _env_flag("SKMEMORY_TRIAGE_USE_LLM") if use_llm is None else use_llm
    if not enabled:
        return _deterministic_tier(candidate, existing, duplicate_matches)

    base_url = gateway_url or os.environ.get("SKMEMORY_TRIAGE_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    model = gateway_model or os.environ.get("SKMEMORY_TRIAGE_GATEWAY_MODEL", DEFAULT_GATEWAY_MODEL)
    tmo = timeout or float(os.environ.get("SKMEMORY_TRIAGE_TIMEOUT", str(DEFAULT_GATEWAY_TIMEOUT)))
    call = llm_call if llm_call is not None else _default_llm_call(base_url, model, tmo)

    attempt = _llm_tier(
        candidate, existing, duplicate_matches, base_url=base_url, model=model, llm_call=call
    )

    if attempt.status in ("failed", "unavailable"):
        fallback = _deterministic_tier(candidate, existing, duplicate_matches)
        return TriageResult(
            verdict=fallback.verdict,
            tier=fallback.tier,
            status=fallback.status,
            detail=(
                f"LLM tier {attempt.status} ({attempt.detail}); fell back to the "
                f"deterministic tier. {fallback.detail}"
            ),
            signals=fallback.signals,
            attempts=(attempt,),
        )

    verdict = attempt.verdict
    assert verdict is not None  # ok/abstained always carry a verdict
    return TriageResult(
        verdict=verdict,
        tier="llm",
        status=attempt.status,
        detail=attempt.detail,
    )
