"""Reranking for fused retrieval (R2).

The problem R1 left behind
-------------------------
Fusion widens recall and dilutes the head. Measured on real skmem-pg with real
mxbai embeddings, R1 took overall recall@5 from 0.375 to 0.417 while MRR fell
from 0.298 to 0.266: the right answers arrive in the pool but not at the top,
because rank-depth interleaving guarantees every arm's champion a head slot and
some of those champions are topical near-misses.

Reranking is the stage that fixes that, and it is why R1 and R2 were always a
pair rather than two independent improvements.

The identifier case, concretely
-------------------------------
For the query "per node postgres rebuild remediation plan prb-6f069c5e" against
a 128-document corpus, measured on the real backend:

    the terse `prb-6f069c5e` record  ... fused pool rank 8
    IDF over that pool              ... prb and 6f069c5e have df=1 (idf 2.85),
                                        every other query term df=8..16
                                        (idf 0.45..1.12)

So the signal separating the answer from its topical neighbours is extremely
clean, and BM25 alone cannot act on it: six common terms at moderate IDF
outweigh one rare term at high IDF, which is why the distractors win the raw
lexical ranking too.

Why rank space, and why it abstains
-----------------------------------
Two deliberate constraints, both learned rather than assumed:

1. **Boost in rank space, never by scaling the fused score.** RRF at k=60 spans
   only 1/61 to 1/360 across a 300-candidate window, under a factor of 6, so any
   multiplicative weight above that spread stops being a weight and silently
   turns the sort lexicographic. Hindsight shipped w=7, above the spread, and
   measured recall@20 collapse from 0.97 to 0.40. Dividing a RANK is bounded by
   construction: a candidate already ranked 1 cannot be displaced by a boost.

   That bound is load-bearing here. The entity query "chiap08" contains a digit
   and is rare, so it looks exactly like an identifier, but the document that
   CONTAINS "chiap08" is the bridge, not the answer. An absolute reorder would
   promote the bridge over the answer and wreck the entity class. A rank-space
   divisor cannot: the answer holds rank 1 from the graph arm, and the bridge
   can at best improve to `its_rank / divisor`.

2. **Abstain unless a term is genuinely identifier-shaped.** A paraphrase query
   ("rows vanished after suspend") shares no raw token with its answer, so
   boosting on raw-term presence would promote whichever decoy happens to carry
   a rare ordinary word. Restricting the boost to tokens that carry a digit (or
   are implausibly long) targets identifiers, hostnames, error codes, card ids
   and versions, and leaves ordinary English alone. A reranker with an opinion
   about every query adds noise to every query.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

#: A query term is only a boost candidate when it appears in at most this
#: fraction of the candidate pool. A term spread across the pool does not
#: discriminate between its members, whatever its global rarity.
DEFAULT_MAX_DF_RATIO = 0.15

#: Rank divisor for a boosted candidate: new sort key is ``rank / divisor``.
#: Bounded by construction, so this can never displace a rank-1 candidate.
#:
#: Swept, not guessed, on BOTH harnesses. Offline, the identifier class reaches
#: 1.000 at any divisor >= 2.0 and MRR saturates at 5.0. On real skmem-pg with
#: real mxbai, overall recall@5 is flat at 0.625 from 2.0 upward while MRR keeps
#: climbing to 5.0 (0.284 -> 0.298 -> 0.347) and then stops. Both agree on 5.0,
#: and no query class regresses at any divisor tried, up to 25.0: the rank-space
#: bound is what makes that safe, since a candidate already at rank 1 cannot be
#: displaced however hard another is boosted.
DEFAULT_RANK_DIVISOR = 5.0

#: Minimum length for a digit-free token to count as identifier-shaped. Ordinary
#: English words are rarely this long; opaque keys and hashes often are.
DEFAULT_MIN_OPAQUE_LEN = 12

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HAS_DIGIT = re.compile(r"\d")

RerankStatus = Literal["applied", "abstained"]


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").casefold()) if len(t) >= 2]


@dataclass(frozen=True)
class RerankOutcome:
    """The new order, plus why it is what it is.

    ``order`` indexes into the candidate list the caller passed in. An
    ``abstained`` outcome returns the identity order, so a caller can apply the
    result unconditionally and still get unchanged behaviour. The status is
    reported rather than inferred, for the same reason every retrieval arm
    reports one: a reranker that silently did nothing must not look like a
    reranker that ran.
    """

    order: list[int]
    status: RerankStatus = "applied"
    boost_terms: tuple[str, ...] = ()
    boosted: tuple[int, ...] = ()
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == "applied"


class Reranker(Protocol):
    """Anything that can reorder a candidate pool for a query."""

    def rerank(self, query: str, documents: list[str]) -> RerankOutcome: ...


def identifier_shaped(term: str, *, min_opaque_len: int = DEFAULT_MIN_OPAQUE_LEN) -> bool:
    """Whether *term* looks like an identifier rather than ordinary vocabulary.

    A digit is the strong signal: card ids (``6f069c5e``), hostnames
    (``zioap01``), versions, dates and error codes carry one, and ordinary
    English words do not. Long opaque tokens without a digit qualify too.
    """
    if _HAS_DIGIT.search(term):
        return True
    return len(term) >= min_opaque_len


def discriminative_terms(
    query: str,
    documents: list[str],
    *,
    max_df_ratio: float = DEFAULT_MAX_DF_RATIO,
    min_opaque_len: int = DEFAULT_MIN_OPAQUE_LEN,
) -> list[str]:
    """Identifier-shaped query terms rare enough to separate pool members.

    Returns them most-discriminative first. Empty means the reranker should
    abstain: either the query carries no identifier, or the identifier it
    carries is spread across the pool and so tells the members apart.
    """
    if not documents:
        return []
    doc_tokens = [set(tokenize(d)) for d in documents]
    n = len(doc_tokens)
    cap = max(1, int(max_df_ratio * n))

    scored: list[tuple[str, float]] = []
    for term in dict.fromkeys(tokenize(query)):
        if not identifier_shaped(term, min_opaque_len=min_opaque_len):
            continue
        df = sum(1 for toks in doc_tokens if term in toks)
        if df == 0 or df > cap:
            continue
        scored.append((term, math.log((n + 1) / (df + 0.5))))
    scored.sort(key=lambda p: (-p[1], p[0]))
    return [term for term, _ in scored]


@dataclass
class RareTermReranker:
    """Rank-space boost for candidates carrying the query's identifiers.

    Deterministic, offline, no dependencies and no model, so it can run in CI
    and on an air-gapped node. It is not a substitute for a cross-encoder on
    general relevance; it targets one specific, measurable failure (an exact
    identifier buried under its topical neighbours) and abstains on everything
    else.
    """

    rank_divisor: float = DEFAULT_RANK_DIVISOR
    max_df_ratio: float = DEFAULT_MAX_DF_RATIO
    min_opaque_len: int = DEFAULT_MIN_OPAQUE_LEN

    def rerank(self, query: str, documents: list[str]) -> RerankOutcome:
        identity = list(range(len(documents)))
        if len(documents) < 2:
            return RerankOutcome(order=identity, status="abstained", detail="pool too small")

        terms = discriminative_terms(
            query,
            documents,
            max_df_ratio=self.max_df_ratio,
            min_opaque_len=self.min_opaque_len,
        )
        if not terms:
            return RerankOutcome(
                order=identity,
                status="abstained",
                detail="no identifier-shaped, pool-discriminating query term",
            )

        term_set = set(terms)
        doc_tokens = [set(tokenize(d)) for d in documents]
        divisor = max(1.0, self.rank_divisor)

        keys: list[tuple[float, int]] = []
        boosted: list[int] = []
        for idx, toks in enumerate(doc_tokens):
            rank = idx + 1  # the pool arrives already ranked
            hits = len(term_set & toks)
            if hits:
                boosted.append(idx)
                # More identifier hits means a stronger claim, so divide harder,
                # but never below rank 1: a boost cannot invent a better
                # position than the head of the list.
                keys.append((max(1.0, rank / (divisor * hits)), idx))
            else:
                keys.append((float(rank), idx))

        keys.sort()
        return RerankOutcome(
            order=[idx for _, idx in keys],
            status="applied",
            boost_terms=tuple(terms),
            boosted=tuple(boosted),
            detail=f"boosted {len(boosted)} candidate(s) on {terms}",
        )


@dataclass
class NoopReranker:
    """Explicitly does nothing, and says so. The off switch with a name."""

    def rerank(self, query: str, documents: list[str]) -> RerankOutcome:
        return RerankOutcome(
            order=list(range(len(documents))), status="abstained", detail="reranking disabled"
        )


@dataclass
class ChainReranker:
    """Apply several rerankers in order, each on the previous one's output."""

    stages: list[Reranker] = field(default_factory=list)

    def rerank(self, query: str, documents: list[str]) -> RerankOutcome:
        order = list(range(len(documents)))
        applied: list[str] = []
        terms: tuple[str, ...] = ()
        for stage in self.stages:
            outcome = stage.rerank(query, [documents[i] for i in order])
            if outcome.applied:
                order = [order[i] for i in outcome.order]
                applied.append(type(stage).__name__)
                terms = terms or outcome.boost_terms
        return RerankOutcome(
            order=order,
            status="applied" if applied else "abstained",
            boost_terms=terms,
            detail=f"stages applied: {applied}" if applied else "no stage applied",
        )
