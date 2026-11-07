"""Synthetic paraphrase queries with gold known by construction (SKMEM-P1-04).

For a seeded sample of non-sensitive memories, a judge behind skgateway (local
rail verified, see ``label.Judge``) writes the question a person would ask whose
answer is that memory, in their own words. Code then REJECTS any question that
shares a rare token, an identifier-shaped token or a word 3-gram with the
memory's title or body, retries once, and leaves a still-rejected memory out
(missing, counted). So the query cannot be answered by lexical overlap and the
gold id is the memory it was written from.

Limits, stated in the bench header: this removes lexical bias, but gold is a
single known item (other memories may answer the question equally well, which
under-counts every arm) and generated questions are not real user queries.

Output is private (``harvest.check_private_dir``): queries are never printed
and never committed.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from skmemory.eval import live_bench as lb
from skmemory.eval.harvest import (
    ensure_private_dir,
    is_sensitive_memory,
    qid_of,
    write_private,
)
from skmemory.eval.label import GATEWAY, MODEL, Judge, RouteRefused  # noqa: F401

DEFAULT_N = 200
TEXT_CAP = 1200
MIN_CONTENT = 80

LIMITS = (
    "Paraphrase limits: it removes lexical bias, but gold is a single known item "
    "(another memory may answer the question as well, which under-counts every arm) "
    "and generated questions are not real user queries. The generator is one local "
    "model, so question style is narrower than people's."
)

SYS = (
    "You write search questions for a memory system benchmark. Output exactly one "
    "question and nothing else."
)


def prompt(title: str, text: str, retry: bool = False) -> str:
    extra = (
        " Your last attempt reused words from the memory. Rephrase completely: no names, "
        "numbers, codes or technical terms copied from it; describe the idea in everyday "
        "words."
        if retry
        else ""
    )
    return (
        f"Memory title: {title}\nMemory text:\n{text}\n\n"
        "Write one natural question a person might ask, in their own words, whose answer "
        "is this memory. Do not copy distinctive words, names, numbers or phrases from "
        "the memory; say it the way someone who only half remembers it would." + extra
    )


_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")


def _words(text: str) -> list[str]:
    return [w.strip("._-") for w in _WORD.findall(text.casefold()) if w.strip("._-")]


def _trigrams(words: Sequence[str]) -> set[tuple[str, str, str]]:
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def overlap_reason(question: str, title: str, body: str, df) -> str | None:
    """Why ``question`` leaks the memory, or None when it is clean."""
    src_text = f"{title}\n{body}"
    qw, sw = _words(question), _words(src_text)
    src_tokens = set(lb._tokens(src_text))
    for t in set(lb._tokens(question)):
        if t in src_tokens and lb._identifier_shaped(t):
            return "identifier"
        if t in src_tokens and df.get(t, 0) <= 2 and t not in lb._STOP:
            return "rare_token"
    if _trigrams(qw) & _trigrams(sw):
        return "trigram"
    return None


_QTYPES = ("what", "how", "why", "when", "where", "who", "which")


def question_type(q: str) -> str:
    first = (_words(q) or [""])[0]
    if first in _QTYPES:
        return first
    if first in {"is", "are", "was", "were", "do", "does", "did", "can", "could", "should",
                 "will", "would", "has", "have"}:  # fmt: skip
        return "yes_no"
    return "other"


def clean_question(raw: str) -> str:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    lines = [ln.strip().strip("\"'`*- ") for ln in raw.splitlines() if ln.strip()]
    return lines[-1] if lines and "?" in lines[-1] else (lines[0] if lines else "")


def select_memories(
    ids: Sequence[str], load: Callable[[str], tuple | None], n: int, seed: int
) -> tuple[list[tuple[str, str, str]], dict]:
    """First ``n`` non-sensitive, non-trivial memories in a seeded order."""
    order = lb.sample_ids(ids, len(set(ids)), seed)
    import random

    random.Random(seed).shuffle(order)
    out, skipped = [], Counter()
    for mid in order:
        if len(out) >= n:
            break
        rec = load(mid)
        if rec is None:
            skipped["missing"] += 1
            continue
        title, content, tags = rec
        if is_sensitive_memory(title, content, tags):
            skipped["sensitive"] += 1
        elif len((content or "").strip()) < MIN_CONTENT:
            skipped["too_short"] += 1
        else:
            out.append((mid, " ".join((title or "").split()), content))
    return out, dict(skipped)


def generate(
    mems: Sequence[tuple[str, str, str]],
    judge: Judge,
    private_dir,
    *,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    d = ensure_private_dir(private_dir)
    judge.canary()  # RouteRefused here means nothing private was sent
    df = lb.document_frequency([f"{t} {c[:4000]}" for _, t, c in mems])
    kept, counts, rejected_by, types = [], Counter(), Counter(), Counter()
    for mid, title, content in mems:
        counts["requested"] += 1
        body = content[:TEXT_CAP]
        q, why = "", "empty"
        for attempt in (0, 1):
            try:
                q = clean_question(judge.ask(SYS, prompt(title, body, retry=bool(attempt))))
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, RouteRefused):
                    raise
                q, why = "", "judge_error"
                continue
            # check against the FULL text: overlap with any part of the memory leaks it
            why = overlap_reason(q, title, content, df) if q else "empty"
            if why is None:
                break
            if attempt == 0:
                counts["retried"] += 1
        if why is not None:
            counts["rejected_overlap" if why in ("identifier", "rare_token", "trigram") else
                   "failed_other"] += 1  # fmt: skip
            rejected_by[why] += 1
        else:
            qt = question_type(q)
            types[qt] += 1
            kept.append({"qid": qid_of(q), "query": q, "gold": [mid], "source": "paraphrase",
                         "qtype": qt})  # fmt: skip
        if interval_s > 0:
            sleep(interval_s)
    write_private(
        d / "paraphrase.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept)
    )
    meta = {
        "requested": counts["requested"],
        "accepted": len(kept),
        "rejected_overlap": counts["rejected_overlap"],
        "failed_other": counts["failed_other"],
        "retried": counts["retried"],
        "rejected_by_reason": dict(rejected_by),
        "query_types": dict(types),
        "judge_routes": dict(judge.routes),
        "limits": LIMITS,
    }
    write_private(d / "paraphrase-meta.json", json.dumps(meta, indent=2, sort_keys=True))
    return meta


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="generate paraphrase queries (live read-only)")
    p.add_argument("--private-dir", required=True)
    p.add_argument("-n", type=int, default=DEFAULT_N)
    p.add_argument("--seed", type=int, default=lb.DEFAULT_SEED)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--gateway", default=GATEWAY)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--agent")
    a = p.parse_args()
    lb.require_live()
    from skmemory.agents import get_active_agent, require_memory_profile
    from skmemory.backend_select import resolve_pg_dsn

    agent = require_memory_profile(a.agent or get_active_agent()).profile_id
    home = Path.home() / ".skcapstone" / "agents" / agent / "memory"
    pg, age, sq = lb.make_backends(resolve_pg_dsn(), agent, str(home))
    lb.verify_read_only(pg, age, sq)
    conn = pg._connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM memories WHERE agent=%s", (pg.agent,))
        ids = [r[0] for r in cur.fetchall()]

    def load(mid):
        m = pg.load(mid)
        return None if m is None else (m.title or "", m.content or "", list(m.tags or []))

    mems, skipped = select_memories(ids, load, a.n, a.seed)
    meta = generate(mems, Judge(a.gateway, a.model), a.private_dir, interval_s=a.interval)
    meta["skipped_before_request"] = skipped
    meta["seed"] = a.seed
    write_private(
        Path(a.private_dir) / "paraphrase-meta.json", json.dumps(meta, indent=2, sort_keys=True)
    )
    print(json.dumps({k: v for k, v in meta.items() if k != "limits"}, sort_keys=True))


if __name__ == "__main__":
    main()
