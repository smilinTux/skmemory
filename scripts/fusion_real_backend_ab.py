#!/usr/bin/env python3
"""Real-backend A/B for retrieval fusion (R1), against skmem-pg + mxbai.

Why this exists
---------------
``skmemory.eval.fusion_bench`` is deterministic and offline, which is what
makes it a CI gate, but every arm in it is a stand-in: the dense leg is a
hashed bag-of-concepts, not mxbai, and the lexical leg is an in-process BM25,
not ParadeDB. So the committed numbers prove the FUSION LOGIC is sound, not
that the win survives contact with the real backends.

Postmortem 2026-10-16 §3.12 is the reason that distinction is not academic:
PR #37's stub guard was real, tested, merged, and never executed in production
for a single day, because the file cron actually ran was a symlink into a
different repo on an unmerged branch. "Tested" and "works where it runs" are
different claims. This script makes the second one.

What it does
------------
Ingests the SAME 128-document corpus and runs the SAME 24 queries as the
offline harness, but through a real ``MemoryStore`` on a real
``PGVectorBackend`` (pgvector + ParadeDB BM25) with real mxbai-embed-large
embeddings, and scores:

    legacy   SKMEMORY_SEARCH_FUSION=off, i.e. vector.search_text() with a
             fall-through to primary.search_text() when it returns empty
    fused    SKMEMORY_SEARCH_FUSION=on, the R1 path

Isolation
---------
Writes to a THROWAWAY database (``skmemory_fusionbench`` by default) under a
throwaway agent, never the production ``skmemory`` database and never an agent
with real memories. That is postmortem §3.9's lesson (roughly 536 of Lumina's
real memories turned out to be test fixtures a consuming repo's test suite
wrote into the live store) applied before the fact rather than after.

Run:
    python scripts/fusion_real_backend_ab.py --dsn postgresql://...  [--keep]
"""

from __future__ import annotations

import argparse
import os
import sys
from statistics import mean

# The bench corpus and queries are the single source of truth for both runs.
from skmemory.eval.fusion_bench import QUERY_CLASSES, build_corpus
from skmemory.eval.recall_benchmark import ndcg_at_k, recall_at_k
from skmemory.eval.retrieval_bench import (
    mean_reciprocal_rank,
    precision_at_k,
    reciprocal_rank,
)

DEFAULT_DB = "skmemory_fusionbench"
BENCH_AGENT = "fusionbench"


def _resolve_dsn(explicit: str | None) -> str:
    if explicit:
        return explicit
    base = os.environ.get("SKMEMORY_PG_DSN", "")
    if not base:
        sys.exit("no DSN: pass --dsn or set SKMEMORY_PG_DSN")
    # Swap only the database name, so credentials and host come from the env
    # and never from this file or from argv.
    head, _, _tail = base.rpartition("/")
    return f"{head}/{DEFAULT_DB}"


def _guard_dsn(dsn: str) -> None:
    """Refuse to run against anything that is not the throwaway database.

    A benchmark that ingests 128 synthetic documents must not be one
    fat-fingered flag away from writing them into the production store.
    """
    db = dsn.rpartition("/")[2].split("?")[0]
    if db != DEFAULT_DB:
        sys.exit(
            f"refusing to run against database {db!r}: this script only runs "
            f"against the throwaway {DEFAULT_DB!r}. Create it first (see the "
            f"module docstring) rather than pointing this at real data."
        )
    for marker in ("192.168.", "chiap", "noroc", "tail"):
        if marker in dsn:
            sys.exit(
                f"refusing a non-local DSN (contains {marker!r}): skmem-pg is "
                f"per-node by design (prb-6f069c5e) and this benchmark writes."
            )


def bootstrap_agent_home() -> str:
    """Create a throwaway agent home and register its memory profile.

    ``SKCAPSTONE_HOME`` must already point somewhere disposable; this refuses to
    run otherwise, so the benchmark cannot provision itself inside the real
    ``~/.skcapstone`` tree. The SKPM-PROF-01 reader fails closed, so a bench
    agent with no registered profile cannot open a backend at all, which is the
    guard working as designed and not something to route around.
    """
    home = os.environ.get("SKCAPSTONE_HOME", "")
    if not home:
        sys.exit("set SKCAPSTONE_HOME to a throwaway directory before running")
    real = os.path.expanduser("~/.skcapstone")
    if os.path.realpath(home) == os.path.realpath(real):
        sys.exit(
            f"refusing to provision a benchmark agent inside the real {real}: "
            f"point SKCAPSTONE_HOME at a throwaway directory"
        )

    from pathlib import Path

    from skmemory.profile_registry import sync_profile_registry

    base = Path(home) / "agents" / BENCH_AGENT
    for sub in ("config", "memory/short-term", "memory/mid-term", "memory/long-term", "logs"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    cfg = base / "config" / "skmemory.yaml"
    if not cfg.exists():
        cfg.write_text(f"agent: {BENCH_AGENT}\n", encoding="utf-8")

    written = sync_profile_registry(home, [BENCH_AGENT], agents_base=Path(home) / "agents")
    print(f"registered throwaway profile(s): {written}")
    return home


def ingest(store, corpus) -> dict[str, str]:
    """Write the corpus and return {bench id -> real memory id}."""
    mapping: dict[str, str] = {}
    for i, doc in enumerate(corpus, start=1):
        mem = store.snapshot(
            title=doc["title"],
            content=doc["content"],
            tags=list(doc.get("tags") or []) + [f"benchid:{doc['id']}"],
            source="fusion-real-ab",
        )
        mapping[doc["id"]] = mem.id
        if i % 25 == 0:
            print(f"    ingested {i}/{len(corpus)}", flush=True)
    return mapping


def score(store, mapping, ks=(1, 5, 10)) -> dict:
    """Score the shared query set, broken out by class."""
    inverse = {v: k for k, v in mapping.items()}
    max_k = max(ks)
    per_class: dict[str, dict] = {}
    all_rr: list[float] = []

    for class_name, queries in QUERY_CLASSES.items():
        rrs: list[float] = []
        recalls = {k: [] for k in ks}
        ndcgs = {k: [] for k in ks}
        precs = {k: [] for k in ks}
        for query, relevant in queries:
            results = store.search(query, limit=max_k) or []
            # Map real ids back to bench ids so scoring is identical to offline.
            got = [inverse.get(getattr(m, "id", ""), "") for m in results]
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

    return {
        "by_class": per_class,
        "overall_mrr": mean_reciprocal_rank(all_rr),
        "overall_recall@5": mean([per_class[c]["recall@5"] for c in QUERY_CLASSES]),
        "overall_ndcg@5": mean([per_class[c]["ndcg@5"] for c in QUERY_CLASSES]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None, help=f"defaults to SKMEMORY_PG_DSN with db swapped to {DEFAULT_DB}")
    ap.add_argument("--keep", action="store_true", help="leave the ingested rows in place")
    args = ap.parse_args()

    dsn = _resolve_dsn(args.dsn)
    _guard_dsn(dsn)
    redacted = dsn.replace(dsn.partition("://")[2].partition("@")[0], "***")
    print(f"DSN: {redacted}")

    os.environ["SKMEMORY_PG_DSN"] = dsn
    os.environ["SKAGENT"] = BENCH_AGENT

    bootstrap_agent_home()

    from skmemory.backends.pgvector_backend import PGVectorBackend
    from skmemory.store import MemoryStore

    vector = PGVectorBackend(agent=BENCH_AGENT, dsn=dsn)
    store = MemoryStore(vector=vector)

    corpus = build_corpus()
    print(f"\ningesting {len(corpus)} documents (real mxbai embeddings) ...")
    mapping = ingest(store, corpus)

    embedded = vector  # named for the report below
    print(f"    done, {len(mapping)} ids mapped\n")

    results = {}
    for label, flag in (("legacy", "off"), ("fused", "on")):
        os.environ["SKMEMORY_SEARCH_FUSION"] = flag
        print(f"scoring {label} (SKMEMORY_SEARCH_FUSION={flag}) ...", flush=True)
        results[label] = score(store, mapping)

    classes = list(QUERY_CLASSES)
    print("\n" + "=" * 78)
    print("REAL BACKEND A/B  (skmem-pg: pgvector + ParadeDB BM25, mxbai-embed-large)")
    print("=" * 78)
    header = f"{'path':<10}" + "".join(f"{c:>11}" for c in classes) + f"{'OVERALL':>11}{'MRR':>9}"
    print(header)
    print("-" * len(header))
    for label in ("legacy", "fused"):
        r = results[label]
        row = f"{label:<10}"
        for c in classes:
            row += f"{r['by_class'][c]['recall@5']:>11.3f}"
        row += f"{r['overall_recall@5']:>11.3f}{r['overall_mrr']:>9.3f}"
        print(row)
    print("\n(recall@5 per query class)\n")

    old, new = results["legacy"], results["fused"]
    regressed = [
        c for c in classes
        if new["by_class"][c]["recall@5"] < old["by_class"][c]["recall@5"] - 1e-9
    ]
    print(f"overall recall@5: {old['overall_recall@5']:.3f} -> {new['overall_recall@5']:.3f}")
    print(f"overall MRR:      {old['overall_mrr']:.3f} -> {new['overall_mrr']:.3f}")
    print(f"overall nDCG@5:   {old['overall_ndcg@5']:.3f} -> {new['overall_ndcg@5']:.3f}")
    print(f"regressed classes: {regressed or 'none'}")

    if not args.keep:
        print("\ncleaning up ingested rows ...")
        with embedded._connection() as conn:  # noqa: SLF001  (benchmark teardown)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE agent=%s", (BENCH_AGENT,))
                deleted = cur.rowcount
            conn.commit()
        print(f"    deleted {deleted} rows for agent={BENCH_AGENT}")

    sys.exit(1 if regressed else 0)


if __name__ == "__main__":
    main()
