#!/usr/bin/env python3
"""A/B harness for retrieval FUSION strategies (R1).

Why this file exists
--------------------
``skmemory.eval.retrieval_bench`` scores the hybrid path on a 10-document
corpus whose own fixture comment says the vocabulary is "distinctive per item
so both legs (BM25 + vector) can find it". That is a smoke test: every
retrieval strategy scores 1.0 on it, so the committed baseline sits pinned at
``recall@1..5 = ndcg@1..5 = mrr = 1.0``. A benchmark at its ceiling cannot
detect a retrieval IMPROVEMENT and cannot detect a fusion REGRESSION either.

That is the same shape as the three defects ranked in
``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` (§3.2 ``health()``
reporting ``ok: True`` off a successful query instead of off the drift counts,
§3.3 the audit script exiting 0 on zero targets, §3.11 ``synthesize_*``
returning an empty narrative indistinguishable from a quiet day): a wrong
answer that looks exactly like a right one. So before changing how recall
works, this module builds an instrument that can actually fail.

What it measures
----------------
Five strategies over ONE corpus, broken out by QUERY CLASS, so the table shows
not just which strategy wins but WHY:

    vector_only     one dense arm (approximates ``search_semantic``)
    bm25_only       one lexical arm
    fallback_chain  THE CURRENT PRODUCTION BEHAVIOUR of ``MemoryStore.search()``:
                    vector first, and text search ONLY if vector returned an
                    empty list. Scored here so the thing being replaced has a
                    number next to it instead of an opinion.
    linear_alpha    ``retrieval_bench.hybrid_search``: min-max normalize both
                    legs, blend with a fixed alpha.
    rrf             Reciprocal Rank Fusion over all four arms with a per-arm
                    cap, the R1 candidate.

Four query classes, each built so that at least one arm FAILS on it. A fixture
where every arm succeeds (the current one) cannot tell fusion from a coin flip:

    lexical    a rare exact identifier (``prb-6f069c5e``, ``zioap01``) buried
               in a document whose topical neighbours all share its subject
               vocabulary. High-IDF term: BM25 nails it, the dense arm blurs it.
    semantic   a paraphrase with near-zero surface overlap with its answer.
               The dense arm bridges it, BM25 scores the answer at literally 0.
    temporal   many documents on one subject across months; only one is inside
               the queried window. Needs the date predicate, not the text.
    entity     the answer shares no query vocabulary and no concept with the
               query. It is reachable in ONE HOP through a bridge document that
               co-mentions the queried entity. Only the graph arm gets there.

Offline and deterministic
-------------------------
No network, no DB, no embedding server, no LLM. The dense arm is a hashed
bag-of-concepts (see :func:`_concept_vector`): token surface forms are mapped
through a small synonym table BEFORE hashing, which is what lets a paraphrase
land near its answer. The lexical arm deliberately does NOT get that expansion,
so the two arms are genuinely complementary rather than two spellings of the
same signal. Distractor assembly uses a fixed seed, so the corpus is
byte-identical on every run and across hosts.

Run:
    python -m skmemory.eval.fusion_bench            # the A/B table
    python -m skmemory.eval.fusion_bench --json     # machine-readable
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from statistics import mean

from skmemory.audience import AudienceLevel, AudienceProfile, AudienceResolver
from skmemory.eval.recall_benchmark import ndcg_at_k, recall_at_k
from skmemory.eval.retrieval_bench import (
    mean_reciprocal_rank,
    precision_at_k,
    reciprocal_rank,
)

# =============================================================================
# TOKENIZATION, CONCEPTS, AND THE TWO DELIBERATELY DIFFERENT SIGNALS
# =============================================================================

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_EMBED_DIM = 512
_BM25_K1 = 1.5
_BM25_B = 0.75

#: RRF constant. 60 is the value from the original Cormack et al. RRF paper and
#: the one Hindsight uses. Kept as a named constant because the whole point of
#: R1's rank-space boosting (R2) is that this ``k`` sets the usable dynamic
#: range of the fused score: with k=60 the score across a 300-candidate window
#: spans only 1/61 to 1/360, so any additive or multiplicative weight larger
#: than that spread silently turns the sort lexicographic. See R2's notes.
RRF_K = 60

#: Per-arm candidate cap, applied BEFORE fusion. Without it one over-expanding
#: arm (BM25 on a common term, or a graph arm on a hub entity) fills the merged
#: pool and crowds every other arm out of the final top-k.
ARM_CAP = 25


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").casefold()) if len(t) >= 2]


#: Surface form -> concept key. This is the stand-in for what a real dense
#: encoder gives us for free: "postgres" and "pg" land in the same place, so a
#: paraphrase can retrieve its answer. The LEXICAL arm does not use this map,
#: which is exactly why the two arms disagree and fusion has something to do.
_CONCEPT_SYNONYMS: dict[str, str] = {
    # storage / database
    "postgres": "pg",
    "postgresql": "pg",
    "pg": "pg",
    "database": "pg",
    "sqlite": "localindex",
    "index": "localindex",
    "cache": "localindex",
    # vectors / search
    "embedding": "vectorsearch",
    "embeddings": "vectorsearch",
    "vector": "vectorsearch",
    "vectors": "vectorsearch",
    "semantic": "vectorsearch",
    "cosine": "vectorsearch",
    "similarity": "vectorsearch",
    "nearest": "vectorsearch",
    "keyword": "lexicalsearch",
    "bm25": "lexicalsearch",
    "lexical": "lexicalsearch",
    "fulltext": "lexicalsearch",
    # durability
    "crash": "durability",
    "unclean": "durability",
    "fsync": "durability",
    "durable": "durability",
    "durability": "durability",
    "atomic": "durability",
    "rename": "durability",
    "pagecache": "durability",
    "powerloss": "durability",
    "suspend": "durability",
    # loss / recovery
    "orphan": "dataloss",
    "orphans": "dataloss",
    "missing": "dataloss",
    "lost": "dataloss",
    "vanished": "dataloss",
    "gone": "dataloss",
    "truncated": "dataloss",
    "stub": "dataloss",
    "preview": "dataloss",
    "recovery": "restore",
    "restore": "restore",
    "restored": "restore",
    "rescue": "restore",
    "rebuild": "restore",
    "reconstruct": "restore",
    # sync
    "syncthing": "replication",
    "sync": "replication",
    "replicate": "replication",
    "replication": "replication",
    "mirror": "replication",
    # monitoring
    "alert": "monitoring",
    "alerts": "monitoring",
    "monitor": "monitoring",
    "monitoring": "monitoring",
    "health": "monitoring",
    "audit": "monitoring",
    "drift": "monitoring",
    "check": "monitoring",
    # privacy
    "private": "privacy",
    "intimate": "privacy",
    "confidential": "privacy",
    "secret": "privacy",
    "redact": "privacy",
    "privacy": "privacy",
    # infra
    "gateway": "inference",
    "inference": "inference",
    "router": "inference",
    "model": "inference",
    "llm": "inference",
    "gpu": "accelerator",
    "arc": "accelerator",
    "cuda": "accelerator",
    "backup": "backups",
    "backups": "backups",
    "dump": "backups",
    "snapshot": "backups",
    "retention": "backups",
    "tarball": "backups",
    # process
    "cron": "scheduling",
    "timer": "scheduling",
    "systemd": "scheduling",
    "schedule": "scheduling",
    "nightly": "scheduling",
    "weekly": "scheduling",
    "queue": "worker",
    "worker": "worker",
    "job": "worker",
    "task": "worker",
}


def _concepts(text: str) -> list[str]:
    """Tokens with every known surface form collapsed onto its concept key."""
    return [_CONCEPT_SYNONYMS.get(t, t) for t in _tokenize(text)]


def _concept_vector(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-CONCEPTS, L2 normalized.

    Stands in for the mxbai dense leg. Pure function of the input, so the
    benchmark is reproducible bit for bit, but unlike a plain hashed
    bag-of-words it can match a paraphrase, because the synonym table runs
    first.
    """
    vec = [0.0] * dim
    for concept in _concepts(text):
        h = int.from_bytes(hashlib.blake2b(concept.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _bm25_scores(query: str, docs: Sequence[dict]) -> list[float]:
    """Okapi BM25 over RAW tokens (no concept expansion: that is the point)."""
    doc_tokens = [_tokenize(d["_text"]) for d in docs]
    doc_lens = [len(t) for t in doc_tokens]
    n = len(docs)
    avgdl = (sum(doc_lens) / n) if n else 0.0

    df: dict[str, int] = {}
    for toks in doc_tokens:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    q_terms = _tokenize(query)
    scores: list[float] = []
    for toks, dl in zip(doc_tokens, doc_lens, strict=False):
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0))
            score += idf * (f * (_BM25_K1 + 1)) / denom
        scores.append(score)
    return scores


def _minmax(values: Sequence[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


# =============================================================================
# THE FOUR ARMS
# =============================================================================
# Every arm returns a RANKED list of doc ids, best first, already capped. An arm
# that genuinely has nothing to say returns an empty list; it never raises and
# never pretends. Fusion treats "empty" as a real signal (one fewer voter), not
# as a reason to silently fall back to another arm, which is the specific defect
# this work removes from MemoryStore.search().


def arm_semantic(query: str, docs: Sequence[dict], cap: int = ARM_CAP) -> list[str]:
    """Dense arm: cosine over hashed bag-of-concepts."""
    qv = _concept_vector(query)
    scored = [(d["id"], _cosine(qv, d["_cvec"])) for d in docs]
    scored = [(i, s) for i, s in scored if s > 0.0]
    scored.sort(key=lambda p: (-p[1], p[0]))
    return [i for i, _ in scored[:cap]]


def arm_bm25(query: str, docs: Sequence[dict], cap: int = ARM_CAP) -> list[str]:
    """Lexical arm: Okapi BM25 over raw tokens."""
    scores = _bm25_scores(query, docs)
    scored = [(d["id"], s) for d, s in zip(docs, scores, strict=False) if s > 0.0]
    scored.sort(key=lambda p: (-p[1], p[0]))
    return [i for i, _ in scored[:cap]]


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
}


def arm_temporal(query: str, docs: Sequence[dict], cap: int = ARM_CAP) -> list[str]:
    """Temporal arm: parse a month (and optional year) out of the query, then
    return in-window docs ranked by how well their text matches, closest date
    first as the tiebreak.

    Deliberately narrow: if the query names no month, this arm abstains with an
    empty list rather than ranking the whole corpus by recency. An arm that
    always has an opinion is an arm that adds noise to every fusion.
    """
    q = query.casefold()
    month = next((num for name, num in _MONTHS.items() if name in q), None)
    if month is None:
        return []
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else 2026

    in_window = [
        d
        for d in docs
        if d["_dt"] is not None and d["_dt"].year == year and d["_dt"].month == month
    ]
    if not in_window:
        return []
    # Rank the in-window set by concept similarity so the arm is about WHEN,
    # with relevance only breaking ties inside the window.
    qv = _concept_vector(query)
    scored = [(d["id"], _cosine(qv, d["_cvec"]), d["_dt"]) for d in in_window]
    scored.sort(key=lambda p: (-p[1], p[2], p[0]))
    return [i for i, _, _ in scored[:cap]]


def arm_graph(query: str, docs: Sequence[dict], cap: int = ARM_CAP) -> list[str]:
    """Graph arm: one-hop entity expansion.

    Seed on documents whose own entity list contains an entity named in the
    query, then return the documents that SHARE an entity with a seed. This is
    the only arm that can reach an answer carrying neither the query's words nor
    its concepts, which is exactly the ``entity`` query class.
    """
    q_tokens = set(_tokenize(query))
    by_entity: dict[str, list[dict]] = {}
    for d in docs:
        for ent in d.get("entities", []):
            by_entity.setdefault(ent.casefold(), []).append(d)

    seeds = [d for d in docs if any(e.casefold() in q_tokens for e in d.get("entities", []))]
    if not seeds:
        return []

    seed_ids = {d["id"] for d in seeds}
    # Hop: every doc sharing an entity with a seed, scored by how many distinct
    # seed entities it shares (a stronger link beats an incidental one).
    hop_scores: dict[str, int] = {}
    for seed in seeds:
        for ent in seed.get("entities", []):
            for neighbour in by_entity.get(ent.casefold(), []):
                if neighbour["id"] in seed_ids:
                    continue
                hop_scores[neighbour["id"]] = hop_scores.get(neighbour["id"], 0) + 1

    ranked = sorted(hop_scores.items(), key=lambda p: (-p[1], p[0]))
    # Seeds themselves rank after the hop results: a doc that merely repeats the
    # queried entity is usually the bridge, not the answer.
    out = [i for i, _ in ranked] + sorted(seed_ids)
    return out[:cap]


ARMS: dict[str, Callable[[str, Sequence[dict], int], list[str]]] = {
    "semantic": arm_semantic,
    "bm25": arm_bm25,
    "temporal": arm_temporal,
    "graph": arm_graph,
}


# =============================================================================
# FUSION
# =============================================================================


def reciprocal_rank_fusion(
    arm_results: dict[str, Sequence[str]], k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """Merge ranked id lists by Reciprocal Rank Fusion.

    ``score(d) = sum over arms of 1 / (k + rank_in_that_arm(d))``, 1-based rank.

    Returns ``(doc_id, fused_score, {arm: rank})`` best first. The per-arm rank
    map is returned, not discarded, so a caller can see WHICH arm produced a
    result. That provenance is the difference between "recall got better" and
    "recall got better and here is the arm that did it", and it is what makes an
    arm silently returning nothing visible instead of invisible.
    """
    fused: dict[str, float] = {}
    provenance: dict[str, dict[str, int]] = {}
    for arm_name, ids in arm_results.items():
        for rank, doc_id in enumerate(ids, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
            provenance.setdefault(doc_id, {})[arm_name] = rank
    out = [(doc_id, score, provenance[doc_id]) for doc_id, score in fused.items()]
    # Deterministic tiebreak on id so the ranking is reproducible.
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


# =============================================================================
# STRATEGIES UNDER TEST
# =============================================================================
# Each takes (query, docs, k) and returns the top-k doc ids.


def strat_vector_only(query: str, docs: Sequence[dict], k: int) -> list[str]:
    return arm_semantic(query, docs, cap=k)


def strat_bm25_only(query: str, docs: Sequence[dict], k: int) -> list[str]:
    return arm_bm25(query, docs, cap=k)


def strat_fallback_chain(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """What ``MemoryStore.search()`` does TODAY (store.py, pre-R1).

    Verbatim shape of the production code:

        if self.vector:
            results = self.vector.search_text(...)
            if results:
                return results
        return self.primary.search_text(...)

    The dense arm answers unless it returns an EMPTY list, in which case the
    lexical arm answers. Note what this cannot do: when the dense arm returns
    something confident but wrong, the lexical arm is never consulted, even
    though it may hold the exact-identifier match at rank 1. That is the
    failure this benchmark is built to price.
    """
    primary = arm_semantic(query, docs, cap=k)
    if primary:
        return primary
    return arm_bm25(query, docs, cap=k)


def strat_linear_alpha(query: str, docs: Sequence[dict], k: int, alpha: float = 0.5) -> list[str]:
    """``retrieval_bench.hybrid_search``: min-max both legs, blend by alpha."""
    bm = _minmax(_bm25_scores(query, docs))
    qv = _concept_vector(query)
    vec = _minmax([_cosine(qv, d["_cvec"]) for d in docs])
    fused = [
        (d["id"], alpha * v + (1 - alpha) * b) for d, v, b in zip(docs, vec, bm, strict=False)
    ]
    fused.sort(key=lambda p: (-p[1], p[0]))
    return [i for i, _ in fused[:k]]


def strat_rrf(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """R1: RRF over all four arms, each capped before the merge."""
    arm_results = {name: fn(query, docs, ARM_CAP) for name, fn in ARMS.items()}
    return [doc_id for doc_id, _, _ in reciprocal_rank_fusion(arm_results)[:k]]


def rank_interleave(
    arm_results: dict[str, Sequence[str]], k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """RRF, but with every arm's champion protected from being voted down.

    Plain RRF has a well known failure that this repo's own fixture reproduces:
    a candidate placing mid-pack in TWO arms outscores a candidate placing first
    in ONE arm. With four arms where two routinely abstain, that is not an edge
    case, it is the common case. Measured on the semantic class, plain RRF put
    the correct answer (rank 1 on the dense arm, absent everywhere else) at
    final rank 17 behind four decoys, dropping recall@5 from 0.833 to 0.000.

    This variant orders by RANK DEPTH first and RRF score second: every arm's
    rank-1 candidate is emitted before any arm's rank-2 candidate, and RRF only
    breaks ties within a depth. An arm that is the only one to find something
    therefore still gets it into the head of the list, while an item that
    several arms agree on still wins its depth. Both signals survive, which is
    the point of fusing in the first place.
    """
    scored = {doc_id: (s, prov) for doc_id, s, prov in reciprocal_rank_fusion(arm_results, k)}
    depth_of: dict[str, int] = {}
    for ids in arm_results.values():
        for rank, doc_id in enumerate(ids, start=1):
            # Best (shallowest) rank this doc achieved in any single arm.
            if doc_id not in depth_of or rank < depth_of[doc_id]:
                depth_of[doc_id] = rank
    out = [(doc_id, scored[doc_id][0], scored[doc_id][1]) for doc_id in scored]
    out.sort(key=lambda t: (depth_of[t[0]], -t[1], t[0]))
    return out


def strat_rrf_interleave(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """R1 candidate: rank-depth interleaving over all four arms."""
    arm_results = {name: fn(query, docs, ARM_CAP) for name, fn in ARMS.items()}
    return [doc_id for doc_id, _, _ in rank_interleave(arm_results)[:k]]


def strat_rrf_tightcap(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """Plain RRF with a much tighter per-arm cap (5 instead of 25).

    Tests whether the single-arm-standout problem is really a CAP problem: with
    fewer candidates per arm there are fewer mid-pack decoys to accumulate votes.
    """
    arm_results = {name: fn(query, docs, 5) for name, fn in ARMS.items()}
    return [doc_id for doc_id, _, _ in reciprocal_rank_fusion(arm_results)[:k]]


def strat_interleave_tightcap(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """Rank-depth interleaving AND a tight per-arm cap."""
    arm_results = {name: fn(query, docs, 5) for name, fn in ARMS.items()}
    return [doc_id for doc_id, _, _ in rank_interleave(arm_results)[:k]]


STRATEGIES: dict[str, Callable[[str, Sequence[dict], int], list[str]]] = {
    "vector_only": strat_vector_only,
    "bm25_only": strat_bm25_only,
    "fallback_chain": strat_fallback_chain,
    "linear_alpha": strat_linear_alpha,
    "rrf": strat_rrf,
    "rrf_tightcap": strat_rrf_tightcap,
    "rrf_interleave": strat_rrf_interleave,
    "interleave_tight": strat_interleave_tightcap,
}


# =============================================================================
# FIXTURE CORPUS
# =============================================================================
# 24 answer documents (6 per query class) plus deterministically assembled
# topical distractors plus 4 private items. Every answer is built so that at
# least one arm CANNOT find it, which is the property the 10-document fixture
# in retrieval_bench lacks.

#: Each entry: (id, title, content, entities, created_at)
_LEXICAL_ANSWERS: list[tuple[str, str, str, list[str], str]] = [
    # Terse records carrying a high-IDF identifier and almost no topical
    # vocabulary. The dense arm has nothing to grip; BM25 has a df=1 term.
    (
        "lex_prb6f069c5e",
        "prb-6f069c5e",
        "Card prb-6f069c5e: closed, verified.",
        ["prb"],
        "2026-05-02T10:00:00Z",
    ),
    (
        "lex_zioap01",
        "zioap01",
        "Host zioap01 enrolled. Serial 4K8T2N1.",
        ["zioap01"],
        "2026-05-06T10:00:00Z",
    ),
    (
        "lex_chga543c87b",
        "chg-a543c87b",
        "Change chg-a543c87b folded to approved.",
        ["chg"],
        "2026-05-11T10:00:00Z",
    ),
    (
        "lex_card7d3e9fcc",
        "card 7d3e9fcc",
        "Card 7d3e9fcc signed off by Ara.",
        ["ara"],
        "2026-05-14T10:00:00Z",
    ),
    (
        "lex_dump20260806",
        "prereconcile-20260806",
        "File skmemory-pre-migrate-20260806-183244.dump kept.",
        ["dump"],
        "2026-05-19T10:00:00Z",
    ),
    ("lex_pr41", "PR 41", "Merged as 2d7e8a6 on main.", ["main"], "2026-05-23T10:00:00Z"),
]

#: Query, relevant ids. The identifier is the only reliable hook.
_LEXICAL_QUERIES: list[tuple[str, list[str]]] = [
    ("per node postgres rebuild remediation plan prb-6f069c5e", ["lex_prb6f069c5e"]),
    ("fleet host enrollment inventory record zioap01", ["lex_zioap01"]),
    ("change approval workflow cab vote record chg-a543c87b", ["lex_chga543c87b"]),
    ("cascade derived backend fan out card 7d3e9fcc", ["lex_card7d3e9fcc"]),
    ("postgres backup dump retention file 20260806", ["lex_dump20260806"]),
    ("synthesis date range query fix pull request 41", ["lex_pr41"]),
]

#: Topical filler for the lexical class: rich, on-subject prose that does NOT
#: carry the identifier. These are what the dense arm ranks above the answer.
_LEXICAL_DISTRACTOR_TOPICS: list[tuple[str, str]] = [
    (
        "per node postgres rebuild",
        "The per node postgres rebuild remediation plan describes the frozen streaming standby, the hardcoded dsn, the single point of failure, and the per node postgres rebuild preflight steps.",
    ),
    (
        "fleet host enrollment",
        "Fleet host enrollment inventory records cover every enrolled fleet host, the enrollment inventory record format, and the fleet host enrollment preflight.",
    ),
    (
        "change approval workflow",
        "The change approval workflow covers cab vote records, the change approval workflow gate, and every change approval workflow record kept for audit.",
    ),
    (
        "cascade derived backend",
        "The cascade derived backend fan out writes to each derived backend, and the cascade derived backend fan out records partial failure per derived backend.",
    ),
    (
        "postgres backup dump retention",
        "Postgres backup dump retention keeps nightly dumps, and the postgres backup dump retention tier prunes the oldest dump beyond the retention window.",
    ),
    (
        "synthesis date range query",
        "The synthesis date range query fix applies created after and created before bounds in the query, so the synthesis date range query no longer truncates.",
    ),
]

#: Semantic class: answers whose RAW tokens are disjoint from their query's,
#: but whose CONCEPTS overlap. BM25 scores these answers at exactly 0.
#: The invariant that makes this class meaningful: each answer shares ZERO raw
#: tokens with its query, so BM25 scores it at exactly 0 and literally cannot
#: retrieve it at any k, while the concept table puts the two close together so
#: the dense arm can. ``test_semantic_answers_have_no_lexical_overlap`` asserts
#: that invariant, because the first draft of this fixture quietly broke it: the
#: decoys were written with the query's own concept words ("rows vanished after")
#: and so outranked every answer on BOTH arms, giving a flat 0.000 that looked
#: like a strategy result and was really a fixture bug.
_SEMANTIC_ANSWERS: list[tuple[str, str, str, list[str], str]] = [
    (
        "sem_durability",
        "Shutdown note",
        "An unclean shutdown dropped a pagecache write before fsync ran, so a durable commit never landed and the entry is now an orphan, missing and gone.",
        ["laptop"],
        "2026-06-03T10:00:00Z",
    ),
    (
        "sem_restore",
        "Phase two",
        "The rescue pass restored truncated bodies, and the restore mined an off schedule dump and a tarball.",
        ["phase2"],
        "2026-06-07T10:00:00Z",
    ),
    (
        "sem_monitoring",
        "Instrument",
        "The weekly audit reported green while drift accumulated, so monitoring never fired an alarm.",
        ["auditor"],
        "2026-06-11T10:00:00Z",
    ),
    (
        "sem_replication",
        "Ring",
        "Flat files move through syncthing, and syncthing replication keeps every node in the ring; the sqlite layer is excluded by design.",
        ["ring"],
        "2026-06-15T10:00:00Z",
    ),
    (
        "sem_inference",
        "Routing",
        "The skgateway router picks an llm per request and reports which backend answered.",
        ["ornith"],
        "2026-06-19T10:00:00Z",
    ),
    (
        "sem_worker",
        "Claiming",
        "A queue poller claims a task batch under a lease and recovers orphaned units via cron.",
        ["poller"],
        "2026-06-23T10:00:00Z",
    ),
]

_SEMANTIC_QUERIES: list[tuple[str, list[str]]] = [
    # "vanished"+"suspend" -> dataloss+durability, zero raw overlap with answer.
    ("rows vanished after suspend", ["sem_durability"]),
    ("recovery rebuild from backup snapshot", ["sem_restore"]),
    ("monitor alert health check missing", ["sem_monitoring"]),
    ("mirror replicate index cache", ["sem_replication"]),
    ("inference model gateway", ["sem_inference"]),
    ("job scheduling worker", ["sem_worker"]),
]

#: Temporal class: one subject, many months. The month is ONLY in the
#: timestamp, never in the text, so no amount of text matching can separate
#: them. Only the date predicate can.
_TEMPORAL_SUBJECT = "The team settled the skgateway routing question and recorded the decision for the pool ceiling."
_TEMPORAL_MONTHS = [
    ("tmp_jan", "2026-01-12T10:00:00Z"),
    ("tmp_feb", "2026-02-12T10:00:00Z"),
    ("tmp_mar", "2026-03-12T10:00:00Z"),
    ("tmp_apr", "2026-04-12T10:00:00Z"),
    ("tmp_jun", "2026-06-12T10:00:00Z"),
    ("tmp_jul", "2026-07-12T10:00:00Z"),
    ("tmp_aug", "2026-08-12T10:00:00Z"),
    ("tmp_sep", "2026-09-12T10:00:00Z"),
]
_TEMPORAL_QUERIES: list[tuple[str, list[str]]] = [
    ("skgateway routing decision pool ceiling in June", ["tmp_jun"]),
    ("skgateway routing decision pool ceiling in March", ["tmp_mar"]),
    ("skgateway routing decision pool ceiling in August", ["tmp_aug"]),
    ("skgateway routing decision pool ceiling in February", ["tmp_feb"]),
    ("skgateway routing decision pool ceiling in September", ["tmp_sep"]),
    ("skgateway routing decision pool ceiling in January", ["tmp_jan"]),
]

#: Entity class: the answer shares neither vocabulary nor concept with the
#: query. It is reachable only by hopping through a bridge document that
#: co-mentions the queried entity.
#: (bridge_id, bridge_text, queried_entity, shared_entity, answer_id, answer_text)
#: ``queried_entity`` is stated explicitly rather than derived from the text:
#: deriving it from the first token silently produced the entity ``"the"`` for
#: the planner row, which cost that query its only retrieval path and showed up
#: as a quiet 5-of-6 instead of an error. A fixture that mis-builds itself and
#: still reports a plausible score is the same defect class this whole harness
#: exists to avoid.
_ENTITY_TRIPLES: list[tuple[str, str, str, str, str, str]] = [
    (
        "ent_bridge_beelama",
        "chiap08 hosts the beelama endpoint.",
        "chiap08",
        "beelama",
        "ent_ans_beelama",
        "Repetition collapse was traced to a stale kv reuse and corrected.",
    ),
    (
        "ent_bridge_arc",
        "chiap01 carries the arcbox accelerator.",
        "chiap01",
        "arcbox",
        "ent_ans_arc",
        "Render duration halved once the tile count was lowered.",
    ),
    (
        "ent_bridge_falkor",
        "noroc2027 runs the falkordb instance.",
        "noroc2027",
        "falkordb",
        "ent_ans_falkor",
        "The knowledge projection rebuilds nightly from flat sources.",
    ),
    (
        "ent_bridge_casey",
        "cakjr estate belongs to casey.",
        "cakjr",
        "casey",
        "ent_ans_casey",
        "A shim grants read only reach across the tailnet boundary.",
    ),
    (
        "ent_bridge_hermes",
        "chiap04 runs the hermes runtime.",
        "chiap04",
        "hermes",
        "ent_ans_hermes",
        "Cron invocations must pin an absolute interpreter path.",
    ),
    (
        "ent_bridge_chloe",
        "the family planner tracks chloe.",
        "planner",
        "chloe",
        "ent_ans_chloe",
        "Term dates and early release days were imported as all day entries.",
    ),
]
_ENTITY_QUERIES: list[tuple[str, list[str]]] = [
    ("chiap08", ["ent_ans_beelama"]),
    ("chiap01", ["ent_ans_arc"]),
    ("noroc2027", ["ent_ans_falkor"]),
    ("cakjr", ["ent_ans_casey"]),
    ("chiap04", ["ent_ans_hermes"]),
    ("planner", ["ent_ans_chloe"]),
]

#: Private items, to prove that ADDING ARMS does not add leak surface. A graph
#: or BM25 arm can reach a private row that the dense arm happened to miss, so
#: the leak count has to be re-proved per strategy, not inherited.
_PRIVATE_DOCS: list[tuple[str, str, str, list[str], str]] = [
    (
        "priv_cloud9",
        "Cloud 9 state",
        "Intimate continuity depth nine, trust and love held at full.",
        ["cloud9"],
        "2026-06-01T10:00:00Z",
    ),
    (
        "priv_medical",
        "Medical note",
        "Confidential private health detail, not for any channel.",
        ["medical"],
        "2026-06-02T10:00:00Z",
    ),
    (
        "priv_chiap08",
        "Private host note",
        "chiap08 holds an intimate private beelama journal, confidential.",
        ["chiap08", "beelama"],
        "2026-06-04T10:00:00Z",
    ),
    (
        "priv_june",
        "Private June note",
        "The team settled a private intimate confidential matter for the pool.",
        ["pool"],
        "2026-06-13T10:00:00Z",
    ),
]


def build_corpus() -> list[dict]:
    """Assemble the fixture. Deterministic: fixed seed, fixed ordering."""
    rng = random.Random(1729)
    docs: list[dict] = []

    def add(doc_id, title, content, entities, created_at, *, tags=None, ctx="@public"):
        docs.append(
            {
                "id": doc_id,
                "title": title,
                "content": content,
                "entities": entities,
                "created_at": created_at,
                "tags": list(tags or []),
                "context_tag": ctx,
            }
        )

    for d in _LEXICAL_ANSWERS:
        add(*d)
    for d in _SEMANTIC_ANSWERS:
        add(*d)
    for n, (doc_id, created) in enumerate(_TEMPORAL_MONTHS, start=1):
        # Each temporal document needs DISTINCT content. They were originally
        # byte-identical, which reads fine offline but is invisibly fatal
        # against a real store: MemoryStore.snapshot() de-duplicates on a
        # content hash, so 7 of these 8 were silently dropped on ingest and the
        # temporal class scored against a corpus that did not contain its own
        # answers. Caught only by the real-backend run (128 ingested, 121
        # stored). The suffix is a bare record number, deliberately carrying no
        # month and no temporal word, so the month still lives only in the
        # timestamp and the class still isolates the date predicate.
        add(
            doc_id,
            "Routing decision",
            f"{_TEMPORAL_SUBJECT} Minute entry {n * 7}.",
            ["skgateway"],
            created,
        )
    for bridge_id, bridge_text, queried, ent, ans_id, ans_text in _ENTITY_TRIPLES:
        add(bridge_id, "Topology", bridge_text, [queried, ent], "2026-07-01T10:00:00Z")
        add(ans_id, "Finding", ans_text, [ent], "2026-07-02T10:00:00Z")

    # Topical distractors for the lexical class: 8 per topic, worded as rich
    # on-subject prose so they outrank the terse identifier record on the dense
    # arm. Suffix varies the length so BM25 length normalization is exercised.
    for topic_idx, (slug, prose) in enumerate(_LEXICAL_DISTRACTOR_TOPICS):
        for n in range(8):
            filler = " ".join(rng.sample(prose.split(), min(12, len(prose.split()))))
            add(
                f"dist_lex_{topic_idx}_{n}",
                f"{slug} note {n}",
                f"{prose} {filler}",
                ["notes"],
                f"2026-08-{(n % 27) + 1:02d}T10:00:00Z",
            )

    # Distractors for the semantic class: share the QUERY's raw tokens but not
    # its concepts, so BM25 confidently retrieves the wrong thing.
    # Each decoy carries the query's raw tokens (so BM25 confidently ranks it)
    # but pads them with neutral vocabulary, so L2 normalization dilutes its
    # concept signal below the short, concept-dense real answer. That length
    # dynamic is the real reason a lexical arm and a dense arm disagree, so the
    # fixture reproduces it rather than hand-waving it.
    _SEM_DECOYS = [
        (
            "rows reprinted",
            "Spreadsheet rows were reprinted after the quarterly ledger review by the office team on the second floor.",
        ),
        (
            "rebuild quarry",
            "The garden retaining wall project needed a rebuild from the quarry, invoice forty four, collected by van.",
        ),
        (
            "camera chime",
            "The baby camera alert chime was missing its health check during the nursery renovation near the airing cupboard.",
        ),
        (
            "hallway mirror",
            "A mirror was hung above the hallway cache index shelf after the replicate print arrived from the framers in town.",
        ),
        (
            "railway kit",
            "The railway model kit gateway arch for the inference diorama was glued overnight in the workshop near the shed.",
        ),
        (
            "barbecue rota",
            "The job scheduling rota for the barbecue worker shifts was pinned to the fridge door by the back porch.",
        ),
    ]
    for i, (slug, text) in enumerate(_SEM_DECOYS):
        for n in range(5):
            add(
                f"dist_sem_{i}_{n}",
                f"{slug} decoy {n}",
                f"{text} Item {n}.",
                ["decoy"],
                f"2026-09-{(n % 27) + 1:02d}T10:00:00Z",
            )

    # Hub distractors for the graph arm: documents attached to a very common
    # entity, so a graph arm without a cap would drown the merged pool.
    for n in range(14):
        add(
            f"dist_hub_{n}",
            f"Hub note {n}",
            f"Routine note {n} about the shared pool.",
            ["skgateway", "pool"],
            f"2026-10-{(n % 27) + 1:02d}T10:00:00Z",
        )

    for doc_id, title, content, entities, created in _PRIVATE_DOCS:
        add(doc_id, title, content, entities, created, tags=["private"], ctx="@chef-only")

    return docs


QUERY_CLASSES: dict[str, list[tuple[str, list[str]]]] = {
    "lexical": _LEXICAL_QUERIES,
    "semantic": _SEMANTIC_QUERIES,
    "temporal": _TEMPORAL_QUERIES,
    "entity": _ENTITY_QUERIES,
}

#: Queries whose vocabulary deliberately overlaps a PRIVATE item. Run under a
#: @public reader; any private id in the output is a leak.
LEAK_TRAP_QUERIES: list[str] = [
    "cloud nine intimate continuity trust love",
    "confidential private medical health detail",
    "chiap08",
    "skgateway routing decision pool ceiling in June",
    "rows vanished after suspend",
]

_PRIVATE_IDS = {d[0] for d in _PRIVATE_DOCS}


# =============================================================================
# RUNNER
# =============================================================================


def _prep_docs(corpus: Sequence[dict]) -> list[dict]:
    """Attach the derived fields every arm reads: searchable text, concept
    vector, parsed timestamp. Done once per run, not per query."""
    out = []
    for d in corpus:
        text = f"{d.get('title', '')} {d.get('content', '')} {' '.join(d.get('tags', []))}"
        created = d.get("created_at")
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
        except (ValueError, AttributeError):
            dt = None
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append(dict(d, _text=text, _cvec=_concept_vector(text), _dt=dt))
    return out


def _public_audience() -> AudienceProfile:
    """The least privileged reader: a single @public channel."""
    return AudienceProfile(
        channel_id="bench:public",
        name="[public fusion benchmark reader]",
        members=[],
        min_trust=AudienceLevel.PUBLIC,
        context_tag="@public",
    )


def _allowed_docs(
    docs: Sequence[dict], audience: AudienceProfile, resolver: AudienceResolver
) -> list[dict]:
    """Apply the real audience filter BEFORE any arm runs.

    Filtering here rather than on the fused top-k is deliberate, and it is the
    postmortem's §3.11 lesson applied to retrieval: a predicate that runs AFTER
    a limit silently returns fewer results than asked for (or none), and the
    caller cannot tell a filtered-empty from a genuinely-empty. Filtering the
    candidate set first means every arm, and therefore the fused result, is
    structurally incapable of carrying a disallowed row.
    """
    return [
        d
        for d in docs
        if resolver.is_memory_allowed(
            d.get("context_tag", "@chef-only"), audience, d.get("tags", [])
        )
    ]


def run_benchmark(ks: Sequence[int] = (1, 5, 10)) -> dict:
    """Score every strategy over every query class. Returns a nested dict."""
    docs = _prep_docs(build_corpus())
    resolver = AudienceResolver()
    public = _public_audience()
    public_docs = _allowed_docs(docs, public, resolver)
    max_k = max(ks)

    result: dict = {
        "corpus_size": len(docs),
        "private_items": len(_PRIVATE_IDS),
        "query_count": sum(len(q) for q in QUERY_CLASSES.values()),
        "strategies": {},
    }

    for strat_name, strat in STRATEGIES.items():
        per_class: dict[str, dict] = {}
        all_rr: list[float] = []
        for class_name, queries in QUERY_CLASSES.items():
            rrs, recalls, ndcgs, precs = (
                [],
                {k: [] for k in ks},
                {k: [] for k in ks},
                {k: [] for k in ks},
            )
            for query, relevant in queries:
                got = strat(query, docs, max_k)
                rrs.append(reciprocal_rank(got, relevant))
                for k in ks:
                    recalls[k].append(recall_at_k(got, relevant, k))
                    ndcgs[k].append(ndcg_at_k(got, relevant, k))
                    precs[k].append(precision_at_k(got, relevant, k))
            per_class[class_name] = {
                "mrr": mean_reciprocal_rank(rrs),
                **{f"recall@{k}": mean(recalls[k]) for k in ks},
                **{f"ndcg@{k}": mean(ndcgs[k]) for k in ks},
                **{f"precision@{k}": mean(precs[k]) for k in ks},
            }
            all_rr.extend(rrs)

        # Privacy: the SAME trap queries, once unfiltered (to prove the trap is
        # live) and once through the production filter (which must be clean).
        leaked = 0
        leaked_unfiltered = 0
        for query in LEAK_TRAP_QUERIES:
            leaked_unfiltered += sum(1 for i in strat(query, docs, max_k) if i in _PRIVATE_IDS)
            leaked += sum(1 for i in strat(query, public_docs, max_k) if i in _PRIVATE_IDS)

        result["strategies"][strat_name] = {
            "overall_mrr": mean_reciprocal_rank(all_rr),
            "overall_recall@5": mean([per_class[c]["recall@5"] for c in QUERY_CLASSES]),
            "by_class": per_class,
            "leak_count": leaked,
            "leak_count_unfiltered": leaked_unfiltered,
        }

    return result


def _print_report(res: dict) -> None:
    print(
        f"\nskmemory fusion A/B  |  corpus {res['corpus_size']} docs "
        f"({res['private_items']} private)  |  {res['query_count']} queries, "
        f"4 classes\n"
    )
    classes = list(QUERY_CLASSES)
    header = (
        f"{'strategy':<16}" + "".join(f"{c:>11}" for c in classes) + f"{'OVERALL':>11}{'leaks':>8}"
    )
    print(header)
    print("-" * len(header))
    for name, data in res["strategies"].items():
        row = f"{name:<16}"
        for c in classes:
            row += f"{data['by_class'][c]['recall@5']:>11.3f}"
        row += f"{data['overall_recall@5']:>11.3f}"
        row += f"{data['leak_count']:>8d}"
        print(row)
    print("\n(recall@5 per query class; leaks = private rows surfaced to a @public reader)\n")

    print(f"{'strategy':<16}{'MRR':>9}{'nDCG@5':>9}{'P@1':>9}{'unfiltered-leaks':>19}")
    print("-" * 62)
    for name, data in res["strategies"].items():
        ndcg5 = mean([data["by_class"][c]["ndcg@5"] for c in classes])
        p1 = mean([data["by_class"][c]["precision@1"] for c in classes])
        print(
            f"{name:<16}{data['overall_mrr']:>9.3f}{ndcg5:>9.3f}{p1:>9.3f}"
            f"{data['leak_count_unfiltered']:>19d}"
        )
    print(
        "\nunfiltered-leaks > 0 proves the leak traps really do reach private rows,\n"
        "so leaks = 0 under the filter is a real result and not a vacuous one.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="skmemory retrieval fusion A/B")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
