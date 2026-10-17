#!/usr/bin/env python3
"""READ-ONLY known-item A/B over a real agent's live memory corpus.

Why this exists
---------------
``skmemory.eval.fusion_bench`` is a 128-document synthetic fixture, and
``scripts/fusion_real_backend_ab.py`` ingests that same synthetic corpus into a
throwaway database. Both prove the fusion logic. Neither proves the win holds on
20,000 real memories written by real daemons over months, where titles are
templated, content is long and repetitive, and whole clusters of near-duplicate
daily reflections exist.

Postmortem 2026-10-16 §3.12 is why that distinction matters: PR #37's stub guard
was real, tested, merged, and never ran in production for a single day. A result
measured on a fixture is not a result measured where the code runs.

Ground truth without labels
---------------------------
Known-item retrieval: sample real memories, derive a query from each, and ask
whether that exact memory comes back. The memory's own id is the label, so no
human annotation is needed and nothing can be fudged. Two query classes, both
drawn from the live corpus:

    identifier  a rare identifier-shaped token lifted out of the memory's own
                content (a card id, a job id, a hostname), wrapped in a few
                words of its surrounding topic. This is the class the legacy
                fallback chain cannot serve, because the dense leg answers and
                the lexical leg is never consulted.
    title       the memory's own title as the query. Titles here are often
                templated ("Lumina's Daily Reflection"), so this class is
                genuinely hard and measures behaviour in a near-duplicate
                cluster rather than on a clean fixture.

No paraphrase class: writing a faithful paraphrase of a real memory needs a
model, and a model-written query would make the benchmark non-deterministic and
unauditable. The offline harness covers paraphrase; this one does not, and the
report says so rather than implying full coverage.

STRICTLY READ-ONLY
------------------
Only SELECT statements and the library's search paths. It never constructs a
writing code path, never calls ``snapshot()``, and refuses to run if asked to
target anything but a local DSN. It is safe to point at the live store, which
is the whole point: the measurement has to happen where the code runs.

Run:
    python scripts/real_corpus_ab.py --agent lumina --samples 60
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from statistics import mean

_IDENT_RE = re.compile(r"\b(?=[a-z0-9-]*\d)[a-z0-9][a-z0-9-]{5,}\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']+")


def _redact(dsn: str) -> str:
    creds = dsn.partition("://")[2].partition("@")[0]
    return dsn.replace(creds, "***") if creds else dsn


def _guard_local(dsn: str) -> None:
    """Refuse a non-local DSN. skmem-pg is per-node by design (prb-6f069c5e),
    and a benchmark has no business reaching another node's store."""
    for marker in ("192.168.", "chiap", "noroc", "tail", ".ts.net"):
        if marker in dsn:
            sys.exit(f"refusing a non-local DSN (contains {marker!r})")


def reciprocal_rank(got: list[str], target: str) -> float:
    for i, item in enumerate(got, start=1):
        if item == target:
            return 1.0 / i
    return 0.0


def hit_at_k(got: list[str], target: str, k: int) -> float:
    return 1.0 if target in got[:k] else 0.0


def build_queries(rows: list[tuple[str, str, str]], rng: random.Random) -> dict[str, list]:
    """Derive (query, target_id) pairs from real rows, by class.

    ``rows`` is (id, title, content).
    """
    identifier: list[tuple[str, str]] = []
    title: list[tuple[str, str]] = []

    for mem_id, mem_title, content in rows:
        words = _WORD_RE.findall((content or "")[:600])
        topic = " ".join(words[:6])

        # identifier class: the rarest-looking token the memory actually
        # contains, so the query is answerable from this memory alone.
        cands = [
            t
            for t in _IDENT_RE.findall((content or "").casefold())
            if not t.isdigit() and len(t) >= 6
        ]
        if cands:
            # Prefer the longest: a longer mixed token is more likely unique.
            token = max(cands, key=len)
            identifier.append((f"{topic} {token}".strip(), mem_id))

        if mem_title and mem_title.strip():
            title.append((mem_title.strip()[:160], mem_id))

    rng.shuffle(identifier)
    rng.shuffle(title)
    return {"identifier": identifier, "title": title}


def score(search_fn, queries: list[tuple[str, str]], k: int = 5) -> dict:
    hits, rrs, empties = [], [], 0
    for query, target in queries:
        got = search_fn(query)
        if not got:
            empties += 1
        hits.append(hit_at_k(got, target, k))
        rrs.append(reciprocal_rank(got, target))
    return {
        f"hit@{k}": mean(hits) if hits else 0.0,
        "mrr": mean(rrs) if rrs else 0.0,
        "empty_results": empties,
        "n": len(queries),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="read-only known-item A/B on a live corpus")
    ap.add_argument("--agent", default=os.environ.get("SKAGENT", "lumina"))
    ap.add_argument("--samples", type=int, default=60, help="memories sampled per class")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument(
        "--with-graph",
        action="store_true",
        help="also score a configuration with the one-hop graph arm attached, to "
        "check whether a fourth arm dilutes the head on these classes",
    )
    args = ap.parse_args()

    dsn = os.environ.get("SKMEMORY_PG_DSN", "")
    if not dsn:
        sys.exit("set SKMEMORY_PG_DSN")
    _guard_local(dsn)
    print(f"DSN:   {_redact(dsn)}")
    print(f"agent: {args.agent}  (READ-ONLY: SELECT plus search paths only)")

    import psycopg

    from skmemory.backends.pgvector_backend import PGVectorBackend
    from skmemory.fusion import DEFAULT_ARM_CAP, fuse, run_arm
    from skmemory.rerank import NoopReranker, RareTermReranker

    graph = None
    if args.with_graph:
        from skmemory.backends.age_backend import AGEGraphBackend

        graph = AGEGraphBackend(dsn=dsn, agent=args.agent)
        if graph.graph is None:
            sys.exit(f"no graph available for agent {args.agent!r}")
        print(f"graph: {graph.graph}")

    rng = random.Random(args.seed)

    # Sample deterministically and read-only. ORDER BY md5(id) gives a stable,
    # arbitrary sample that does not favour recent or old rows.
    want = args.samples * 3
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, content FROM memories "
            "WHERE agent=%s AND content IS NOT NULL AND length(content) > 80 "
            "ORDER BY md5(id) LIMIT %s",
            (args.agent, want),
        )
        rows = [(str(r[0]), r[1] or "", r[2] or "") for r in cur.fetchall()]
    print(f"sampled {len(rows)} rows from the live corpus\n")

    by_class = build_queries(rows, rng)
    by_class = {name: pairs[: args.samples] for name, pairs in by_class.items()}
    for name, pairs in by_class.items():
        print(f"  {name:11} {len(pairs)} queries")

    vector = PGVectorBackend(agent=args.agent, dsn=dsn)

    def legacy(query: str) -> list[str]:
        """What search() did before R1: the vector-first chain, which only
        reaches its lexical stages when the vector stage returns zero rows."""
        return [m.id for m in vector.search_text(query, limit=args.k)]

    def fused(query: str, reranker, use_graph: bool = False) -> list[str]:
        arm_limit = max(args.k * 3, DEFAULT_ARM_CAP)
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
            out = []
            for row in graph.search_by_entity(query, limit=arm_limit) or []:
                row_id = str(row.get("id") or "").strip('"')
                if not row_id:
                    continue
                # A derived projection can name a row the source of truth no
                # longer holds, so an unhydratable id is skipped rather than
                # handed to the caller.
                if row_id not in texts:
                    mem = vector.load(row_id)
                    if mem is None:
                        continue
                    texts[row_id] = f"{mem.title or ''} {mem.content or ''}"
                out.append(row_id)
            return out

        arms = [run_arm("semantic", _sem), run_arm("lexical", _lex)]
        if use_graph and graph is not None:
            arms.append(run_arm("graph", _graph))
        report = fuse(arms, limit=arm_limit)
        pool = report.ids
        if not pool:
            return []
        outcome = reranker.rerank(query, [texts.get(i, "") for i in pool])
        ordered = [pool[i] for i in outcome.order] if outcome.applied else pool
        return ordered[: args.k]

    configs = [
        ("legacy (pre-R1)", legacy),
        ("fused (R1 only)", lambda q: fused(q, NoopReranker())),
        ("fused+rerank (R2)", lambda q: fused(q, RareTermReranker())),
    ]
    if args.with_graph:
        # The point of this row is the REGRESSION check: these two classes are
        # not entity queries, so the graph arm has nothing to contribute to
        # them and can only dilute the head. Its benefit is measured in
        # scripts/entity_arm_ab.py; its cost is measured here.
        configs.append(
            ("fused+rerank+graph", lambda q: fused(q, RareTermReranker(), use_graph=True))
        )

    results: dict[str, dict[str, dict]] = {}
    for label, fn in configs:
        print(f"\nscoring {label} ...", flush=True)
        results[label] = {name: score(fn, pairs, args.k) for name, pairs in by_class.items()}

    classes = list(by_class)
    print("\n" + "=" * 78)
    print(f"REAL CORPUS KNOWN-ITEM A/B  (agent={args.agent}, k={args.k})")
    print("=" * 78)
    header = f"{'config':<20}" + "".join(f"{c + ' hit@' + str(args.k):>20}" for c in classes)
    print(header)
    print("-" * len(header))
    for label, _ in configs:
        row = f"{label:<20}"
        for c in classes:
            row += f"{results[label][c][f'hit@{args.k}']:>20.3f}"
        print(row)

    print()
    header2 = f"{'config':<20}" + "".join(f"{c + ' MRR':>20}" for c in classes)
    print(header2)
    print("-" * len(header2))
    for label, _ in configs:
        row = f"{label:<20}"
        for c in classes:
            row += f"{results[label][c]['mrr']:>20.3f}"
        print(row)

    print("\nempty result sets (a query that returned nothing at all):")
    for label, _ in configs:
        empties = {c: results[label][c]["empty_results"] for c in classes}
        print(f"  {label:<20} {empties}")

    base = results["legacy (pre-R1)"]
    best = results[configs[-1][0]]
    print(f"\ndelta, {configs[-1][0]} vs legacy:")
    for c in classes:
        d_hit = best[c][f"hit@{args.k}"] - base[c][f"hit@{args.k}"]
        d_mrr = best[c]["mrr"] - base[c]["mrr"]
        print(f"  {c:11} hit@{args.k} {d_hit:+.3f}   MRR {d_mrr:+.3f}")

    regressed = [c for c in classes if best[c][f"hit@{args.k}"] < base[c][f"hit@{args.k}"] - 1e-9]
    print(f"\nregressed classes vs legacy: {regressed or 'none'}")

    # The comparison that decides whether to keep a fourth arm is NOT against
    # legacy, which the graph arm beats easily. It is against fusion WITHOUT
    # the graph arm, because on these two classes the graph arm has nothing to
    # contribute and can only dilute the head. Printing only the vs-legacy
    # verdict would hide the one number worth knowing.
    if args.with_graph and "fused+rerank+graph" in results:
        no_graph = results["fused+rerank (R2)"]
        with_graph = results["fused+rerank+graph"]
        print("\ndelta, graph arm vs the SAME pipeline without it (the real cost):")
        worse = []
        for c in classes:
            d_hit = with_graph[c][f"hit@{args.k}"] - no_graph[c][f"hit@{args.k}"]
            d_mrr = with_graph[c]["mrr"] - no_graph[c]["mrr"]
            print(f"  {c:11} hit@{args.k} {d_hit:+.3f}   MRR {d_mrr:+.3f}")
            if d_hit < -1e-9 or d_mrr < -1e-9:
                worse.append(c)
        print(f"  classes the graph arm makes WORSE: {worse or 'none'}")
        print("  Weigh against the entity gain in scripts/entity_arm_ab.py.")
    print("\nNOTE: no paraphrase class here. A faithful paraphrase of a real memory")
    print("needs a model, which would make this non-deterministic and unauditable.")
    print("Paraphrase is covered offline in skmemory.eval.fusion_bench.")
    sys.exit(1 if regressed else 0)


if __name__ == "__main__":
    main()
