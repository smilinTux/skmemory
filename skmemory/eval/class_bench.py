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
candidate        the shipped fusion (``interleave_rerank``)

Abstain
-------
The signal is the cosine between the query and the strategy's top-1 document in
the hashed concept space. A query abstains when that is below the threshold
(env ``SKMEMORY_ABSTAIN``, default :data:`DEFAULT_ABSTAIN`). Neither the env
var nor an abstain path exists in the product yet, so this is the bench's own
definition. The default is a round number that was NOT fit to the fixtures;
the report also prints false-abstain on answerable queries so a threshold that
"passes" absent by abstaining on everything is visible.

``as_of`` does not exist until SKMEM-P3-03. Queries carrying it are scored as
the system behaves today and tagged ``expected_fail`` (never skipped).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from statistics import mean

from skmemory.eval import fusion_bench as fb
from skmemory.eval.fixtures import Query, load_corpus, load_queries
from skmemory.eval.recall_benchmark import recall_at_k

K = 5
DEFAULT_ABSTAIN = 0.30
EXPECTED_FAIL_REASON = "needs as_of (SKMEM-P3-03)"
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
    needle = query.casefold()
    scored = []
    for d in docs:
        hits = d["_flat"].count(needle)
        if hits:
            scored.append((d["id"], hits))
    scored.sort(key=lambda p: (-p[1], p[0]))
    return [i for i, _ in scored[:k]]


STRATEGIES: dict[str, Strategy] = {
    "bm25_only": fb.strat_bm25_only,
    "grep_substring": _strat_grep,
    "vector_only": fb.strat_vector_only,
    "fallback_chain": fb.strat_fallback_chain,
    CANDIDATE: fb.strat_interleave_rerank,
}
ORACLE = "full_context"
ALL_STRATEGIES = [*STRATEGIES, ORACLE]


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
    docs = _prep([d.model_dump() for d in load_corpus(corpus_dir)])
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
    out = [
        f"skmemory per-class baselines | abstain threshold {res['threshold']} "
        f"(cosine of top-1) | full_context cost {res['corpus_tokens']} tokens (whole corpus)\n"
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
    hdr = f"{'abstain':<12}" + "".join(f"{n:>16}" for n in STRATEGIES)
    out += [hdr, "-" * len(hdr)]
    a = res["by_class"]["absent"]["strategies"]
    out.append(
        f"{'absent ok':<12}" + "".join(f"{_f(a[n]['abstain_correct']):>16}" for n in STRATEGIES)
    )
    fa = []
    for n in STRATEGIES:
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
            "as_of queries passing today anyway: " + ", ".join(res["expected_fail_passing_today"])
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
