"""Citation-gated, two-pass labelling of harvested queries (SKMEM-P1-04).

For each harvested query a pool of candidate memories is built from several
arms (so no single arm owns the gold), sensitive memories are removed, and a
judge behind skgateway proposes which pooled memories are relevant. Rules:

* Every proposed id must carry a QUOTE copied verbatim from that memory's shown
  text. Quotes are validated in code (:func:`quote_is_valid`); a proposal with an
  unverifiable quote is discarded.
* A second pass with a DIFFERENT prompt (per-memory verdicts, reversed order)
  must produce the identical gold set. Otherwise the query goes to the
  ``unjudged`` bucket, reported as missingness. Nothing is guessed.
* The gateway route must be local (response headers ``x-sk-rail`` and
  ``x-sk-provider`` both ``local``). A canary call proves it before any query is
  sent, and every later response is re-checked; a non-local answer aborts the run.
  Sensitive queries and memories are filtered before any call, so nothing
  sacred or medical is ever sent to a model.

All output is private (see ``harvest.check_private_dir``) and never committed.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from skmemory.eval.harvest import (
    Candidate,
    check_private_dir,
    ensure_private_dir,
    is_sensitive_memory,
    is_sensitive_text,
    load_candidates,
    write_private,
)

GATEWAY = "http://localhost:18780/v1"
MODEL = "sk-default"
POOL_CAP = 12
TEXT_CAP = 1200
MIN_QUOTE = 20

JUDGE_LIMITS = (
    "Judge limits: (1) both passes use the same model family, so their errors are "
    "correlated and agreement is NOT accuracy; judge accuracy was not measured against "
    "humans (no human gate by decision). (2) Quote validation proves the quote exists in "
    "the memory, not that the memory answers the query. (3) Gold can only come from the "
    "pooled memories (union of the lexical, vector, SQLite and fused arms), so a relevant "
    "memory no arm surfaced is never gold; this flatters the arms that fed the pool and "
    "caps recall of anything else. (4) Memories are cut to a fixed length before judging. "
    "(5) Sensitive memories are removed from the pool, so gold never includes them. (6) "
    "Unjudged queries are not random: hard or ambiguous queries disagree more, so the "
    "labelled set skews toward easy queries. (7) Multi-gold recall@5 is the fraction of "
    "gold ids found in the top 5."
)


class RouteRefused(RuntimeError):
    """The gateway answered from a non-local route. The run must stop."""


class JudgeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Quote validation (pure)
# ---------------------------------------------------------------------------

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _norm(text: str) -> str:
    return " ".join(str(text).translate(_QUOTES).casefold().split())


def quote_is_valid(quote: object, shown_text: str) -> bool:
    """True iff ``quote`` is a verbatim (whitespace and case-insensitive) span of the text."""
    if not isinstance(quote, str):
        return False
    q = _norm(quote).strip("\"'`. ")
    q = q.removeprefix("...").removesuffix("...").strip()
    src = _norm(shown_text)
    need = min(MIN_QUOTE, len(src))
    return len(q) >= need > 0 and q in src


# ---------------------------------------------------------------------------
# Judge transport and route guard
# ---------------------------------------------------------------------------

Transport = Callable[[str, dict, float], "tuple[int, dict[str, str], str]"]


def _http_post(url: str, payload: dict, timeout: float) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(
        url, json.dumps(payload).encode(), {"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, ""


def assert_local(headers: dict[str, str]) -> None:
    rail, prov = headers.get("x-sk-rail", ""), headers.get("x-sk-provider", "")
    if rail != "local" or prov != "local":
        raise RouteRefused(f"route not local (rail={rail!r} provider={prov!r}); refusing")


class Judge:
    def __init__(
        self,
        base_url: str = GATEWAY,
        model: str = MODEL,
        transport: Transport = _http_post,
        timeout: float = 240.0,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.transport = transport
        self.timeout = timeout
        self.routes: Counter = Counter()

    def _chat(self, messages: list[dict], max_tokens: int = 1500) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        status, headers, body = self.transport(self.url, payload, self.timeout)
        assert_local(headers)  # checked before the body is even looked at
        self.routes[f"{headers.get('x-sk-provider')}/{headers.get('x-sk-model-served')}"] += 1
        if status != 200:
            raise JudgeError(f"http {status}")
        try:
            return json.loads(body)["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise JudgeError("bad response shape") from exc

    def canary(self) -> None:
        """Prove the route is local with a prompt that carries no private data."""
        self._chat([{"role": "user", "content": "Reply with the single word: ok"}], 20)

    def ask(self, system: str, user: str) -> str:
        return self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )


def parse_json(text: str) -> dict | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# Prompts: two deliberately different framings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolMemory:
    id: str
    title: str
    text: str  # what the judge is shown (and what quotes are validated against)

    @property
    def shown(self) -> str:
        return f"{self.title}\n{self.text}"


def _render(pool: Sequence[PoolMemory]) -> str:
    return "\n\n".join(f"[M{i}]\n{m.shown}" for i, m in enumerate(pool, 1))


SYS_A = (
    "You label search relevance. You only output JSON. You never invent text: every "
    "quote must be copied character for character from the memory it supports."
)
SYS_B = (
    "You are a strict evidence checker for a retrieval benchmark. You only output "
    "JSON. A memory is relevant only if a specific sentence in it helps answer the "
    "query. If you cannot copy such a sentence exactly, the memory is not relevant."
)


def prompt_a(query: str, pool: Sequence[PoolMemory]) -> str:
    return (
        f"Query: {query}\n\nMemories:\n\n{_render(pool)}\n\n"
        "Which memories directly answer the query or hold facts needed to answer it? "
        'Reply as {"gold":[{"m":"M3","quote":"<exact text copied from M3>"}]}. '
        'Use {"gold":[]} if none do. Quotes must be at least 20 characters.'
    )


def prompt_b(query: str, pool: Sequence[PoolMemory]) -> str:
    return (
        f"A user asked: {query}\n\nCandidate memories (listed in reverse order):\n\n"
        f"{_render(pool)}\n\n"
        "Grade EACH memory. Reply as "
        '{"verdicts":[{"m":"M1","relevant":true,"quote":"<exact sentence copied from M1>"},'
        '{"m":"M2","relevant":false,"quote":""}]}. '
        "Mark relevant only when you can copy an exact supporting sentence (20+ characters)."
    )


def _handle_index(h: object, n: int) -> int | None:
    m = re.fullmatch(r"\s*\[?M(\d+)\]?\s*", str(h))
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1)) - 1
    return None


def _gold_from(
    obj: dict | None, key: str, pool: Sequence[PoolMemory]
) -> tuple[dict[str, str], int]:
    """Validated ``{id: quote}`` and the count of rejected proposals."""
    gold: dict[str, str] = {}
    rejected = 0
    for item in (obj or {}).get(key) or []:
        if not isinstance(item, dict):
            rejected += 1
            continue
        if key == "verdicts" and item.get("relevant") is not True:
            continue
        i = _handle_index(item.get("m"), len(pool))
        if i is None or not quote_is_valid(item.get("quote"), pool[i].shown):
            rejected += 1
            continue
        gold[pool[i].id] = str(item["quote"])
    return gold, rejected


# ---------------------------------------------------------------------------
# Pool and per-query judging
# ---------------------------------------------------------------------------

Loader = Callable[[str], "tuple[str, str, Sequence[str]] | None"]  # id -> (title, content, tags)


def build_pool(
    arms: dict[str, Callable[[str], list[str]]],
    load: Loader,
    query: str,
    cap: int = POOL_CAP,
) -> tuple[list[PoolMemory], dict]:
    """Round-robin union of the arms, sensitive and missing memories removed."""
    lists = []
    for fn in arms.values():
        try:
            lists.append(list(fn(query)))
        except Exception:  # noqa: BLE001 - a dead arm just contributes nothing
            lists.append([])
    order: list[str] = []
    for rank in range(max(map(len, lists), default=0)):
        for ids in lists:
            if rank < len(ids) and ids[rank] not in order:
                order.append(ids[rank])
    pool: list[PoolMemory] = []
    info = {"sensitive_dropped": 0, "missing": 0}
    for mid in order:
        if len(pool) >= cap:
            break
        rec = load(mid)
        if rec is None:
            info["missing"] += 1
            continue
        title, content, tags = rec
        if is_sensitive_memory(title, content, tags):
            info["sensitive_dropped"] += 1
            continue
        pool.append(PoolMemory(mid, " ".join((title or "").split()), (content or "")[:TEXT_CAP]))
    # Deterministic, arm-blind order so position does not leak which arm found it.
    pool.sort(key=lambda m: m.id)
    return pool, info


def judge_query(judge: Judge, query: str, pool: Sequence[PoolMemory]) -> dict:
    """Two independent passes. Returns a record with ``status`` labelled or unjudged."""
    if not pool:
        return {"status": "unjudged", "reason": "pool_empty"}
    try:
        a_raw = judge.ask(SYS_A, prompt_a(query, pool))
        rev = list(reversed(pool))
        b_raw = judge.ask(SYS_B, prompt_b(query, rev))
    except JudgeError as exc:
        return {"status": "unjudged", "reason": f"judge_error:{exc}"}
    a_obj, b_obj = parse_json(a_raw), parse_json(b_raw)
    if a_obj is None or b_obj is None or "gold" not in a_obj or "verdicts" not in b_obj:
        return {"status": "unjudged", "reason": "parse_error"}
    gold_a, rej_a = _gold_from(a_obj, "gold", pool)
    gold_b, rej_b = _gold_from(b_obj, "verdicts", rev)
    base = {
        "rejected_quotes": rej_a + rej_b,
        "pool": len(pool),
        "agree": set(gold_a) == set(gold_b),
    }
    if set(gold_a) != set(gold_b):
        return {"status": "unjudged", "reason": "disagree", **base}
    if not gold_a:
        return {"status": "unjudged", "reason": "no_gold", **base}
    return {"status": "labelled", "gold": sorted(gold_a), "quotes": gold_a, **base}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def label_run(
    cands: Sequence[Candidate],
    arms: dict[str, Callable[[str], list[str]]],
    load: Loader,
    judge: Judge,
    private_dir: str | os.PathLike,
    *,
    limit: int | None = None,
    seed: int = 20261106,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    d = ensure_private_dir(private_dir)
    judge.canary()  # RouteRefused here means nothing private was sent
    picked = sorted(cands, key=lambda c: c.qid)
    if limit is not None and limit < len(picked):
        picked = sorted(random.Random(seed).sample(picked, limit), key=lambda c: c.qid)
    counts: Counter = Counter()
    reasons: Counter = Counter()
    labelled_lines, unjudged_lines = [], []
    for c in picked:
        counts["attempted"] += 1
        if is_sensitive_text(c.query):  # defence in depth: harvest already filters
            counts["excluded_sensitive"] += 1
            continue
        pool, info = build_pool(arms, load, c.query)
        counts["pool_sensitive_dropped"] += info["sensitive_dropped"]
        rec = judge_query(judge, c.query, pool)
        counts["rejected_quotes"] += rec.get("rejected_quotes", 0)
        if "agree" in rec:
            counts["both_parsed"] += 1
            counts["agreed"] += int(rec["agree"])
        if rec["status"] == "labelled":
            counts["labelled"] += 1
            labelled_lines.append(
                {"qid": c.qid, "query": c.query, "source": c.source, "gold": rec["gold"],
                 "quotes": rec["quotes"]}
            )  # fmt: skip
        else:
            counts["unjudged"] += 1
            reasons[rec["reason"].split(":")[0]] += 1
            unjudged_lines.append({"qid": c.qid, "source": c.source, "reason": rec["reason"]})
        if interval_s > 0:
            sleep(interval_s)
    write_private(
        d / "labelled.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in labelled_lines)
    )
    write_private(
        d / "unjudged.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in unjudged_lines)
    )
    meta = {
        "candidates": len(cands),
        "attempted": counts["attempted"],
        "labelled": counts["labelled"],
        "unjudged": counts["unjudged"],
        "unjudged_reasons": dict(reasons),
        "excluded_sensitive": counts["excluded_sensitive"],
        "pool_sensitive_dropped": counts["pool_sensitive_dropped"],
        "rejected_quotes": counts["rejected_quotes"],
        "agreement_rate": (counts["agreed"] / counts["both_parsed"])
        if counts["both_parsed"]
        else None,
        "both_parsed": counts["both_parsed"],
        "judge_routes": dict(judge.routes),
        "judge_model_requested": judge.model,
        "limits": JUDGE_LIMITS,
    }
    write_private(d / "label-meta.json", json.dumps(meta, indent=2, sort_keys=True))
    return meta


def main() -> None:
    import argparse

    from skmemory.eval import live_bench as lb

    p = argparse.ArgumentParser(description="label harvested queries (live read-only pool)")
    p.add_argument("--private-dir", required=True)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--gateway", default=GATEWAY)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--agent")
    a = p.parse_args()
    lb.require_live()
    check_private_dir(a.private_dir)
    cands = load_candidates(Path(a.private_dir) / "candidates.jsonl")

    from skmemory.agents import get_active_agent, require_memory_profile
    from skmemory.backend_select import resolve_pg_dsn

    agent = require_memory_profile(a.agent or get_active_agent()).profile_id
    home = Path.home() / ".skcapstone" / "agents" / agent / "memory"
    pg, age, sq = lb.make_backends(resolve_pg_dsn(), agent, str(home))
    lb.verify_read_only(pg, age, sq)
    alive = lb.probe_arms(pg, age, sq)
    store = lb.build_store(pg, age, sq)
    all_arms = lb.build_arms(pg, age, sq, store, {})
    os.environ["SKMEMORY_SEARCH_FUSION"] = "on"
    pool_arms = {
        n: all_arms[n]
        for n in ("bm25_only", "vector_only", "sqlite_like", "candidate")
        if alive.get(n, (False, ""))[0]
    }

    def load(mid: str):
        m = pg.load(mid)
        return None if m is None else (m.title or "", m.content or "", list(m.tags or []))

    meta = label_run(
        cands, pool_arms, load, Judge(a.gateway, a.model), a.private_dir,
        limit=a.limit, interval_s=a.interval,
    )  # fmt: skip
    meta["pool_arms"] = sorted(pool_arms)
    print(json.dumps({k: v for k, v in meta.items() if k != "limits"}, sort_keys=True))


if __name__ == "__main__":
    main()
