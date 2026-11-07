"""Read-only per-query diff: vector_only vs the fused semantic arm vs the final page.

SKMEM-P1-10. With ``SKMEMORY_FUSION_VECTOR_PIN=5`` on the natural route the fused
top 5 should equal the semantic arm's top 5, yet live candidate recall@5 sits
below vector_only. This tool shows, per query and BY MEMORY ID ONLY (never query
or memory text), where every vector_only top-5 hit goes::

    vector_only      pg.search_semantic(raw query, limit=10)            (what live_bench scores)
    arm_raw          the semantic arm inside search_fused, before fusion (sanitized query, 3x pool)
    fused            fuse(): pinned_rrf head plus fill
    reranked         RareTermReranker over the whole fused pool
    demoted          supersession demotion
    capped           source-tier 40 percent cap + procedure order (store.search tail)
    page             the real ``MemoryStore.search(q, limit=10, record_access=False, reader=None)``

Stages after ``fused`` are replayed from the same arm outcomes with the store's
own functions; ``replica_matches_page`` says whether the replay equals the real
page (False means the replay drifted from store.py, trust ``page``).

Read-only: reuses live_bench's guarded backends, never records access, requires
``SKMEMORY_EVAL_LIVE=1`` and writes only under ``--out`` (outside the repo and
``~/.skcapstone``). Run::

    SKMEMORY_EVAL_LIVE=1 SKMEMORY_FUSION_VECTOR_PIN=5 ~/.skenv/bin/python -m \\
        skmemory.eval.arm_diff --agent lumina --paraphrase <private_dir> --out <out_dir>
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from skmemory.eval import live_bench as lb

TOP = 5
LIMIT = lb.LIMIT
#: Order matters: the first stage where a vector hit leaves the top 5 (or moves)
#: is the one blamed.
STAGES = ("arm_raw", "fused", "reranked", "demoted", "capped", "page")


def _pos(ids: Sequence[str], mid: str) -> int | None:
    """1-based rank of ``mid`` in ``ids``, or None."""
    try:
        return list(ids).index(mid) + 1
    except ValueError:
        return None


def blame(vec_top: Sequence[str], stages: dict[str, Sequence[str]]) -> dict[str, dict]:
    """For each vector_only top-5 id: its rank per stage and the stage that hurt it.

    ``removed_by`` is the first stage that takes it out of the top 5, ``moved_by``
    the first stage that changes its rank while it stays inside. ``arm_raw``
    means the semantic arm itself did not have it in its top 5 (different query
    string, different limit/ANN approximation, a filter in the arm, or an arm
    failure), before any fusion stage ran.
    """
    out = {}
    for mid in vec_top[:TOP]:
        ranks = {s: _pos(stages.get(s, ()), mid) for s in STAGES}
        prev = _pos(vec_top, mid)
        removed = moved = None
        for s in STAGES:
            r = ranks[s]
            was_in = prev is not None and prev <= TOP
            now_in = r is not None and r <= TOP
            if removed is None and was_in and not now_in:
                removed = s
            elif moved is None and was_in and now_in and r != prev:
                moved = s
            prev = r
        out[mid] = {"rank": ranks, "removed_by": removed, "moved_by": moved}
    return out


def trace_query(store, vector_only: Callable[[str], list[str]], query: str) -> dict:
    """Run one query through every stage. Ids only; the query text is never stored."""
    from skmemory.fusion import (
        DEFAULT_ARM_CAP,
        fuse,
        fusion_strategy,
        route_query,
    )
    from skmemory.procedures import order_procedures
    from skmemory.query_sanitizer import sanitize_query
    from skmemory.store import _demote_superseded
    from skmemory.tiers import apply_tier_cap

    vec = list(vector_only(query))[:LIMIT]
    pool = max(LIMIT, LIMIT * 3)  # what MemoryStore.search asks search_fused for
    sq = sanitize_query(query)
    mems, report = store.search_fused(sq, limit=pool, record_access=False, return_report=True)
    found = {m.id: m for m in (mems or [])}
    arms = {a.name: list(a.ids) for a in report.arms}
    status = {a.name: a.status for a in report.arms}
    stages: dict[str, list[str]] = {"arm_raw": arms.get("semantic", [])}

    has_pg_bm25 = getattr(store.vector, "search_lexical", None) is not None
    df_fn = getattr(store.primary, "term_doc_freq", None)
    strategy = fusion_strategy()
    route = route_query(sq, df_fn) if strategy == "pinned_rrf" else None
    fused = fuse(
        list(report.arms),
        limit=max(pool, DEFAULT_ARM_CAP),
        strategy=strategy,
        pin_arms=("lexical", "lexical_local") if has_pg_bm25 else ("lexical_local", "lexical"),
        route=route,
    )
    pool_ids = [i for i in fused.ids if i in found]
    stages["fused"] = pool_ids
    texts = [
        f"{getattr(found[i], 'title', '')} {getattr(found[i], 'content', '')}" for i in pool_ids
    ]
    outcome = store._reranker().rerank(sq, texts)
    reranked = [pool_ids[i] for i in outcome.order] if outcome.applied else pool_ids
    stages["reranked"] = reranked
    demoted = _demote_superseded(reranked, found)[:pool]
    stages["demoted"] = demoted
    capped = apply_tier_cap(demoted, found, LIMIT)
    capped = [m.id for m in order_procedures([found[i] for i in capped], sq)]
    stages["capped"] = capped

    page = [m.id for m in store.search(query, limit=LIMIT, record_access=False, reader=None)]
    stages["page"] = page
    return {
        "route": getattr(report, "route", None),
        "arm_status": status,
        "rerank": "applied" if outcome.applied else "abstained",
        "vector_only": vec[:TOP],
        "arm_raw_top5": stages["arm_raw"][:TOP],
        "fused_top5": stages["fused"][:TOP],
        "page_top5": page[:TOP],
        "vector_top5_in_page_top5": sum(1 for m in vec[:TOP] if m in page[:TOP]),
        "page_top5_equals_vector_top5": page[:TOP] == vec[:TOP],
        "replica_matches_page": capped == page,
        "hits": blame(vec, {s: v for s, v in stages.items()}),
    }


def summarise(rows: Sequence[dict]) -> dict:
    n = len(rows)
    removed: Counter = Counter()
    moved: Counter = Counter()
    for r in rows:
        for h in r.get("hits", {}).values():
            if h["removed_by"]:
                removed[h["removed_by"]] += 1
            if h["moved_by"]:
                moved[h["moved_by"]] += 1
    return {
        "queries": n,
        "page_equals_vector_top5": sum(1 for r in rows if r.get("page_top5_equals_vector_top5")),
        "arm_raw_equals_vector_top5": sum(
            1 for r in rows if r.get("arm_raw_top5") == r.get("vector_only")
        ),
        "routes": dict(Counter(str(r.get("route")) for r in rows)),
        "rerank": dict(Counter(r.get("rerank") for r in rows)),
        "replica_mismatch": sum(1 for r in rows if r.get("replica_matches_page") is False),
        "vector_hits_removed_by": dict(removed),
        "vector_hits_moved_by": dict(moved),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def run(
    *,
    out_dir: str,
    paraphrase: str,
    agent: str | None = None,
    embed_interval_s: float = lb.DEFAULT_EMBED_INTERVAL_S,
    max_queries: int | None = None,
) -> tuple[list[dict], dict, list[Path]]:
    lb.require_live()
    d = lb.check_out_dir(out_dir)
    from skmemory.agents import get_active_agent, require_memory_profile
    from skmemory.backend_select import resolve_pg_dsn

    agent = require_memory_profile(agent or get_active_agent()).profile_id
    home = Path.home() / ".skcapstone" / "agents" / agent / "memory"
    pg, age, sq = lb.make_backends(resolve_pg_dsn(), agent, str(home))
    guards = lb.verify_read_only(pg, age, sq)
    store = lb.build_store(pg, age, sq)
    queries, _ = lb.load_paraphrase(paraphrase)
    if max_queries:
        queries = queries[:max_queries]
    keep = os.environ.get("SKMEMORY_SEARCH_FUSION")
    os.environ["SKMEMORY_SEARCH_FUSION"] = "on"
    rows: list[dict] = []
    last = -1e9
    try:
        for q in queries:
            wait = embed_interval_s - (time.perf_counter() - last)
            if wait > 0:
                time.sleep(wait)
            try:
                row = trace_query(
                    store, lambda t: [m.id for m in pg.search_semantic(t, limit=LIMIT)], q.text
                )
            except Exception as exc:  # noqa: BLE001 - one bad query must not end the run
                row = {"error": lb.scrub(type(exc).__name__)}
            last = time.perf_counter()
            row["qid"] = q.id
            row["gold_in_vector_top5"] = [g for g in q.gold_ids if g in row.get("vector_only", [])]
            rows.append(row)
    finally:
        if keep is None:
            os.environ.pop("SKMEMORY_SEARCH_FUSION", None)
        else:
            os.environ["SKMEMORY_SEARCH_FUSION"] = keep
        for c in (pg, age):
            with contextlib.suppress(Exception):
                (getattr(c, "_conn_obj", None) or getattr(c, "_conn", None)).close()
    summary = {
        "agent": agent,
        "vector_pin": os.environ.get("SKMEMORY_FUSION_VECTOR_PIN", "unset (default 4)"),
        "read_only_guards": guards,
        **summarise(rows),
    }
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    paths = []
    for name, body in (
        (
            f"arm-diff-{agent}-{stamp}.rows.jsonl",
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        ),
        (f"arm-diff-{agent}-{stamp}.summary.json", json.dumps(summary, indent=2, sort_keys=True)),
    ):
        path = d / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o600)
        paths.append(path)
    return rows, summary, paths


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="skmemory arm diff (read-only, ids only)")
    p.add_argument("--out", required=True, help="output dir (outside the repo and ~/.skcapstone)")
    p.add_argument(
        "--paraphrase",
        required=True,
        metavar="PRIVATE_DIR",
        help="dir holding paraphrase.jsonl from eval.paraphrase (same as live_bench)",
    )
    p.add_argument("--agent")
    p.add_argument("--max-queries", type=int)
    p.add_argument("--embed-interval", type=float, default=lb.DEFAULT_EMBED_INTERVAL_S)
    a = p.parse_args()
    _, summary, paths = run(
        out_dir=a.out,
        paraphrase=a.paraphrase,
        agent=a.agent,
        embed_interval_s=a.embed_interval,
        max_queries=a.max_queries,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("wrote: " + ", ".join(str(x) for x in paths))


if __name__ == "__main__":
    main()
