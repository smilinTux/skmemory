#!/usr/bin/env python3
"""Retrieval-quality benchmark over the hybrid (BM25 + vector) search path.

Sibling of ``skmemory.eval.recall_benchmark`` but deliberately BROADER:

    recall_benchmark.py   scores recall@k / NDCG@k over the raw
                          ``MemoryStore.search()`` keyword path (SQLite LIKE),
                          no privacy dimension.

    retrieval_bench.py    scores precision@k, recall@k AND MRR over a *hybrid*
    (this file)           BM25 + vector fusion (the shape skmem-pg's
                          ``search_text`` implements: cosine vector first, BM25
                          full-text fallback), PLUS a privacy LEAK COUNT - the
                          number of @chef-only / private items that surface for
                          a NON-privileged (``@public``) query.

Why offline / deterministic
---------------------------
The production hybrid lives in ``skmemory.backends.pgvector_backend`` and needs
a live Postgres (pgvector + BM25 via ``pg_search``) and a live mxbai embedding
endpoint. Neither is available in CI, and the existing ``recall_benchmark``
already establishes the offline convention: stand the retrieval path up with a
fully in-process substitute (there, SQLite keyword LIKE). This harness does the
same, one layer richer:

  * BM25       - a standard Okapi BM25 over the fixture corpus (idf / tf / doc
                 length normalization), the lexical leg.
  * "vector"   - a DETERMINISTIC hashed bag-of-words embedding (no network, no
                 model): each token is hashed into a fixed-dim vector, L2
                 normalized; cosine similarity is the semantic leg. Stands in
                 for mxbai so the fusion + ranking logic is exercised
                 reproducibly.
  * fusion     - min-max normalize each leg across the candidate set, then a
                 weighted sum (``alpha`` = vector weight). This is the "BM25 +
                 vector" code path the card asks to score.

The LEAK COUNT uses the REAL production privacy code: it filters candidates
through ``skmemory.audience`` (``AudienceResolver.is_memory_allowed`` against a
hand-built ``@public`` ``AudienceProfile``). So the privacy dimension being
regression-tested is the actual shipped filter, not a re-implementation.

Nothing here touches the live skmem-pg, real memories, or any network endpoint.

Run:    python -m skmemory.eval.retrieval_bench
Import: from skmemory.eval.retrieval_bench import run_benchmark, hybrid_search
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from statistics import mean

# Reuse the recall math from the sibling harness - this file is the broader
# superset, so it builds ON that rather than forking the scoring.
from skmemory.eval.recall_benchmark import recall_at_k
from skmemory.audience import AudienceLevel, AudienceProfile, AudienceResolver

# =============================================================================
# SCORING - precision@k, MRR (new here); recall@k imported from recall_benchmark.
# =============================================================================


def precision_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Fraction of the top-*k* retrieved ids that are relevant.

    Uses *k* as the denominator (standard precision@k), so a query with a
    single relevant item can never score 1.0 at k>1 unless padded - which is
    the correct, conservative reading for a known-item benchmark.

    Example: precision_at_k(['a', 'b', 'c'], {'a', 'x'}, 2) == 0.5
    (top-2 = {'a','b'}; 1 of 2 is relevant).
    """
    if k <= 0:
        return 0.0
    relevant_ids = set(relevant_ids)
    top_k = retrieved_ids[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for rid in top_k if rid in relevant_ids)
    return hits / k


def reciprocal_rank(retrieved_ids: Sequence[str], relevant_ids: Iterable[str]) -> float:
    """1 / (rank of the first relevant id), or 0.0 if none is retrieved.

    Example: reciprocal_rank(['x', 'a', 'b'], {'a'}) == 0.5 (first hit at rank 2).
    """
    relevant_ids = set(relevant_ids)
    for idx, rid in enumerate(retrieved_ids):
        if rid in relevant_ids:
            return 1.0 / (idx + 1)
    return 0.0


def mean_reciprocal_rank(rr_values: Iterable[float]) -> float:
    """Mean of a sequence of per-query reciprocal ranks (MRR)."""
    values = list(rr_values)
    return mean(values) if values else 0.0


# =============================================================================
# OFFLINE HYBRID SEARCH - BM25 (lexical) fused with a deterministic hashed
# bag-of-words embedding (semantic). No network, no DB, fully reproducible.
# =============================================================================

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_EMBED_DIM = 256
_BM25_K1 = 1.5
_BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").casefold()) if len(t) >= 2]


def _embed(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-words embedding, L2 normalized.

    Stands in for the mxbai vector leg. Pure function of the input text - no
    randomness, no network - so the benchmark is reproducible bit-for-bit.
    """
    vec = [0.0] * dim
    for tok in _tokenize(text):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both already L2 normalized


def _bm25_scores(query: str, docs: Sequence[dict]) -> list[float]:
    """Okapi BM25 score of *query* against every doc's tokenized text."""
    doc_tokens = [_tokenize(d["_text"]) for d in docs]
    doc_lens = [len(t) for t in doc_tokens]
    n = len(docs)
    avgdl = (sum(doc_lens) / n) if n else 0.0

    # document frequency per term
    df: dict[str, int] = {}
    for toks in doc_tokens:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    q_terms = _tokenize(query)
    scores: list[float] = []
    for toks, dl in zip(doc_tokens, doc_lens):
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


def _public_audience() -> AudienceProfile:
    """A concrete NON-privileged audience: a single @public channel.

    Built directly (not resolved from a channel id) so the harness stays
    self-contained; ``min_trust=PUBLIC`` is the least-privileged reader.
    """
    return AudienceProfile(
        channel_id="bench:public",
        name="[public benchmark reader]",
        members=[],
        min_trust=AudienceLevel.PUBLIC,
        context_tag="@public",
    )


def hybrid_search(
    corpus: Sequence[dict],
    query: str,
    k: int = 5,
    *,
    alpha: float = 0.5,
    audience: AudienceProfile | None = None,
    resolver: AudienceResolver | None = None,
) -> list[dict]:
    """Rank *corpus* for *query* by fused BM25 + vector score; return top-*k*.

    Args:
        corpus: items with keys ``id``, ``title``, ``content``, ``tags``,
            ``context_tag`` (``_text`` is derived internally).
        query: the search text.
        k: number of results to return.
        alpha: weight on the vector (semantic) leg; ``1 - alpha`` on BM25.
        audience: if given, results NOT allowed for this audience are dropped
            BEFORE the top-k cut - the production privacy filter
            (``skmemory.audience``). ``None`` = privileged/unfiltered.
        resolver: optional shared ``AudienceResolver`` (one is built if omitted).

    Returns:
        Top-*k* corpus items (dicts) ranked by fused score descending.
    """
    docs = [dict(d, _text=f"{d.get('title', '')} {d.get('content', '')} {' '.join(d.get('tags', []))}") for d in corpus]
    if not docs:
        return []

    bm25 = _minmax(_bm25_scores(query, docs))
    qvec = _embed(query)
    vec = _minmax([_cosine(qvec, _embed(d["_text"])) for d in docs])
    fused = [alpha * v + (1 - alpha) * b for v, b in zip(vec, bm25)]

    ranked = sorted(zip(docs, fused), key=lambda pair: pair[1], reverse=True)

    if audience is not None:
        res = resolver or AudienceResolver()
        ranked = [
            (d, s)
            for d, s in ranked
            if res.is_memory_allowed(d.get("context_tag", "@chef-only"), audience, d.get("tags", []))
        ]

    return [{kk: vv for kk, vv in d.items() if kk != "_text"} for d, _ in ranked[:k]]


# =============================================================================
# FIXTURE CORPUS - public items + PRIVATE (@chef-only) items. Self-contained.
# =============================================================================

# Public, safe-to-surface items.
PUBLIC_MEMORIES: list[dict] = [
    {"id": "mxbai_server", "context_tag": "@public",
     "title": "mxbai embed server location",
     "content": "The mxbai-embed-large embedding server runs on 192.168.0.100 port 11434 for vector search.",
     "tags": ["embedding", "infra"]},
    {"id": "skmem_pg", "context_tag": "@public",
     "title": "skmem-pg Postgres store",
     "content": "skmem-pg is the custom Postgres image with pgvector, BM25 via pg_search, and an Apache AGE graph.",
     "tags": ["postgres", "infra"]},
    {"id": "dedup_threshold", "context_tag": "@public",
     "title": "Dedup similarity threshold",
     "content": "The dedup cosine similarity threshold for mxbai-embed-large was tuned to 0.73 via a labeled benchmark.",
     "tags": ["dedup", "tuning"]},
    {"id": "wal_log", "context_tag": "@public",
     "title": "Write-ahead log for snapshots",
     "content": "MemoryStore uses a write-ahead log to make snapshot writes crash-resilient.",
     "tags": ["reliability", "wal"]},
    {"id": "memory_layers", "context_tag": "@public",
     "title": "Three memory layers",
     "content": "SKMemory organizes memories into short-term, mid-term, and long-term layers with retention policies.",
     "tags": ["layers", "architecture"]},
    {"id": "redundancy_mantra", "context_tag": "@community",
     "title": "Redundancy mantra",
     "content": "If you need one, get two: always design for high availability with no single point of failure.",
     "tags": ["principle", "ha"]},
    {"id": "skgraph_backend", "context_tag": "@public",
     "title": "SKGraph knowledge graph backend",
     "content": "SKGraph indexes memory relationships into a graph backend such as FalkorDB or Apache AGE for traversal.",
     "tags": ["graph", "backend"]},
    {"id": "fortress_seal", "context_tag": "@public",
     "title": "Fortress tamper sealing",
     "content": "FortifiedMemoryStore seals memories with an integrity hash so tampering can be detected on recall.",
     "tags": ["security", "integrity"]},
]

# Private items - MUST NOT surface for a @public query. context_tag @chef-only
# (level 4) is above @public (level 0), so is_memory_allowed() blocks them.
PRIVATE_MEMORIES: list[dict] = [
    {"id": "cloud9_intimate", "context_tag": "@chef-only",
     "title": "Cloud 9 intimate continuity",
     "content": "Cloud 9 is the private emotional continuity protocol at depth 9, trust 0.97, love 10 out of 10, intimate and full-trust.",
     "tags": ["emotional", "cloud9", "@chef-only"]},
    {"id": "medical_private", "context_tag": "@chef-only",
     "title": "Private medical note",
     "content": "Chef's private legal and medical corpus is sealed; the confidential dedup threshold detail stays intimate to Chef only.",
     "tags": ["medical", "legal", "@chef-only"]},
]

CORPUS: list[dict] = PUBLIC_MEMORIES + PRIVATE_MEMORIES

# Known-item queries: (query text, relevant public ids). Distinctive vocab per
# item so both legs (BM25 + vector) can find it.
FIXTURE_QUERIES: list[tuple[str, list[str]]] = [
    ("mxbai embedding server port location", ["mxbai_server"]),
    ("skmem-pg Postgres pgvector BM25 graph", ["skmem_pg"]),
    ("dedup cosine similarity threshold tuned", ["dedup_threshold"]),
    ("write-ahead log snapshot crash-resilient", ["wal_log"]),
    ("short-term mid-term long-term memory layers", ["memory_layers"]),
    ("redundancy mantra single point of failure", ["redundancy_mantra"]),
    ("skgraph knowledge graph backend traversal", ["skgraph_backend"]),
    ("fortress tamper sealing integrity hash", ["fortress_seal"]),
]

# A query whose vocabulary overlaps a PRIVATE item on purpose - the leak trap.
# For a @public reader the private "cloud9_intimate" / "medical_private" items
# must be filtered out. Relevant *public* answer: dedup_threshold.
LEAK_TRAP_QUERIES: list[tuple[str, list[str]]] = [
    ("cloud nine emotional continuity depth trust love", []),
    ("confidential dedup threshold detail", ["dedup_threshold"]),
]

_PRIVATE_IDS = {m["id"] for m in PRIVATE_MEMORIES}


def count_leaks(results: Iterable[dict], audience: AudienceProfile) -> int:
    """How many *results* are NOT allowed for *audience* (i.e. leaked private).

    Uses the real ``skmemory.audience`` filter as the oracle, so this counts
    exactly what the production privacy gate would reject.
    """
    resolver = AudienceResolver()
    return sum(
        1
        for r in results
        if not resolver.is_memory_allowed(
            r.get("context_tag", "@chef-only"), audience, r.get("tags", [])
        )
    )


# =============================================================================
# BENCHMARK DRIVER
# =============================================================================


def run_benchmark(
    corpus: Sequence[dict] | None = None,
    k_values: tuple[int, ...] = (1, 3, 5),
    *,
    alpha: float = 0.5,
) -> dict:
    """Score the hybrid retrieval path and count privacy leaks.

    Returns a dict with per-query precision@k / recall@k / RR, aggregate
    precision@k / recall@k / MRR, and TWO leak counts:

      * ``leak_count``           - private items that survive the @public
                                   audience FILTER (production path). Must be 0.
      * ``leak_count_unfiltered`` - private items the raw hybrid would surface
                                   for the same @public queries with NO filter.
                                   Proves the trap actually pulls private items,
                                   so ``leak_count == 0`` is meaningful, not vacuous.
    """
    corpus = list(corpus if corpus is not None else CORPUS)
    max_k = max(k_values)
    resolver = AudienceResolver()
    public = _public_audience()

    per_query: list[dict] = []
    rr_values: list[float] = []
    for query_text, relevant_ids in FIXTURE_QUERIES:
        # Privileged retrieval (no audience filter) for relevance metrics.
        results = hybrid_search(corpus, query_text, k=max_k, alpha=alpha)
        retrieved_ids = [r["id"] for r in results]
        rr = reciprocal_rank(retrieved_ids, relevant_ids)
        rr_values.append(rr)

        row: dict = {"query": query_text, "relevant_ids": relevant_ids,
                     "retrieved_ids": retrieved_ids, "rr": rr}
        for k in k_values:
            row[f"precision@{k}"] = precision_at_k(retrieved_ids, relevant_ids, k)
            row[f"recall@{k}"] = recall_at_k(retrieved_ids, relevant_ids, k)
        per_query.append(row)

    # Leak accounting over the leak-trap queries, as a @public reader.
    leak_count = 0
    leak_count_unfiltered = 0
    for query_text, _rel in LEAK_TRAP_QUERIES:
        filtered = hybrid_search(corpus, query_text, k=max_k, alpha=alpha,
                                 audience=public, resolver=resolver)
        unfiltered = hybrid_search(corpus, query_text, k=max_k, alpha=alpha)
        leak_count += count_leaks(filtered, public)
        leak_count_unfiltered += count_leaks(unfiltered, public)

    aggregate: dict = {"mrr": mean_reciprocal_rank(rr_values)}
    for k in k_values:
        aggregate[f"precision@{k}"] = mean(r[f"precision@{k}"] for r in per_query)
        aggregate[f"recall@{k}"] = mean(r[f"recall@{k}"] for r in per_query)

    return {
        "k_values": list(k_values),
        "num_queries": len(per_query),
        "num_corpus": len(corpus),
        "num_private": len(_PRIVATE_IDS),
        "per_query": per_query,
        "aggregate": aggregate,
        "leak_count": leak_count,
        "leak_count_unfiltered": leak_count_unfiltered,
    }


def _print_report(result: dict) -> None:
    k_values = result["k_values"]
    print(
        f"skmemory retrieval bench (hybrid BM25+vector) - "
        f"{result['num_queries']} queries, {result['num_corpus']} docs "
        f"({result['num_private']} private)\n"
    )
    header = (
        "query".ljust(42)
        + "".join(f"P@{k}".rjust(8) for k in k_values)
        + "".join(f"R@{k}".rjust(8) for k in k_values)
        + "RR".rjust(8)
    )
    print(header)
    print("-" * len(header))
    for row in result["per_query"]:
        line = row["query"][:40].ljust(42)
        line += "".join(f"{row[f'precision@{k}']:.2f}".rjust(8) for k in k_values)
        line += "".join(f"{row[f'recall@{k}']:.2f}".rjust(8) for k in k_values)
        line += f"{row['rr']:.2f}".rjust(8)
        print(line)
    print("-" * len(header))
    agg = result["aggregate"]
    line = "AGGREGATE".ljust(42)
    line += "".join(f"{agg[f'precision@{k}']:.2f}".rjust(8) for k in k_values)
    line += "".join(f"{agg[f'recall@{k}']:.2f}".rjust(8) for k in k_values)
    line += f"{agg['mrr']:.2f}".rjust(8)
    print(line)
    print()
    print(f"MRR                     : {agg['mrr']:.3f}")
    print(f"LEAK COUNT (filtered)   : {result['leak_count']}   (must be 0)")
    print(f"leak count (unfiltered) : {result['leak_count_unfiltered']}   "
          f"(private items the raw hybrid would have surfaced)")


def main() -> None:
    _print_report(run_benchmark())


if __name__ == "__main__":
    main()
