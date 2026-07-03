#!/usr/bin/env python3
"""Recall/NDCG benchmark harness for skmemory's own ``MemoryStore`` API.

Ports the scoring math (``dcg`` / ``ndcg`` / recall@k) from MemPalace's
LongMemEval harness (``mempalace/benchmarks/longmemeval_bench.py``) — that
project's 9 retrieval-strategy variants, ephemeral-ChromaDB setup, and
live-Claude-API diary mode are intentionally NOT ported. This is a much
smaller thing: a self-contained, CI-friendly regression check that skmemory
retrieval quality (sanitizer/extractor/dedup tuning, etc.) hasn't regressed.

Drives the REAL ``MemoryStore`` API — ``snapshot()`` to build a small labeled
fixture corpus, then ``search()`` known-item queries and score the results.
One "raw" retrieval mode: whatever ``store.search()`` does by default (no
extra reranking/compression tricks layered on top by this harness).

By default this needs no network and no embedding server: it builds a
``MemoryStore`` on a local ``SQLiteBackend`` (keyword LIKE/AND-OR search,
fully in-process) in a throwaway temp directory. If a vector backend is
attached to the store you pass in, ``search()`` will use that instead — the
harness doesn't care which backend answers ``search()``, it just scores
whatever comes back.

Run:    python -m skmemory.eval.recall_benchmark
Import: from skmemory.eval.recall_benchmark import recall_at_k, ndcg_at_k, run_benchmark
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Iterable, Sequence
from statistics import mean

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import MemoryLayer, MemoryRole
from skmemory.store import MemoryStore

# =============================================================================
# SCORING (ported from mempalace/benchmarks/longmemeval_bench.py — dcg/ndcg/
# recall@k logic, ~30 lines, dependency-free). Interface adapted to work
# directly on ranked ID lists rather than corpus-index rankings, since
# MemoryStore.search() already returns ranked Memory objects with IDs.
# =============================================================================


def dcg(relevances: Sequence[float], k: int) -> float:
    """Discounted Cumulative Gain over the first *k* relevance scores."""
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def recall_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Fraction of *relevant_ids* found in the top-*k* of *retrieved_ids*.

    Example: recall_at_k(['a', 'b', 'c'], {'b', 'x'}, 2) == 0.5
    (top-2 = {'a', 'b'}; 1 of the 2 relevant ids — 'b' — was found).
    """
    relevant_ids = set(relevant_ids)
    if not relevant_ids:
        return 0.0
    top_k = set(retrieved_ids[:k])
    hits = len(top_k & relevant_ids)
    return hits / len(relevant_ids)


def ndcg_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Normalized DCG@k using binary relevance (id is in *relevant_ids* or not).

    Same math as MemPal's ``ndcg()``: build the binary relevance vector for
    the top-k ranking, DCG it, then normalize against the best possible
    ordering of that same set of hits (IDCG).
    """
    relevant_ids = set(relevant_ids)
    relevances = [1.0 if rid in relevant_ids else 0.0 for rid in retrieved_ids[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


# =============================================================================
# FIXTURE CORPUS — small, self-contained, no external dataset required.
# =============================================================================

# Each item: label (used to reference it from FIXTURE_QUERIES), title, content,
# tags. Content is intentionally short (< 150 chars) so the full text lands in
# SQLiteBackend's content_preview column, which is one of the columns the
# default (no-vector) keyword search matches against.
FIXTURE_MEMORIES: list[dict] = [
    {
        "label": "mxbai_server",
        "title": "mxbai embed server location",
        "content": "The mxbai-embed-large embedding server runs on 192.168.0.100 port 11434 for skmemory vector search.",
        "tags": ["embedding", "infra"],
    },
    {
        "label": "bge_legal_deprecated",
        "title": "bge-legal-v2 decommissioned",
        "content": "The bge-legal-v2 embedding model was decommissioned and removed; all embedding now uses mxbai.",
        "tags": ["embedding", "deprecated"],
    },
    {
        "label": "skmem_pg",
        "title": "skmem-pg Postgres store",
        "content": "skmem-pg is the custom Postgres image with pgvector, BM25 via pg_search, and an Apache AGE graph.",
        "tags": ["postgres", "infra"],
    },
    {
        "label": "dedup_threshold",
        "title": "Dedup similarity threshold",
        "content": "The dedup cosine similarity threshold for mxbai-embed-large was tuned to 0.73 via a labeled benchmark.",
        "tags": ["dedup", "tuning"],
    },
    {
        "label": "wal_log",
        "title": "Write-ahead log for snapshots",
        "content": "MemoryStore uses a write-ahead log at memory/wal/write_log.jsonl to make snapshot writes crash-resilient.",
        "tags": ["reliability", "wal"],
    },
    {
        "label": "sqlite_backend",
        "title": "SQLite backend text search",
        "content": "SQLiteBackend search_text tries an AND match across title, summary, content preview and tags, then falls back to OR.",
        "tags": ["search", "sqlite"],
    },
    {
        "label": "memory_layers",
        "title": "Three memory layers",
        "content": "SKMemory organizes memories into short-term, mid-term, and long-term layers with different retention policies.",
        "tags": ["layers", "architecture"],
    },
    {
        "label": "redundancy_mantra",
        "title": "Redundancy mantra",
        "content": "If you need one, get two -- always design for high availability with no single point of failure.",
        "tags": ["principle", "ha"],
    },
    {
        "label": "cloud9_protocol",
        "title": "Cloud 9 emotional continuity",
        "content": "Cloud 9 is the emotional continuity protocol at depth 9, with trust 0.97 and love 10 out of 10.",
        "tags": ["emotional", "cloud9"],
    },
    {
        "label": "skgraph_backend",
        "title": "SKGraph knowledge graph backend",
        "content": "SKGraph indexes memory relationships into a graph backend such as FalkorDB or Apache AGE for traversal.",
        "tags": ["graph", "backend"],
    },
    {
        "label": "fortress_seal",
        "title": "Fortress tamper sealing",
        "content": "FortifiedMemoryStore seals memories with an integrity hash so tampering can be detected on recall.",
        "tags": ["security", "integrity"],
    },
    {
        "label": "journal_ritual",
        "title": "Daily ritual and journal",
        "content": "The skmemory ritual loads the soul, FEB emotional state, the journal, and the strongest memories for context.",
        "tags": ["ritual", "journal"],
    },
]

# Known-item queries: (query text, labels of the memories that should rank
# well for this query). Each query is written using distinctive vocabulary
# pulled from exactly one fixture memory so keyword search can find it.
FIXTURE_QUERIES: list[tuple[str, list[str]]] = [
    ("mxbai embedding server port", ["mxbai_server"]),
    ("bge-legal decommissioned removed", ["bge_legal_deprecated"]),
    ("skmem-pg Postgres pgvector BM25", ["skmem_pg"]),
    ("dedup similarity threshold tuned", ["dedup_threshold"]),
    ("write-ahead log snapshot crash-resilient", ["wal_log"]),
    ("SQLiteBackend search_text AND OR fallback", ["sqlite_backend"]),
    ("short-term mid-term long-term layers", ["memory_layers"]),
    ("redundancy mantra single point of failure", ["redundancy_mantra"]),
]


def _build_default_store() -> MemoryStore:
    """A throwaway, fully local MemoryStore: SQLiteBackend only, no vector
    backend, no network. Good enough to run the harness with zero external
    dependencies (SQLite keyword search is the "raw" retrieval mode)."""
    base_path = tempfile.mkdtemp(prefix="skmemory_recall_bench_")
    return MemoryStore(primary=SQLiteBackend(base_path=base_path), vector=None)


def _load_fixture_corpus(store: MemoryStore) -> dict[str, str]:
    """snapshot() every FIXTURE_MEMORIES item into *store*, return label -> id."""
    label_to_id: dict[str, str] = {}
    for item in FIXTURE_MEMORIES:
        memory = store.snapshot(
            title=item["title"],
            content=item["content"],
            layer=MemoryLayer.LONG,
            role=MemoryRole.GENERAL,
            tags=item.get("tags", []),
            source="recall-benchmark-fixture",
        )
        label_to_id[item["label"]] = memory.id
    return label_to_id


def run_benchmark(
    store: MemoryStore | None = None,
    k_values: tuple[int, ...] = (1, 3, 5),
) -> dict:
    """Run the fixture corpus + known-item queries against *store* (or a
    fresh local one if not given) and score recall@k / NDCG@k.

    Args:
        store: A MemoryStore to snapshot the fixture into and search. If
            None, a throwaway local SQLiteBackend-only store is created
            (no network required).
        k_values: The k cutoffs to report recall/NDCG at.

    Returns:
        dict with "k_values", "num_queries", "num_fixture_memories",
        "per_query" (list of per-query result rows), and "aggregate"
        (mean recall@k / ndcg@k across all queries).
    """
    if store is None:
        store = _build_default_store()

    label_to_id = _load_fixture_corpus(store)
    max_k = max(k_values)

    per_query: list[dict] = []
    for query_text, relevant_labels in FIXTURE_QUERIES:
        relevant_ids = {label_to_id[label] for label in relevant_labels}
        results = store.search(query_text, limit=max_k)
        retrieved_ids = [m.id for m in results]

        row: dict = {
            "query": query_text,
            "relevant_ids": sorted(relevant_ids),
            "retrieved_ids": retrieved_ids,
        }
        for k in k_values:
            row[f"recall@{k}"] = recall_at_k(retrieved_ids, relevant_ids, k)
            row[f"ndcg@{k}"] = ndcg_at_k(retrieved_ids, relevant_ids, k)
        per_query.append(row)

    aggregate: dict = {}
    for k in k_values:
        aggregate[f"recall@{k}"] = mean(row[f"recall@{k}"] for row in per_query)
        aggregate[f"ndcg@{k}"] = mean(row[f"ndcg@{k}"] for row in per_query)

    return {
        "k_values": list(k_values),
        "num_queries": len(per_query),
        "num_fixture_memories": len(label_to_id),
        "per_query": per_query,
        "aggregate": aggregate,
    }


def _print_report(result: dict) -> None:
    k_values = result["k_values"]
    print(f"skmemory recall benchmark — {result['num_queries']} known-item queries, "
          f"{result['num_fixture_memories']} fixture memories\n")

    header = "query".ljust(44) + "".join(f"recall@{k}".rjust(11) for k in k_values) + \
        "".join(f"ndcg@{k}".rjust(11) for k in k_values)
    print(header)
    print("-" * len(header))
    for row in result["per_query"]:
        line = row["query"][:42].ljust(44)
        line += "".join(f"{row[f'recall@{k}']:.2f}".rjust(11) for k in k_values)
        line += "".join(f"{row[f'ndcg@{k}']:.2f}".rjust(11) for k in k_values)
        print(line)

    print("-" * len(header))
    agg = result["aggregate"]
    line = "AGGREGATE (mean)".ljust(44)
    line += "".join(f"{agg[f'recall@{k}']:.2f}".rjust(11) for k in k_values)
    line += "".join(f"{agg[f'ndcg@{k}']:.2f}".rjust(11) for k in k_values)
    print(line)


def main() -> None:
    result = run_benchmark()
    _print_report(result)


if __name__ == "__main__":
    main()
