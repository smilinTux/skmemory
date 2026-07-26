#!/usr/bin/env python3
"""Groundedness / citation-faithfulness scorer for grounded answers.

Companion to the retrieval harnesses in this package:

    recall_benchmark.py   recall@k / NDCG@k over the keyword path.
    retrieval_bench.py    precision@k / recall@k / MRR over hybrid search,
                          plus a privacy leak count.
    groundedness_scorer   THIS file: given a produced ANSWER and the source
                          chunks it cited, decide whether each claim in the
                          answer is actually SUPPORTED by those citations, and
                          FLAG the unsupported ones (hallucinations).

Where the answers come from
---------------------------
The ``fill_stub`` / wiki-grounding path (in the skingest ingestion service)
produces an answer string together with the retrieved source chunks it was
grounded on. This scorer is the quality gate over that pair: it does NOT
retrieve anything itself, it audits an already-produced (answer, citations)
tuple for citation faithfulness. It is equally usable over any grounded-answer
pipeline that hands back ``(answer_text, cited_source_chunks)``.

The judge is PLUGGABLE
----------------------
Deciding "is this claim supported by these sources?" is a natural-language
entailment call, so the default backend is a local LLM judge on ``sk-default``
(the skgateway inference endpoint, ``http://localhost:18780/v1``, an
OpenAI-compatible ``/chat/completions``). But the scorer never hard-depends on
that: it accepts an injected ``judge_fn`` so callers - and, crucially, the
regression tests - can run fully OFFLINE and DETERMINISTIC with a mock judge.
No network is touched unless the default judge is actually selected AND invoked.

A ``judge_fn`` has the signature::

    judge_fn(claim: str, citations: Sequence[str]) -> ClaimVerdict | bool | str

Return either a :class:`ClaimVerdict`, a bare ``bool`` (True = supported), or a
string verdict (``"supported"`` / ``"unsupported"``, case-insensitive). The
scorer normalizes all three shapes.

Run:    python -m skmemory.eval.groundedness_scorer   (uses the live sk-default
        judge over a tiny built-in demo; prints a report)
Import: from skmemory.eval.groundedness_scorer import (
            score_groundedness, split_claims, sk_default_judge,
        )
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

# =============================================================================
# DATA MODEL
# =============================================================================


@dataclass
class ClaimVerdict:
    """The judgement for a single claim against the cited sources.

    Attributes:
        claim: the claim text (one atomic statement from the answer).
        supported: True if the citations back the claim.
        reason: optional short rationale from the judge.
    """

    claim: str
    supported: bool
    reason: str = ""


@dataclass
class GroundednessResult:
    """Aggregate groundedness of an answer against its citations.

    Attributes:
        score: fraction of claims that are supported, in ``[0.0, 1.0]``. An
            answer with no claims scores ``1.0`` (nothing to hallucinate).
        num_claims: total claims extracted from the answer.
        num_supported: how many were judged supported.
        verdicts: per-claim :class:`ClaimVerdict` list (answer order).
        flagged_claims: the claim strings judged UNSUPPORTED (the hallucination
            flags the card asks for), a convenience view over ``verdicts``.
    """

    score: float
    num_claims: int
    num_supported: int
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    flagged_claims: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """True when every claim is supported (no flags)."""
        return not self.flagged_claims


# The three accepted return shapes of a judge_fn.
JudgeReturn = "ClaimVerdict | bool | str"
JudgeFn = Callable[[str, Sequence[str]], object]


# =============================================================================
# CLAIM SPLITTING - break an answer into atomic, individually-checkable claims.
# =============================================================================

# Sentence terminator followed by whitespace, but NOT after a common
# abbreviation or a decimal point. Deliberately simple + dependency-free; the
# scorer's job is the entailment call, not perfect NLP segmentation.
_ABBREV = {
    "e.g",
    "i.e",
    "etc",
    "vs",
    "mr",
    "mrs",
    "ms",
    "dr",
    "st",
    "no",
    "fig",
    "approx",
    "cf",
    "al",
    "inc",
    "ltd",
    "co",
}
# Abbreviations + decimals are neutralized (\x00) before this runs, so any
# remaining terminator is a real sentence end; the next token may start with a
# lowercase identifier (e.g. "skmem-pg"), so the lookahead accepts any letter.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Za-z0-9])")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)


def split_claims(answer_text: str) -> list[str]:
    """Split an answer into atomic claims (roughly one per sentence / bullet).

    Splits first on line boundaries (so bullet / numbered lists become one
    claim each), then on sentence terminators within a line, guarding against
    a handful of common abbreviations and decimal points so "e.g." or "3.5"
    do not spuriously break a claim.

    Args:
        answer_text: the produced answer.

    Returns:
        A list of non-empty, stripped claim strings, in answer order.
        Empty / whitespace-only input yields ``[]``.
    """
    if not answer_text or not answer_text.strip():
        return []

    claims: list[str] = []
    # Normalize bullet markers to line breaks so each list item stands alone.
    for line in _BULLET_RE.sub("\n", answer_text).splitlines():
        line = line.strip()
        if not line:
            continue
        # Protect abbreviations + decimals from the sentence splitter.
        protected = line
        for abv in _ABBREV:
            protected = re.sub(
                rf"(?i)\b{re.escape(abv)}\.",
                lambda m: m.group(0)[:-1] + "\x00",
                protected,
            )
        protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + "\x00" + m.group(2), protected)

        for piece in _SENT_SPLIT_RE.split(protected):
            piece = piece.replace("\x00", ".").strip()
            if piece:
                claims.append(piece)
    return claims


# =============================================================================
# VERDICT NORMALIZATION - accept ClaimVerdict / bool / str from any judge_fn.
# =============================================================================

_SUPPORTED_WORDS = {"supported", "support", "yes", "true", "entailed", "grounded"}
_UNSUPPORTED_WORDS = {
    "unsupported",
    "no",
    "false",
    "contradicted",
    "hallucinated",
    "not_supported",
}


def _normalize_verdict(claim: str, raw: object) -> ClaimVerdict:
    """Coerce a judge_fn return value into a :class:`ClaimVerdict`.

    Accepts a ready ``ClaimVerdict`` (passed through, claim text preserved), a
    bare ``bool`` (True = supported), or a string verdict. Unknown strings are
    treated as UNSUPPORTED - fail closed, so an ambiguous judge never silently
    marks a claim grounded.
    """
    if isinstance(raw, ClaimVerdict):
        # Preserve the caller's claim text association.
        return ClaimVerdict(claim=claim, supported=bool(raw.supported), reason=raw.reason)
    if isinstance(raw, bool):
        return ClaimVerdict(claim=claim, supported=raw)
    if isinstance(raw, str):
        token = raw.strip().casefold()
        first = token.split()[0] if token.split() else token
        if token in _SUPPORTED_WORDS or first in _SUPPORTED_WORDS:
            return ClaimVerdict(claim=claim, supported=True, reason=raw.strip())
        return ClaimVerdict(claim=claim, supported=False, reason=raw.strip())
    if isinstance(raw, dict):
        supported = raw.get("supported")
        if isinstance(supported, str):
            supported = supported.strip().casefold() in _SUPPORTED_WORDS
        return ClaimVerdict(
            claim=claim, supported=bool(supported), reason=str(raw.get("reason", ""))
        )
    # Anything else -> fail closed.
    return ClaimVerdict(claim=claim, supported=False, reason="unparseable judge verdict")


# =============================================================================
# CORE SCORER
# =============================================================================


def score_groundedness(
    answer_text: str,
    cited_source_chunks: Sequence[str],
    *,
    judge_fn: JudgeFn | None = None,
    claim_splitter: Callable[[str], list[str]] | None = None,
) -> GroundednessResult:
    """Score how well an answer is grounded in its cited sources.

    For each claim extracted from ``answer_text``, ask the judge whether the
    ``cited_source_chunks`` support it. The groundedness score is the fraction
    of supported claims; every unsupported claim is FLAGGED.

    Args:
        answer_text: the produced (grounded) answer to audit.
        cited_source_chunks: the source chunk texts the answer cited / was
            grounded on. If EMPTY, no claim can be supported, so every claim is
            flagged and the score is ``0.0`` (unless there are no claims).
        judge_fn: pluggable entailment judge. Defaults to :func:`sk_default_judge`
            (the live sk-default LLM). Inject a deterministic function to run
            offline - the tests do exactly this.
        claim_splitter: override the claim segmentation (defaults to
            :func:`split_claims`).

    Returns:
        A :class:`GroundednessResult`.
    """
    splitter = claim_splitter or split_claims
    claims = splitter(answer_text)

    if not claims:
        # Nothing asserted -> vacuously grounded.
        return GroundednessResult(score=1.0, num_claims=0, num_supported=0)

    citations = [c for c in (cited_source_chunks or []) if c and c.strip()]

    # Short-circuit: with zero usable citations nothing can be supported. We do
    # NOT call the judge at all (correct AND keeps the no-network guarantee when
    # a caller passes empty citations with the default judge).
    if not citations:
        verdicts = [
            ClaimVerdict(claim=c, supported=False, reason="no citations provided") for c in claims
        ]
        return GroundednessResult(
            score=0.0,
            num_claims=len(claims),
            num_supported=0,
            verdicts=verdicts,
            flagged_claims=list(claims),
        )

    judge = judge_fn or sk_default_judge

    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        verdicts.append(_normalize_verdict(claim, judge(claim, citations)))

    num_supported = sum(1 for v in verdicts if v.supported)
    flagged = [v.claim for v in verdicts if not v.supported]
    return GroundednessResult(
        score=num_supported / len(claims),
        num_claims=len(claims),
        num_supported=num_supported,
        verdicts=verdicts,
        flagged_claims=flagged,
    )


# =============================================================================
# DEFAULT JUDGE - sk-default via the skgateway OpenAI-compatible endpoint.
# Stdlib-only (urllib), no added dependency. Never imported side effects; only
# reaches the network when actually CALLED.
# =============================================================================

SK_DEFAULT_URL = "http://localhost:18780/v1"
SK_DEFAULT_MODEL = "sk-default"
SK_DEFAULT_TIMEOUT = 60

_JUDGE_SYSTEM = (
    "You are a strict citation-faithfulness judge. You are given a CLAIM and a "
    "set of numbered SOURCE excerpts. Decide whether the SOURCES, taken "
    "together, directly support the CLAIM. A claim is supported ONLY if its "
    "factual content can be verified from the sources; do not use outside "
    "knowledge. If the claim adds any fact not present in the sources, it is "
    "unsupported. Respond with a single word on the first line, exactly "
    '"SUPPORTED" or "UNSUPPORTED", optionally followed by a brief reason.'
)


def _build_judge_prompt(claim: str, citations: Sequence[str]) -> str:
    sources = "\n".join(f"[{i + 1}] {c}" for i, c in enumerate(citations))
    return f"SOURCES:\n{sources}\n\nCLAIM: {claim}\n\nIs the claim supported by the sources?"


def sk_default_judge(
    claim: str,
    citations: Sequence[str],
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
) -> ClaimVerdict:
    """Default LLM judge: ask sk-default (skgateway) if the claim is supported.

    OpenAI-compatible ``POST {base_url}/chat/completions``. Stdlib urllib only.

    Fails CLOSED: any transport / parse error yields an UNSUPPORTED verdict
    (with the error in ``reason``) rather than raising, so a scorer run never
    crashes because the endpoint is down - it just flags conservatively. Tests
    never reach this path (they inject a mock judge).

    Args:
        claim: the single claim to check.
        citations: the source excerpts to check it against.
        base_url: override endpoint (default env ``SKMEMORY_JUDGE_URL`` or
            :data:`SK_DEFAULT_URL`).
        model: override model (default env ``SKMEMORY_JUDGE_MODEL`` or
            :data:`SK_DEFAULT_MODEL`).
        timeout: request timeout seconds.

    Returns:
        A :class:`ClaimVerdict`.
    """
    url = (base_url or os.environ.get("SKMEMORY_JUDGE_URL", SK_DEFAULT_URL)).rstrip("/")
    mdl = model or os.environ.get("SKMEMORY_JUDGE_MODEL", SK_DEFAULT_MODEL)
    tmo = timeout or int(os.environ.get("SKMEMORY_JUDGE_TIMEOUT", str(SK_DEFAULT_TIMEOUT)))

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": _build_judge_prompt(claim, citations)},
        ],
        "temperature": 0.0,
        "stream": False,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=tmo) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return _normalize_verdict(claim, content.strip() or "unsupported")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return ClaimVerdict(claim=claim, supported=False, reason=f"judge error: {exc}")


# =============================================================================
# SIMPLE OFFLINE JUDGE - a dependency-free lexical-overlap fallback, handy for
# demos / smoke runs when no LLM is up. NOT used by the scorer unless injected.
# =============================================================================


def lexical_overlap_judge(
    claim: str,
    citations: Sequence[str],
    *,
    threshold: float = 0.6,
) -> ClaimVerdict:
    """Deterministic fallback judge: token-overlap of claim vs citations.

    A claim is "supported" if at least ``threshold`` of its content tokens (>=3
    chars, stopwords dropped) appear somewhere in the concatenated citations.
    Crude - it cannot detect contradiction or paraphrase - but fully offline
    and deterministic. Useful as an injected judge for demos; the tests use
    their own explicit mock for clarity.
    """
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "are",
        "was",
        "were",
        "has",
        "have",
        "from",
        "into",
        "its",
        "their",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "is",
        "it",
        "as",
        "at",
        "by",
        "or",
    }
    toks = [t for t in re.findall(r"[a-z0-9]+", claim.casefold()) if len(t) >= 3 and t not in stop]
    if not toks:
        return ClaimVerdict(claim=claim, supported=True, reason="no content tokens")
    haystack = " ".join(citations).casefold()
    hits = sum(1 for t in toks if t in haystack)
    ratio = hits / len(toks)
    return ClaimVerdict(
        claim=claim,
        supported=ratio >= threshold,
        reason=f"token overlap {hits}/{len(toks)}={ratio:.2f}",
    )


# =============================================================================
# DEMO DRIVER
# =============================================================================

_DEMO_CITATIONS = [
    "The mxbai-embed-large embedding server runs on 192.168.0.100 port 11434.",
    "skmem-pg is a Postgres image with pgvector and BM25 via pg_search.",
]
_DEMO_ANSWER = (
    "The mxbai embedding server runs on port 11434. "
    "skmem-pg uses pgvector for vector search. "
    "It also automatically trains a new model every night."  # unsupported
)


def _print_report(result: GroundednessResult) -> None:
    print(
        f"groundedness score: {result.score:.2f}  "
        f"({result.num_supported}/{result.num_claims} claims supported)"
    )
    if result.flagged_claims:
        print(f"\nFLAGGED (unsupported) claims: {len(result.flagged_claims)}")
        for v in result.verdicts:
            if not v.supported:
                reason = f"  [{v.reason}]" if v.reason else ""
                print(f"  x {v.claim}{reason}")
    else:
        print("all claims supported - fully grounded.")


def main() -> None:
    # Uses the live sk-default judge. If the endpoint is down, sk_default_judge
    # fails closed (flags everything) rather than crashing.
    result = score_groundedness(_DEMO_ANSWER, _DEMO_CITATIONS)
    _print_report(result)


if __name__ == "__main__":
    main()
