#!/usr/bin/env python3
"""Before/after of decay reranking on the fusion_bench query classes.

Takes each fusion_bench strategy's top-10, applies ``skmemory.decay.rerank``
for a channel, and reports recall@5 per class next to the undecayed number.
The corpus carries no usage counters, so every doc is scored as never accessed
(the worst case for decay: nothing is reinforced) at a clock one day after the
newest document. Offline and deterministic.

Run:  python -m skmemory.eval.decay_bench [--strategy interleave_rerank]
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from types import SimpleNamespace

from .. import decay
from . import fusion_bench as fb


def _recall5(got, relevant):
    return fb.recall_at_k(got, relevant, 5)


def run(
    strategy: str = "interleave_rerank",
    channels=("recency", "reinforcement", "affect_weighted"),
    gamma: float = decay.RERANK_GAMMA,
):
    docs = fb._prep_docs(fb.build_corpus())
    by_id = {d["id"]: d for d in docs}
    as_of = max(d["_dt"] for d in docs if d["_dt"]) + timedelta(days=1)
    strat = fb.STRATEGIES[strategy]

    def as_mem(i):
        d = by_id[i]
        return SimpleNamespace(id=i, created_at=d["created_at"], emotional=None, layer="mid-term")

    out = {}
    for ch in ("none", *channels):
        per = {}
        moved = total = 0
        mrrs = []
        cls_mrr = {}
        for cls, queries in fb.QUERY_CLASSES.items():
            vals = []
            for q, rel in queries:
                got = strat(q, docs, 10)
                if ch != "none":
                    base = got
                    got = [
                        m.id
                        for m in decay.rerank(
                            [as_mem(i) for i in got], {}, ch, as_of=as_of, gamma=gamma
                        )
                    ]
                    moved += got != base
                total += 1
                vals.append(_recall5(got, rel))
                mrrs.append(fb.reciprocal_rank(got, rel))
            per[cls] = sum(vals) / len(vals)
            cls_mrr[cls] = sum(mrrs[-len(vals) :]) / len(vals)
        per["_mrr_by_class"] = cls_mrr
        per["_mrr"] = sum(mrrs) / len(mrrs)
        per["_reordered"] = f"{moved}/{total}"
        out[ch] = per
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="interleave_rerank")
    ap.add_argument("--gamma", type=float, default=decay.RERANK_GAMMA)
    args = ap.parse_args()
    res = run(args.strategy, gamma=args.gamma)
    classes = list(fb.QUERY_CLASSES)
    print(f"decay A/B  strategy={args.strategy}  recall@5 per class")
    print(
        f"{'channel':<18}" + "".join(f"{c:>10}" for c in classes) + f"{'mrr':>8}{'reordered':>11}"
    )
    for ch, per in res.items():
        print(
            f"{ch:<18}"
            + "".join(f"{per[c]:>10.3f}" for c in classes)
            + f"{per['_mrr']:>8.3f}{per['_reordered']:>11}"
        )
    print("mrr per class")
    for ch, per in res.items():
        print(f"{ch:<18}" + "".join(f"{per['_mrr_by_class'][c]:>10.4f}" for c in classes))


if __name__ == "__main__":
    main()
