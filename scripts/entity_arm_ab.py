#!/usr/bin/env python3
"""READ-ONLY A/B for the GRAPH retrieval arm, on a real agent's live corpus.

Why a separate harness from ``scripts/real_corpus_ab.py``
---------------------------------------------------------
That one is known-item retrieval: one memory is the answer, so hit@k and MRR
are the right measures. Entity queries are not known-item. Measured on lumina's
graph, 91 of 120 sampled entity names appear in 21 or more memories and only 5
appear in 1 to 5, so "did the one right memory come back" is meaningless here.
The question for an entity query is instead: of what came back, how much is
actually about the entity I asked for. That is precision@k, and it needs its
own harness rather than a bolted-on special case.

Avoiding a circular benchmark
-----------------------------
The obvious ground truth is the graph itself: ask it which memories mention an
entity, then check whether the graph arm returns them. That measures nothing,
because the oracle and the retriever are the same component. A graph arm scored
against graph labels is guaranteed to win and guaranteed to teach us nothing.

So the labels come from the SOURCE OF TRUTH instead: a memory is relevant to
entity E when its own stored content actually contains E (case-insensitive).
The graph supplies only the candidate query STRINGS, never the labels, and it
then has to earn its score against the text. That also means the graph can be
penalised for being stale or wrong, which is the whole point of measuring it:
14 of those 120 sampled entity names appear in NO memory content at all, so the
projection already holds entities whose surface form is not in the text.

What it compares
----------------
    legacy            PGVectorBackend.search_text, the pre-R1 vector-first
                      chain that only reaches its lexical stages when the
                      vector stage returns zero rows
    fused             semantic + lexical arms, fused (R1), reranked (R2)
    fused + graph     the same, plus the one-hop MENTIONS arm

Reported per configuration: precision@5 over all entity queries, plus hit@5 and
MRR over the RARE subset (relevant set of 5 or fewer) where known-item scoring
is still meaningful, with that subset's size printed so a small n is visible
rather than hidden.

The other two query classes from the known-item harness are deliberately NOT
re-measured here. The risk of adding an arm is head dilution, and that risk is
measured where it shows up, in ``scripts/real_corpus_ab.py``.

STRICTLY READ-ONLY: SELECT statements and search paths only. Never constructs a
write path, and refuses a non-local DSN.

Run:
    python scripts/entity_arm_ab.py --agent lumina --queries 40
"""

from __future__ import annotations

import argparse
import os
import sys
from statistics import mean

MIN_ENTITY_LEN = 4
#: A relevant set at or below this size still supports known-item scoring.
RARE_MAX = 5


def _redact(dsn: str) -> str:
    creds = dsn.partition("://")[2].partition("@")[0]
    return dsn.replace(creds, "***") if creds else dsn


def _guard_local(dsn: str) -> None:
    """skmem-pg is per-node by design (prb-6f069c5e); a benchmark has no
    business reaching another node's store."""
    for marker in ("192.168.", "chiap", "noroc", "tail", ".ts.net"):
        if marker in dsn:
            sys.exit(f"refusing a non-local DSN (contains {marker!r})")


def precision_at_k(got: list[str], relevant: set[str], k: int) -> float:
    top = got[:k]
    if not top:
        return 0.0
    return sum(1 for i in top if i in relevant) / len(top)


def hit_at_k(got: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(i in relevant for i in got[:k]) else 0.0


def reciprocal_rank(got: list[str], relevant: set[str]) -> float:
    for idx, item in enumerate(got, start=1):
        if item in relevant:
            return 1.0 / idx
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="read-only A/B for the graph arm")
    ap.add_argument("--agent", default=os.environ.get("SKAGENT", "lumina"))
    ap.add_argument("--queries", type=int, default=40, help="entity queries to score")
    ap.add_argument("--candidates", type=int, default=900, help="entity names to sample")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    dsn = os.environ.get("SKMEMORY_PG_DSN", "")
    if not dsn:
        sys.exit("set SKMEMORY_PG_DSN")
    _guard_local(dsn)
    print(f"DSN:   {_redact(dsn)}")
    print(f"agent: {args.agent}  (READ-ONLY)")

    import psycopg

    from skmemory.backends.age_backend import AGEGraphBackend
    from skmemory.backends.pgvector_backend import PGVectorBackend
    from skmemory.fusion import DEFAULT_ARM_CAP, fuse, run_arm
    from skmemory.rerank import RareTermReranker

    vector = PGVectorBackend(agent=args.agent, dsn=dsn)
    graph = AGEGraphBackend(dsn=dsn, agent=args.agent)
    if graph.graph is None:
        sys.exit("no graph available for this agent")
    print(f"graph: {graph.graph}")

    # Candidate query strings from the graph. These are QUERIES, never labels.
    rows = graph._cypher(  # noqa: SLF001  (benchmark reaching for a raw query)
        "MATCH (e:Entity) RETURN e.name LIMIT $limit",
        {"limit": int(args.candidates)},
        cols="name agtype",
    )
    names = []
    seen = set()
    for row in rows:
        name = str(row[0]).strip('"').strip()
        key = name.casefold()
        if len(name) >= MIN_ENTITY_LEN and key not in seen:
            seen.add(key)
            names.append(name)
    print(f"candidate entity names: {len(names)}")

    # Labels from the SOURCE OF TRUTH: the memory's own stored content.
    queries: list[tuple[str, set[str], int]] = []
    unmentioned = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for name in names:
            cur.execute(
                "SELECT id FROM memories WHERE agent=%s AND content ILIKE %s LIMIT 500",
                (args.agent, f"%{name}%"),
            )
            relevant = {str(r[0]) for r in cur.fetchall()}
            if not relevant:
                unmentioned += 1
                continue
            queries.append((name, relevant, len(relevant)))
            if len(queries) >= args.queries:
                break

    print(f"scored entity queries: {len(queries)}")
    print(f"entity names in the graph that appear in NO memory content: {unmentioned}")
    rare = [(n, r, c) for n, r, c in queries if c <= RARE_MAX]
    print(f"of those, rare enough for known-item scoring (<= {RARE_MAX} relevant): {len(rare)}")
    if not queries:
        sys.exit("no scorable entity queries found")

    arm_limit = max(args.k * 3, DEFAULT_ARM_CAP)
    # Arm outcomes per configuration. A FAILED arm scores exactly like an
    # absent one, so without this a broken arm reads as a weak arm. That
    # happened: an earlier run of this harness imported skmemory from the
    # installed checkout rather than the worktree, so the token lookup did
    # not exist, run_arm caught the AttributeError, and the token row scored
    # like plain fusion. It looked like a measurement and it was a bug.
    import collections

    arm_status: dict = collections.defaultdict(collections.Counter)
    arm_failures: dict = {}

    def legacy(query: str) -> list[str]:
        return [m.id for m in vector.search_text(query, limit=args.k)]

    def _fused(query: str, use_graph: bool, token_graph: bool = False) -> list[str]:
        texts: dict[str, str] = {}

        def _sem() -> list[str]:
            out = []
            for m in vector.search_semantic(query, limit=arm_limit):
                texts[m.id] = f"{m.title or ''} {m.content or ''}"
                out.append(m.id)
            return out

        def _lex() -> list[str]:
            out = []
            for m in vector.search_lexical(query, limit=arm_limit):
                texts[m.id] = f"{m.title or ''} {m.content or ''}"
                out.append(m.id)
            return out

        def _graph() -> list[str]:
            lookup = graph.search_by_entity_tokens if token_graph else graph.search_by_entity
            out = []
            for row in lookup(query, limit=arm_limit) or []:
                row_id = str(row.get("id") or "").strip('"')
                if not row_id:
                    continue
                # The graph is a derived projection and can name a row the
                # source of truth no longer holds. Emitting an id the caller
                # cannot hydrate is how the orphan class of bugs spreads, so
                # an unhydratable id is skipped rather than returned.
                if row_id not in texts:
                    mem = vector.load(row_id)
                    if mem is None:
                        continue
                    texts[row_id] = f"{mem.title or ''} {mem.content or ''}"
                out.append(row_id)
            return out

        arms = [run_arm("semantic", _sem), run_arm("lexical", _lex)]
        if use_graph:
            arms.append(run_arm("graph", _graph))
        report = fuse(arms, limit=arm_limit)
        for arm in report.arms:
            arm_status[arm.name][arm.status] += 1
            if arm.status == "failed":
                arm_failures[arm.name] = arm.detail
        pool = report.ids
        if not pool:
            return []
        outcome = RareTermReranker().rerank(query, [texts.get(i, "") for i in pool])
        ordered = [pool[i] for i in outcome.order] if outcome.applied else pool
        return ordered[: args.k]

    configs = [
        ("legacy (pre-R1)", legacy),
        ("fused (R1+R2)", lambda q: _fused(q, use_graph=False)),
        ("fused + graph (whole)", lambda q: _fused(q, use_graph=True)),
        (
            "fused + graph (tokens)",
            lambda q: _fused(q, use_graph=True, token_graph=True),
        ),
    ]

    results = {}
    for label, fn in configs:
        print(f"\nscoring {label} ...", flush=True)
        arm_status.clear()
        arm_failures.clear()
        precs, rare_hits, rare_rrs, empties = [], [], [], 0
        for name, relevant, count in queries:
            got = fn(name)
            if not got:
                empties += 1
            precs.append(precision_at_k(got, relevant, args.k))
            if count <= RARE_MAX:
                rare_hits.append(hit_at_k(got, relevant, args.k))
                rare_rrs.append(reciprocal_rank(got, relevant))
        if arm_status:
            summary = "  ".join(
                f"{name}={dict(counts)}" for name, counts in sorted(arm_status.items())
            )
            print(f"    arms: {summary}")
        for name, detail in sorted(arm_failures.items()):
            print(f"    ARM FAILED: {name}: {detail[:120]}")
        results[label] = {
            "arm_status": {k: dict(v) for k, v in arm_status.items()},
            "arm_failures": dict(arm_failures),
            "precision": mean(precs) if precs else 0.0,
            "rare_hit": mean(rare_hits) if rare_hits else None,
            "rare_mrr": mean(rare_rrs) if rare_rrs else None,
            "empty": empties,
        }

    print("\n" + "=" * 74)
    print(f"GRAPH ARM A/B  (agent={args.agent}, k={args.k}, {len(queries)} entity queries)")
    print("=" * 74)
    print(f"{'config':<24}{'precision@5':>14}{'rare hit@5':>13}{'rare MRR':>11}{'empty':>8}")
    print("-" * 76)
    for label, _ in configs:
        r = results[label]
        rh = "n/a" if r["rare_hit"] is None else f"{r['rare_hit']:.3f}"
        rm = "n/a" if r["rare_mrr"] is None else f"{r['rare_mrr']:.3f}"
        print(f"{label:<24}{r['precision']:>14.3f}{rh:>13}{rm:>11}{r['empty']:>8}")

    broken = {label: r["arm_failures"] for label, r in results.items() if r.get("arm_failures")}
    if broken:
        print("\nREFUSING a verdict: an arm FAILED in these configurations:")
        for label, fails in broken.items():
            print(f"  {label}: {fails}")
        print("  Fix the arm and re-run. A failed arm scores like an absent one,")
        print("  so any comparison above understates it rather than measuring it.")
        sys.exit(2)

    base = results["fused (R1+R2)"]
    withg = results["fused + graph (tokens)"]
    print(f"\nrare subset size: {len(rare)} queries")
    print("\ndelta, graph arm vs fused without it:")
    print(f"  precision@5  {withg['precision'] - base['precision']:+.3f}")
    if base["rare_hit"] is not None:
        print(f"  rare hit@5   {withg['rare_hit'] - base['rare_hit']:+.3f}")
        print(f"  rare MRR     {withg['rare_mrr'] - base['rare_mrr']:+.3f}")
    print(
        "\nLabels come from memory CONTENT, never from the graph, so the graph arm\n"
        "is scored against the source of truth and can be penalised for being\n"
        "stale. Head-dilution risk from adding an arm is measured separately in\n"
        "scripts/real_corpus_ab.py, not here."
    )


if __name__ == "__main__":
    main()
