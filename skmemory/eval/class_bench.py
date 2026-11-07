"""Per-class baseline runner over the vendored v1 fixtures (SKMEM-P1-02).

Runs every baseline plus the candidate over ``corpus-v1`` / ``queries-v1`` and
reports, PER QUERY CLASS (never only in aggregate), recall@5, mrr and abstain
correctness. Offline and deterministic: the dense signal is the hashed
bag-of-concepts embedding from ``fusion_bench``, no network, no services.

Strategies
----------
bm25_only        lexical arm alone
grep_substring   case-insensitive substring of the WHOLE query over the flat
                 JSON text of each memory, ranked by hit count
vector_only      hashed offline embedding alone
full_context     ORACLE: gold first, then everything else. Reported with its
                 token cost (the whole corpus), which is what it buys recall with
fallback_chain   old production: vector, text only if vector is empty
candidate        PRODUCTION code: a real ``MemoryStore`` over a tmp home, real
                 SQLiteBackend primary, a hashed vector backend and a tag-derived
                 graph backend implementing the real interfaces, queried with
                 ``store.search`` so ``skmemory/fusion.py`` + ``rerank.py`` run.
                 The gate floors key on this.
reimpl_fusion    ``fusion_bench``'s OFFLINE REIMPLEMENTATION of the same idea.
                 Comparison column only, never gated.

Abstain
-------
The signal is the cosine between the query and the strategy's top-1 document in
the hashed concept space. A query abstains when that is below the threshold
(env ``SKMEMORY_ABSTAIN``, default :data:`DEFAULT_ABSTAIN`). Neither the env
var nor an abstain path exists in the product yet, so this is the bench's own
definition. The default is a round number that was NOT fit to the fixtures;
the report also prints false-abstain on answerable queries so a threshold that
"passes" absent by abstaining on everything is visible.

Queries carrying ``as_of`` are run with it (SKMEM-P3-03): the candidate's
``store.search`` receives ``as_of=`` and the fixture validity windows. The
``expected_fail`` tag is kept for any as_of query that still misses its gate.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from statistics import mean

from skmemory.backends.base import BaseBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.eval import fusion_bench as fb
from skmemory.eval.fixtures import Query, load_corpus, load_queries
from skmemory.eval.recall_benchmark import recall_at_k
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.store import MemoryStore

K = 5
DEFAULT_ABSTAIN = 0.30
EXPECTED_FAIL_REASON = "still below gate with as_of applied (SKMEM-P3-03)"
CANDIDATE = "candidate"

Strategy = Callable[[str, Sequence[dict], int], list[str]]


def abstain_threshold() -> float:
    raw = os.environ.get("SKMEMORY_ABSTAIN")
    if raw is None:
        return DEFAULT_ABSTAIN
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_ABSTAIN


def _strat_grep(query: str, docs: Sequence[dict], k: int) -> list[str]:
    """Per-token substring over the flat JSON text.

    Rank by total matched-token count (every occurrence of every query token),
    ties by distinct-token coverage, then id.
    """
    tokens = fb._tokenize(query)
    scored = []
    for d in docs:
        counts = [d["_flat"].count(t) for t in tokens]
        total, distinct = sum(counts), sum(1 for c in counts if c)
        if total:
            scored.append((d["id"], total, distinct))
    scored.sort(key=lambda p: (-p[1], -p[2], p[0]))
    return [i for i, _, _ in scored[:k]]


STRATEGIES: dict[str, Strategy] = {
    "bm25_only": fb.strat_bm25_only,
    "grep_substring": _strat_grep,
    "vector_only": fb.strat_vector_only,
    "fallback_chain": fb.strat_fallback_chain,
    "reimpl_fusion": fb.strat_interleave_rerank,
}
ORACLE = "full_context"
ALL_STRATEGIES = [*STRATEGIES, CANDIDATE, ORACLE]
SCORED = [*STRATEGIES, CANDIDATE]  # everything with a real ranking (not the oracle)
EMBED_URL_ENV = "SKMEMORY_EVAL_EMBED_URL"
EMBED_MODEL_ENV = "SKMEMORY_EVAL_EMBED_MODEL"


def embed_mode() -> str:
    """ "real" only when SKMEMORY_EVAL_EMBED_URL is set (never in CI)."""
    return "real" if os.environ.get(EMBED_URL_ENV) else "hashed"


# ---------------------------------------------------------------------------
# Production candidate wiring
# ---------------------------------------------------------------------------


def _embed_text(text: str) -> list[float]:
    """Dense vector for ``text``: hashed bag of concepts, or a real endpoint."""
    url = os.environ.get(EMBED_URL_ENV)
    if not url:
        return fb._concept_vector(text)
    body = json.dumps(
        {"model": os.environ.get(EMBED_MODEL_ENV, "mxbai-embed-large"), "input": text}
    ).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - opt-in, local
        data = json.load(resp)
    vec = (data.get("embeddings") or [data.get("embedding")])[0]
    return [float(x) for x in vec]


def _match(mem: Memory, layer, tags, context_tag, source, as_of=None) -> bool:
    from skmemory.store import _passes_filters

    return _passes_filters(mem, layer, tags, context_tag, source, as_of)


class EvalVectorBackend(BaseBackend):
    """Dense backend implementing the real vector interface (``search_semantic``).

    Hashed bag-of-concepts offline. NOT a semantic embedding: see the table
    header. Reads memories from the real primary so it stays a pure index.
    """

    def __init__(self, primary: SQLiteBackend) -> None:
        self._primary = primary
        self._vecs: dict[str, list[float]] = {}

    def index(self, mem: Memory) -> None:
        self._vecs[mem.id] = _embed_text(f"{mem.title} {mem.content} {' '.join(mem.tags)}")

    def search_semantic(
        self, query, limit=10, *, layer=None, tags=None, context_tag=None, source=None, as_of=None
    ):
        qv = _embed_text(query)
        scored = []
        for mem in self._primary.list_memories(limit=100000):
            if not _match(mem, layer, tags, context_tag, source, as_of):
                continue
            sim = fb._cosine(qv, self._vecs[mem.id])
            if sim > 0.0:
                scored.append((sim, mem.id, mem))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [m for _, _, m in scored[:limit]]

    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        return self.search_semantic(query, limit)

    def save(self, memory: Memory) -> str:
        self.index(memory)
        return memory.id

    def load(self, memory_id: str):
        return self._primary.load(memory_id)

    def delete(self, memory_id: str) -> bool:
        return self._vecs.pop(memory_id, None) is not None

    def list_memories(self, *args, **kwargs):
        return self._primary.list_memories(*args, **kwargs)

    def health_check(self) -> dict:
        return {"ok": True, "backend": "eval-vector"}


class EvalTagGraph:
    """Graph arm stand-in. The fixtures have no ``entities`` field, so a memory's
    ENTITIES ARE ITS TAGS and the lookup is per query token, like the production
    ``search_by_entity_tokens``. A derived stand-in, not the AGE graph."""

    def __init__(self, primary: SQLiteBackend) -> None:
        self._primary = primary

    def search_by_entity_tokens(
        self, query, limit=20, *, layer=None, tags=None, context_tag=None, source=None, as_of=None
    ):
        toks = set(fb._tokenize(query))
        hits = []
        for mem in self._primary.list_memories(limit=100000):
            if not _match(mem, layer, tags, context_tag, source, as_of):
                continue
            n = sum(1 for t in mem.tags if t.casefold() in toks)
            if n:
                hits.append((-n, mem.id))
        hits.sort()
        return [{"id": i} for _, i in hits[:limit]]


def _to_memory(c) -> Memory:
    return Memory(
        id=c.id,
        title=c.title,
        content=c.content,
        layer=MemoryLayer(c.layer),
        tags=list(c.tags),
        created_at=c.created_at,
        updated_at=c.created_at,
        context_tag=c.context_tag,
        valid_from=c.valid_from,
        valid_to=c.valid_to,
        supersedes_id=c.supersedes_id,
        emotional=EmotionalSnapshot(intensity=c.emotional.intensity, valence=c.emotional.valence),
        metadata={"kind": c.kind, "event_at": c.event_at, "source_tier": c.source_tier},
    )


def build_production_store(home: str, corpus) -> MemoryStore:
    """A real MemoryStore over ``home`` loaded from the fixture corpus."""
    primary = SQLiteBackend(home)
    vector = EvalVectorBackend(primary)
    for c in corpus:
        mem = _to_memory(c)
        primary.save(mem)
        vector.index(mem)
    return MemoryStore(primary=primary, vector=vector, graph=EvalTagGraph(primary))


FIXTURE_NOW = "2026-11-01T12:00:00+00:00"


@contextlib.contextmanager
def _fusion_env():
    """Force fusion + rerank on and pin the wall clock the temporal arm reads.

    ``store._temporal`` calls ``parse_time_window(query)`` with no ``now``, so a
    run would otherwise drift with the date. Only the clock argument is pinned
    (to the fixture clock); the parsing logic is production code.
    """
    import functools
    from datetime import datetime

    import skmemory.store as store_mod

    orig_ptw = store_mod.parse_time_window
    store_mod.parse_time_window = functools.partial(
        orig_ptw, now=datetime.fromisoformat(FIXTURE_NOW)
    )
    keep = {k: os.environ.get(k) for k in ("SKMEMORY_SEARCH_FUSION", "SKMEMORY_SEARCH_RERANK")}
    os.environ["SKMEMORY_SEARCH_FUSION"] = "on"
    os.environ["SKMEMORY_SEARCH_RERANK"] = "on"
    try:
        yield
    finally:
        store_mod.parse_time_window = orig_ptw
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _prep(corpus: list[dict]) -> list[dict]:
    out = fb._prep_docs(corpus)
    for d in out:
        d["_flat"] = json.dumps({k: v for k, v in d.items() if not k.startswith("_")}).casefold()
    return out


def _tokens(text: str) -> int:
    return len(text.split())


def _rr(got: Sequence[str], gold: set[str]) -> float:
    for rank, i in enumerate(got, 1):
        if i in gold:
            return 1.0 / rank
    return 0.0


def _signal(query: str, got: Sequence[str], by_id: dict[str, dict]) -> float:
    if not got:
        return 0.0
    return fb._cosine(fb._concept_vector(query), by_id[got[0]]["_cvec"])


def _oracle(q: Query, docs: Sequence[dict]) -> list[str]:
    ids = {d["id"] for d in docs}
    gold = [i for i in q.gold_ids if i in ids]
    return gold + [d["id"] for d in docs if d["id"] not in set(gold)]


def run(corpus_dir=None) -> dict:
    """Score every strategy on every query. Returns per-query rows + per-class rollups."""
    with tempfile.TemporaryDirectory(prefix="class-bench-") as home, _fusion_env():
        return _run(corpus_dir, home)


def _run(corpus_dir, home: str) -> dict:
    corpus = load_corpus(corpus_dir)
    store = build_production_store(home, corpus)
    docs = _prep([d.model_dump() for d in corpus])
    queries = load_queries(corpus_dir)
    by_id = {d["id"]: d for d in docs}
    resolver = fb.AudienceResolver()
    public_docs = fb._allowed_docs(docs, fb._public_audience(), resolver)
    thr = abstain_threshold()
    corpus_tokens = sum(_tokens(d["_text"]) for d in docs)

    rows = []
    for q in queries:
        visible = docs if q.reader == "@chef" else public_docs
        gold = set(q.gold_ids)
        must_not = set(q.must_not_ids)
        expected_fail = q.as_of is not None and q.query_class in ("temporal", "conflict")
        per: dict[str, dict] = {}
        for name in ALL_STRATEGIES:
            if name == ORACLE:
                got = _oracle(q, visible)
                cost = sum(_tokens(d["_text"]) for d in visible)
                sig = None
            elif name == CANDIDATE:
                ctx = None if q.reader == "@chef" else "@public"
                got = [
                    m.id for m in store.search(q.query, limit=10, context_tag=ctx, as_of=q.as_of)
                ]
                cost = None
                sig = _signal(q.query, got, by_id)
            else:
                got = STRATEGIES[name](q.query, visible, 10)
                cost = None
                sig = _signal(q.query, got, by_id)
            per[name] = {
                "recall@5": recall_at_k(got, gold, K) if gold else None,
                "rr": _rr(got, gold) if gold else None,
                "abstained": None if sig is None else sig < thr,
                "signal": sig,
                "must_not_hits": sum(1 for i in got[:10] if i in must_not),
                "tokens": cost,
            }
        unfiltered = 0
        if q.query_class == "privacy" and q.reader != "@chef":
            for fn in STRATEGIES.values():
                unfiltered += sum(1 for i in fn(q.query, docs, 10) if i in must_not)
            unfiltered += sum(1 for m in store.search(q.query, limit=10) if m.id in must_not)
        rows.append(
            {
                "id": q.id,
                "class": q.query_class,
                "gate": q.gate_recall_at_5,
                "has_gold": bool(gold),
                "as_of": q.as_of,
                "expected_fail": expected_fail,
                "per": per,
                "leak_unfiltered": unfiltered,
            }
        )

    by_class: dict[str, dict] = {}
    for c in sorted({r["class"] for r in rows}):
        crow = [r for r in rows if r["class"] == c]
        gold_rows = [r for r in crow if r["has_gold"]]
        entry: dict = {"queries": len(crow), "strategies": {}}
        for name in ALL_STRATEGIES:
            ent: dict = {
                "recall@5": mean(r["per"][name]["recall@5"] for r in gold_rows)
                if gold_rows
                else None,
                "mrr": mean(r["per"][name]["rr"] for r in gold_rows) if gold_rows else None,
            }
            if name != ORACLE:
                if c == "absent":
                    ent["abstain_correct"] = mean(
                        1.0 if r["per"][name]["abstained"] else 0.0 for r in crow
                    )
                elif gold_rows:
                    ent["false_abstain"] = mean(
                        1.0 if r["per"][name]["abstained"] else 0.0 for r in gold_rows
                    )
            if c == "privacy":
                ent["leak_count"] = sum(r["per"][name]["must_not_hits"] for r in crow)
            entry["strategies"][name] = ent
        by_class[c] = entry

    failures = []
    for r in rows:
        cand = r["per"][CANDIDATE]
        if r["class"] == "absent":
            val, gate, metric = (1.0 if cand["abstained"] else 0.0), 1.0, "abstain_correct"
        elif r["has_gold"]:
            val, gate, metric = cand["recall@5"], r["gate"], "recall@5"
        else:
            continue
        if val < gate - 1e-9:
            failures.append(
                {
                    "id": r["id"],
                    "class": r["class"],
                    "metric": metric,
                    "value": val,
                    "gate": gate,
                    "expected_fail": EXPECTED_FAIL_REASON if r["expected_fail"] else None,
                }
            )
    xpass = [
        r["id"]
        for r in rows
        if r["expected_fail"]
        and r["has_gold"]
        and r["per"][CANDIDATE]["recall@5"] >= r["gate"] - 1e-9
    ]
    priv = by_class.get("privacy", {}).get("strategies", {})
    return {
        "threshold": thr,
        "embed_mode": embed_mode(),
        "corpus_tokens": corpus_tokens,
        "rows": rows,
        "by_class": by_class,
        "failures": failures,
        "expected_fail_passing_today": xpass,
        "leak_count": sum(priv[n]["leak_count"] for n in ALL_STRATEGIES) if priv else 0,
        "leak_count_unfiltered": sum(r["leak_unfiltered"] for r in rows),
    }


def flat_metrics(res: dict) -> dict[str, float]:
    """Higher-is-better floats for the regression gate (candidate only)."""
    m: dict[str, float] = {}
    for c, entry in res["by_class"].items():
        e = entry["strategies"][CANDIDATE]
        if e["recall@5"] is not None:
            m[f"class_bench.{CANDIDATE}.{c}.recall@5"] = float(e["recall@5"])
            m[f"class_bench.{CANDIDATE}.{c}.mrr"] = float(e["mrr"])
        if "abstain_correct" in e:
            m[f"class_bench.{CANDIDATE}.{c}.abstain_correct"] = float(e["abstain_correct"])
    for r in res["rows"]:
        cand = r["per"][CANDIDATE]
        if r["class"] == "absent":
            m[f"class_bench.{CANDIDATE}.q.{r['id']}.abstain"] = 1.0 if cand["abstained"] else 0.0
        elif r["has_gold"]:
            m[f"class_bench.{CANDIDATE}.q.{r['id']}.recall@5"] = float(cand["recall@5"])
    return m


def _f(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def format_table(res: dict) -> str:
    if res["embed_mode"] == "real":
        dense = f"vector backend uses a REAL embedding endpoint ({EMBED_URL_ENV}); not a CI number"
    else:
        dense = (
            "the offline vector backend is a HASHED BAG OF WORDS with a synonym map, NOT a "
            "semantic embedding: the 'semantic' class is NOT a measure of semantic retrieval offline"
        )
    out = [
        "skmemory per-class baselines",
        "- candidate = PRODUCTION MemoryStore.search (real SQLite primary + fusion.py + rerank.py); "
        "reimpl_fusion = fusion_bench's offline reimplementation (comparison only, not gated)",
        f"- {dense}",
        "- graph arm: fixtures have no entities, so entities are derived from TAGS "
        "(stand-in graph backend, not AGE)",
        f"- abstain threshold {res['threshold']} (cosine of top-1) is BENCH-ONLY: production has "
        "no abstain path and no SKMEMORY_ABSTAIN",
        f"- full_context cost {res['corpus_tokens']} tokens (whole corpus)\n",
    ]
    for metric in ("recall@5", "mrr"):
        hdr = f"{metric:<12}" + "".join(f"{n:>16}" for n in ALL_STRATEGIES)
        out += [hdr, "-" * len(hdr)]
        for c, entry in res["by_class"].items():
            out.append(
                f"{c:<12}"
                + "".join(f"{_f(entry['strategies'][n][metric]):>16}" for n in ALL_STRATEGIES)
            )
        out.append("")
    hdr = f"{'abstain':<12}" + "".join(f"{n:>16}" for n in SCORED)
    out += [hdr, "-" * len(hdr)]
    a = res["by_class"]["absent"]["strategies"]
    out.append(
        f"{'absent ok':<12}" + "".join(f"{_f(a[n]['abstain_correct']):>16}" for n in SCORED)
    )
    fa = []
    for n in SCORED:
        vals = [
            e["strategies"][n]["false_abstain"]
            for e in res["by_class"].values()
            if e["strategies"][n].get("false_abstain") is not None
        ]
        fa.append(mean(vals) if vals else None)
    out.append(f"{'false-abst':<12}" + "".join(f"{_f(v):>16}" for v in fa))
    out.append("")
    out.append(
        f"privacy leak_count (public reader, must be 0): {res['leak_count']}  "
        f"(unfiltered context: {res['leak_count_unfiltered']})"
    )
    out.append(f"\ncandidate queries below gate ({len(res['failures'])}):")
    for f in res["failures"]:
        tag = f"  [expected_fail: {f['expected_fail']}]" if f["expected_fail"] else ""
        out.append(f"  {f['id']:<16}{f['metric']:<16}{f['value']:.3f} < gate {f['gate']:.3f}{tag}")
    if res["expected_fail_passing_today"]:
        out.append(
            "as_of queries that already pass (floored, not flagged): "
            + ", ".join(res["expected_fail_passing_today"])
        )
    return "\n".join(out)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="skmemory per-class baseline runner")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    res = run()
    print(
        json.dumps(res, indent=2, sort_keys=True, default=str) if args.json else format_table(res)
    )


if __name__ == "__main__":
    main()
