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
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
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
        " Your last attempt was rejected. Rephrase completely: no names, numbers, codes or "
        "technical terms copied from the memory; describe the idea in everyday words. The "
        "question MUST begin with What, Why or How."
        if retry
        else ""
    )
    return (
        f"Memory title: {title}\nMemory text:\n{text}\n\n"
        "Write one natural question a person might ask, in their own words, whose answer "
        "is this memory. Start with What, Why or How; avoid yes/no questions. Do not copy "
        "distinctive words, names, numbers or phrases from the memory; say it the way "
        "someone who only half remembers it would." + extra
    )


_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_SUFFIXES = ("ations", "ation", "ings", "ing", "edly", "ies", "ied", "ed", "es", "ly", "s")
RARE_FRAC = 0.002  # a stem in more than 0.2 percent of the CORPUS is "common"
COMMON_CAP = 200_000


def _words(text: str) -> list[str]:
    return [w.strip("._-") for w in _WORD.findall(text.casefold()) if w.strip("._-")]


def stem(word: str) -> str:
    """Crude plural and suffix stemmer, applied to BOTH sides so it only has to be consistent."""
    w = word.casefold()
    if any(c.isdigit() for c in w):
        return w
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
    return w


def _stems(text: str) -> list[str]:
    return [stem(p) for w in _words(text) for p in re.split(r"[-_.]+", w) if p]


def _trigrams(words: Sequence[str]) -> set[tuple[str, str, str]]:
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


_NUM = re.compile(r"\d+")


def _squash(tok: str) -> str:
    return re.sub(r"[-_.]", "", tok)


class CorpusDF:
    """Which stems are COMMON in the whole corpus (document frequency over every memory).

    Rarity is a corpus property, not a sample one: a stem is rare unless it appears in
    more than ``RARE_FRAC`` of all memories. Only the common set is stored (it is small).
    """

    def __init__(self, n: int, common: set[str]) -> None:
        self.n, self.common = n, common

    def is_rare(self, st: str) -> bool:
        return st not in self.common

    @classmethod
    def from_texts(cls, texts: Iterable[str], frac: float = RARE_FRAC) -> CorpusDF:
        df: Counter = Counter()
        n = 0
        for t in texts:
            n += 1
            df.update(set(_stems(t)))
        floor = max(2, int(frac * n))
        top = [s for s, c in df.most_common(COMMON_CAP) if c > floor]
        return cls(n, set(top))

    def dump(self, path: Path) -> None:
        write_private(path, json.dumps({"n": self.n, "common": sorted(self.common)}))

    @classmethod
    def load(cls, path: Path) -> CorpusDF:
        d = json.loads(Path(path).read_text())
        return cls(d["n"], set(d["common"]))


def overlap_reason(question: str, title: str, body: str, df: CorpusDF) -> str | None:
    """Why ``question`` leaks the memory, or None when it is clean.

    Checks stems (plural and simple suffix), un-hyphenated identifier forms, bare
    numbers of any length, corpus-rare stems and stem 3-grams.
    """
    src = f"{title}\n{body}"
    if set(_NUM.findall(question)) & set(_NUM.findall(src)):
        return "number"
    src_words = _words(src)
    src_squash = {_squash(w) for w in src_words}
    src_ident = {_squash(w) for w in src_words if _identifier_like(w) or "-" in w or "_" in w}
    for w in _words(question):
        sq = _squash(w)
        if (_identifier_like(w) or "-" in w or "_" in w) and sq in src_squash:
            return "identifier"
        if sq in src_ident:  # the question wrote a memory's hyphenated id without hyphens
            return "identifier"
    q_st, s_st = _stems(question), _stems(src)
    s_set = set(s_st)
    for st in set(q_st):
        if st in s_set and len(st) >= 3 and df.is_rare(st) and st not in lb._STOP:
            return "rare_stem"
    if _trigrams(q_st) & _trigrams(s_st):
        return "trigram"
    return None


def _identifier_like(tok: str) -> bool:
    return any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)


_QTYPES = ("what", "how", "why", "when", "where", "who", "which")
_YES_NO = {"is", "are", "was", "were", "do", "does", "did", "can", "could", "should",
           "will", "would", "has", "have"}  # fmt: skip
YES_NO_CAP = 0.2


def question_type(q: str) -> str:
    first = (_words(q) or [""])[0]
    if first in _QTYPES:
        return first
    return "yes_no" if first in _YES_NO else "other"


def clean_question(raw: str) -> str:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    lines = [ln.strip().strip("\"'`*- ") for ln in raw.splitlines() if ln.strip()]
    return lines[-1] if lines and "?" in lines[-1] else (lines[0] if lines else "")


def _len_bucket(n: int) -> str:
    return "<300" if n < 300 else "<1000" if n < 1000 else ">=1000"


def select_memories(
    ids: Sequence[str], load: Callable[[str], tuple | None], n: int, seed: int
) -> tuple[list[tuple[str, str, str, str]], dict]:
    """First ``n`` non-sensitive, non-trivial memories in a seeded order.

    ``load`` returns ``(title, content, tags[, layer[, source_ref]])``. Output tuples
    are ``(id, title, content, layer)``.
    """
    import random

    order = lb.sample_ids(ids, len(set(ids)), seed)
    random.Random(seed).shuffle(order)
    out, skipped = [], Counter()
    for mid in order:
        if len(out) >= n:
            break
        rec = load(mid)
        if rec is None:
            skipped["missing"] += 1
            continue
        title, content, tags = rec[:3]
        layer = rec[3] if len(rec) > 3 else "?"
        ref = rec[4] if len(rec) > 4 else ""
        if is_sensitive_memory(title, content, tags, ref):
            skipped["sensitive"] += 1
        elif len((content or "").strip()) < MIN_CONTENT:
            skipped["too_short"] += 1
        else:
            out.append((mid, " ".join((title or "").split()), content, str(layer)))
    return out, dict(skipped)


def generate(
    mems: Sequence[tuple],
    judge: Judge,
    private_dir,
    *,
    df: CorpusDF,
    near: Callable[[str], list[str]] | None = None,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Generate, gate and store. ``near(id)`` lists near-duplicates of a gold memory
    (vector distance <= 0.05) for the separate LENIENT column; it never changes gold."""
    d = ensure_private_dir(private_dir)
    judge.canary()  # RouteRefused here means nothing private was sent
    kept, counts, rejected_by, types = [], Counter(), Counter(), Counter()
    prof = {"rejected": {"layer": Counter(), "length": Counter()},
            "accepted": {"layer": Counter(), "length": Counter()}}  # fmt: skip
    cap = math.ceil(YES_NO_CAP * len(mems))
    for m in mems:
        mid, title, content = m[:3]
        layer = m[3] if len(m) > 3 else "?"
        counts["requested"] += 1
        body = content[:TEXT_CAP]
        q, why = "", "empty"
        for attempt in (0, 1):
            try:
                q = clean_question(judge.ask(SYS, prompt(title, body, retry=bool(attempt))))
            except RouteRefused:
                raise
            except Exception:  # noqa: BLE001
                q, why = "", "judge_error"
                continue
            # check against the FULL text: overlap with any part of the memory leaks it
            why = overlap_reason(q, title, content, df) if q else "empty"
            if why is None and question_type(q) == "yes_no" and types["yes_no"] >= cap:
                why = "yes_no_cap"
            if why is None:
                break
            if attempt == 0:
                counts["retried"] += 1
        bucket = "accepted" if why is None else "rejected"
        prof[bucket]["layer"][layer] += 1
        prof[bucket]["length"][_len_bucket(len(content))] += 1
        if why is not None:
            rejected_by[why] += 1
            if why in ("judge_error", "empty"):
                counts["failed_other"] += 1
            elif why == "yes_no_cap":
                counts["rejected_yes_no_cap"] += 1
            else:
                counts["rejected_overlap"] += 1
        else:
            qt = question_type(q)
            types[qt] += 1
            kept.append({"qid": qid_of(q), "query": q, "gold": [mid], "source": "paraphrase",
                         "qtype": qt, "near": sorted(set(near(mid)) - {mid}) if near else []})  # fmt: skip
        if interval_s > 0:
            sleep(interval_s)
    write_private(
        d / "paraphrase.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept)
    )
    meta = {
        "requested": counts["requested"],
        "accepted": len(kept),
        "rejected_overlap": counts["rejected_overlap"],
        "rejected_yes_no_cap": counts["rejected_yes_no_cap"],
        "failed_other": counts["failed_other"],
        "retried": counts["retried"],
        "rejected_by_reason": dict(rejected_by),
        "query_types": dict(types),
        "yes_no_cap": cap,
        "with_near_duplicates": sum(1 for r in kept if r["near"]),
        "missing_profile": {k: {a: dict(b) for a, b in v.items()} for k, v in prof.items()},
        "corpus_df": {"n": df.n, "common_stems": len(df.common), "rare_frac": RARE_FRAC},
        "judge_routes": dict(judge.routes),
        "limits": LIMITS,
    }
    write_private(d / "paraphrase-meta.json", json.dumps(meta, indent=2, sort_keys=True))
    return meta


NEAR_DISTANCE = 0.05
NEAR_SQL = (
    "WITH g AS (SELECT embedding AS e FROM memories WHERE id=%s AND agent=%s AND embedding IS NOT NULL) "
    "SELECT m.id FROM memories m, g WHERE m.agent=%s AND m.id<>%s AND m.embedding IS NOT NULL "
    "AND (m.embedding <=> g.e) <= %s ORDER BY m.embedding <=> g.e LIMIT 50"
)


def pg_near(pg, mid: str, distance: float = NEAR_DISTANCE) -> list[str]:
    """Read-only: ids within cosine distance ``distance`` of memory ``mid`` (same agent)."""
    with pg._connection().cursor() as cur:
        cur.execute(NEAR_SQL, (mid, pg.agent, pg.agent, mid, distance))
        return [r[0] for r in cur.fetchall()]


def pg_corpus_df(pg) -> CorpusDF:
    """Read-only scan of every memory's title and content for corpus document frequency."""

    def texts():
        last = ""
        while True:
            with pg._connection().cursor() as cur:
                cur.execute(
                    "SELECT id, coalesce(memory_json->>'title',''), content FROM memories "
                    "WHERE agent=%s AND id>%s ORDER BY id LIMIT 2000",
                    (pg.agent, last),
                )
                rows = cur.fetchall()
            if not rows:
                return
            last = rows[-1][0]
            for _, t, c in rows:
                yield f"{t} {(c or '')[:4000]}"

    return CorpusDF.from_texts(texts())


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
    priv = ensure_private_dir(a.private_dir)
    cache = priv / f"corpus-df-{agent}.json"
    if cache.exists():
        df = CorpusDF.load(cache)
    else:
        df = pg_corpus_df(pg)
        df.dump(cache)
    with pg._connection().cursor() as cur:
        cur.execute("SELECT id FROM memories WHERE agent=%s", (pg.agent,))
        ids = [r[0] for r in cur.fetchall()]

    def load(mid):
        m = pg.load(mid)
        if m is None:
            return None
        return (m.title or "", m.content or "", list(m.tags or []),
                getattr(m.layer, "value", str(m.layer)), m.source_ref or "")  # fmt: skip

    mems, skipped = select_memories(ids, load, a.n, a.seed)
    meta = generate(
        mems, Judge(a.gateway, a.model), a.private_dir, df=df,
        near=lambda mid: pg_near(pg, mid), interval_s=a.interval,
    )  # fmt: skip
    meta["skipped_before_request"] = skipped
    meta["seed"] = a.seed
    write_private(priv / "paraphrase-meta.json", json.dumps(meta, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in meta.items() if k != "limits"}, sort_keys=True))


if __name__ == "__main__":
    main()
