"""Live-corpus self-retrieval runner over the real arms (SKMEM-P1-03).

Samples N memories (default 200) from the active agent with a seeded,
deterministic sampler, builds template queries from each memory's TITLE and from
its RAREST TOKENS (no LLM), and runs the same strategies ``class_bench`` reports
against the real backends: skmem-pg lexical (BM25), skmem-pg dense, the AGE
graph, the SQLite index and the production ``MemoryStore.search`` fusion.

Gated behind ``SKMEMORY_EVAL_LIVE=1``. Never part of CI, never run by the tests.

Bias, read this before quoting a number
---------------------------------------
This is SELF-RETRIEVAL: the query is cut from the very memory it must find. It
is easy for lexical arms (the title is indexed verbatim, a rare token is an
exact hit) and says little about semantic recall on a paraphrased question. Use
it to check that every arm is alive and fast on the real corpus, and to compare
arms on identical queries, NOT as an absolute quality number. The offline
``reimpl_fusion`` and ``full_context`` columns of ``class_bench`` have no live
analogue (they run over an in-memory corpus) and are not reported here.

Strategy mapping onto ``class_bench``
-------------------------------------
bm25_only       skmem-pg ``search_lexical`` (ParadeDB BM25, else ts_rank)
vector_only     skmem-pg ``search_semantic`` (mxbai over a real embed endpoint)
sqlite_like     the SQLite index ``search_text`` (LIKE), the live stand-in for
                ``grep_substring``
graph_only      AGE ``search_by_entity_tokens``
fallback_chain  old production: vector, lexical only if vector is empty. Derived
                from the two runs above, so it costs no extra embed call
candidate       PRODUCTION ``MemoryStore.search_fused`` (fusion + rerank)

Read-only contract
------------------
Nothing here may write to the live store. SQLite is opened ``mode=ro`` with
``query_only``; both Postgres sessions are ``TRANSACTION READ ONLY``; every
write method on the wrappers raises; ``record_access`` is never True; the
write-ahead log the store would create under the agent home is replaced by a
null object. :func:`verify_read_only` PROVES the guards by attempting a write
and requires it to fail before a single query runs.

A dead arm is reported as ``dead`` with the reason, never as zero recall.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import re
import sqlite3
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from skmemory.eval.recall_benchmark import recall_at_k

LIVE_ENV = "SKMEMORY_EVAL_LIVE"
DEFAULT_N = 200
DEFAULT_SEED = 20261106
K = 5
LIMIT = 10
DEFAULT_EMBED_INTERVAL_S = 0.25

ARMS = ["bm25_only", "vector_only", "sqlite_like", "graph_only", "fallback_chain", "candidate"]
CANDIDATE = "candidate"
EMBEDDING_ARMS = frozenset({"vector_only", CANDIDATE})

Arm = Callable[[str], "list[str]"]


class LiveNotEnabled(RuntimeError):
    pass


class ReadOnlyViolation(RuntimeError):
    pass


def require_live(environ: dict | None = None) -> None:
    env = os.environ if environ is None else environ
    if env.get(LIVE_ENV) != "1":
        raise LiveNotEnabled(f"live bench is opt-in: set {LIVE_ENV}=1")


# ---------------------------------------------------------------------------
# Sampler and query templates (pure, deterministic)
# ---------------------------------------------------------------------------


def sample_ids(ids: Sequence[str], n: int, seed: int = DEFAULT_SEED) -> list[str]:
    """Seeded sample of ``n`` ids. Order-independent: the input is sorted first."""
    pool = sorted(set(ids))
    if n >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, n))


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{4,}")
_STOP = frozenset(
    [
        "about",
        "after",
        "again",
        "being",
        "below",
        "could",
        "doing",
        "during",
        "every",
        "first",
        "other",
        "should",
        "still",
        "their",
        "there",
        "these",
        "those",
        "through",
        "under",
        "until",
        "where",
        "which",
        "while",
        "would",
        "these",
        "before",
    ]
)


def _tokens(text: str) -> list[str]:
    return [t.strip("._-") for t in _TOKEN_RE.findall(text.casefold())]


def _identifier_shaped(tok: str) -> bool:
    return any(c.isdigit() for c in tok) or any(c in tok for c in "-_")


def document_frequency(texts: Sequence[str]) -> Counter:
    df: Counter = Counter()
    for t in texts:
        df.update(set(_tokens(t)))
    return df


def rare_tokens(text: str, df: Counter, k: int = 3, max_df: int = 2) -> list[str]:
    """Up to ``k`` rarest tokens of ``text``.

    Rarity is document frequency over the SAMPLE (a proxy: the whole corpus is
    not scanned). Identifier-shaped tokens rank first, then lower df, then
    longer, then alphabetical, so the pick is fully deterministic.
    """
    cands = {t for t in _tokens(text) if t not in _STOP and df.get(t, 0) <= max_df}
    ranked = sorted(cands, key=lambda t: (not _identifier_shaped(t), df.get(t, 0), -len(t), t))
    return ranked[:k]


@dataclass(frozen=True)
class LiveQuery:
    id: str  # gold memory id
    kind: str  # "title" | "rare_token"
    text: str


def build_queries(mems: Sequence[tuple[str, str, str]]) -> list[LiveQuery]:
    """``mems`` is ``(id, title, content)``. Returns title and rare-token queries."""
    df = document_frequency([f"{t} {c[:4000]}" for _, t, c in mems])
    out: list[LiveQuery] = []
    for mid, title, content in mems:
        title = " ".join((title or "").split())
        if len(title.split()) >= 2:
            out.append(LiveQuery(mid, "title", title))
        rare = rare_tokens(f"{title} {content[:4000]}", df)
        if rare:
            out.append(LiveQuery(mid, "rare_token", " ".join(rare)))
    return out


# ---------------------------------------------------------------------------
# Runner (backend-agnostic: arms are plain callables)
# ---------------------------------------------------------------------------


def _pct(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def _rr(got: Sequence[str], gold: str) -> float:
    for rank, i in enumerate(got, 1):
        if i == gold:
            return 1.0 / rank
    return 0.0


def run_live(
    arms: dict[str, Arm],
    queries: Sequence[LiveQuery],
    *,
    alive: dict[str, tuple[bool, str]],
    embed_interval_s: float = DEFAULT_EMBED_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    """Run every arm on every query. ``fallback_chain`` is derived, not called.

    An arm whose health said dead is NOT called and reports ``dead``. An arm that
    was alive but raised on, or returned nothing for, every query is reported
    ``dead`` too (with the reason), so a silent embed outage cannot pass as a
    recall of zero.
    """
    rows: list[dict] = []
    last_embed = -1e9
    for q in queries:
        per: dict[str, dict] = {}
        for name in ARMS:
            if name == "fallback_chain":
                continue
            if not alive.get(name, (False, "not wired"))[0]:
                per[name] = {"skipped": True}
                continue
            if name in EMBEDDING_ARMS and embed_interval_s > 0:
                wait = embed_interval_s - (clock() - last_embed)
                if wait > 0:
                    sleep(wait)
                last_embed = clock()
            t0 = clock()
            try:
                got = list(arms[name](q.text))
                err = None
            except Exception as exc:  # noqa: BLE001
                got, err = [], type(exc).__name__
            per[name] = {"ids": got[:LIMIT], "ms": (clock() - t0) * 1000.0, "error": err}
        vec, lex = per.get("vector_only"), per.get("bm25_only")
        if "vector_only" in per and "bm25_only" in per and not vec.get("skipped"):
            if vec["ids"] or lex.get("skipped"):
                per["fallback_chain"] = dict(vec)
            else:
                per["fallback_chain"] = {
                    "ids": lex.get("ids", []),
                    "ms": vec["ms"] + lex.get("ms", 0.0),
                    "error": lex.get("error"),
                }
        elif lex and not lex.get("skipped"):
            per["fallback_chain"] = dict(lex)
        else:
            per["fallback_chain"] = {"skipped": True}
        rows.append({"id": q.id, "kind": q.kind, "per": per})

    return _summarise(rows, alive)


def _summarise(rows: list[dict], alive: dict[str, tuple[bool, str]]) -> dict:
    kinds = sorted({r["kind"] for r in rows})
    arms_out: dict[str, dict] = {}
    for name in ARMS:
        calls = [r["per"][name] for r in rows if not r["per"].get(name, {}).get("skipped")]
        ok = [c for c in calls if not c["error"]]
        if name == "fallback_chain":
            up = alive.get("vector_only", (False, ""))[0] or alive.get("bm25_only", (False, ""))[0]
            health = (up, "derived from vector_only and bm25_only")
        else:
            health = alive.get(name, (False, "not wired"))
        status, reason = ("alive", health[1]) if health[0] else ("dead", health[1])
        if health[0] and calls:
            if len(ok) == 0:
                status = "dead"
                reason = f"raised on all {len(calls)} queries"
            elif not any(c["ids"] for c in ok):
                status = "dead"
                reason = f"healthy but returned nothing for all {len(ok)} queries"
        entry: dict = {
            "status": status,
            "reason": reason,
            "calls": len(calls),
            "errors": len(calls) - len(ok),
            "empty": sum(1 for c in ok if not c["ids"]),
        }
        lat = [c["ms"] for c in ok]
        entry["p50_ms"], entry["p95_ms"] = _pct(lat, 50), _pct(lat, 95)
        by_kind: dict[str, dict] = {}
        for kind in [*kinds, "all"]:
            sel = [
                (r, r["per"][name])
                for r in rows
                if (kind == "all" or r["kind"] == kind)
                and not r["per"].get(name, {}).get("skipped")
                and not r["per"][name]["error"]
            ]
            by_kind[kind] = {
                "n": len(sel),
                "recall@5": mean(recall_at_k(c["ids"], {r["id"]}, K) for r, c in sel)
                if sel
                else None,
                "mrr": mean(_rr(c["ids"], r["id"]) for r, c in sel) if sel else None,
            }
        entry["by_kind"] = by_kind
        arms_out[name] = entry
    return {
        "queries": len(rows),
        "kinds": kinds,
        "arms": arms_out,
        "rows": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "per": {
                    n: (
                        {"skipped": True}
                        if c.get("skipped")
                        else {
                            "rank": (c["ids"].index(r["id"]) + 1 if r["id"] in c["ids"] else None),
                            "n": len(c["ids"]),
                            "ms": round(c["ms"], 2),
                            "error": c["error"],
                        }
                    )
                    for n, c in r["per"].items()
                },
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Table (metrics and ids only: no titles, no content)
# ---------------------------------------------------------------------------


def _f(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def _ms(v) -> str:
    return "-" if v is None else f"{v:.0f}"


def format_table(res: dict, meta: dict | None = None) -> str:
    meta = meta or {}
    out = [
        "skmemory LIVE self-retrieval (SKMEM-P1-03)",
        "- BIAS: SELF-RETRIEVAL. Each query is cut from the memory it must find (its title, or "
        "its rarest tokens). Easy for lexical arms; says little about semantic recall on a "
        "paraphrased question. Compare arms on identical queries; do not read as absolute quality.",
        "- rare tokens are rare relative to the SAMPLE (document frequency proxy), not the corpus",
        "- candidate = PRODUCTION MemoryStore.search_fused (fusion + rerank), record_access=False",
        "- not reported (no live analogue): reimpl_fusion, full_context",
    ]
    for k, v in meta.items():
        out.append(f"- {k}: {v}")
    out.append(f"- {res['queries']} queries; ids and metrics only, no memory text\n")
    out += ["arm health", "-" * 10]
    for n in ARMS:
        a = res["arms"][n]
        tag = "ALIVE" if a["status"] == "alive" else "DEAD"
        out.append(
            f"{n:<15}{tag:<6}calls={a['calls']} errors={a['errors']} empty={a['empty']}  "
            f"{a['reason']}"
        )
    out.append("")
    for metric in ("recall@5", "mrr"):
        hdr = f"{metric:<12}" + "".join(f"{n:>16}" for n in ARMS)
        out += [hdr, "-" * len(hdr)]
        for kind in [*res["kinds"], "all"]:
            cells = []
            for n in ARMS:
                a = res["arms"][n]
                cells.append("dead" if a["status"] == "dead" else _f(a["by_kind"][kind][metric]))
            out.append(f"{kind:<12}" + "".join(f"{c:>16}" for c in cells))
        out.append("")
    hdr = f"{'latency ms':<12}" + "".join(f"{n:>16}" for n in ARMS)
    out += [hdr, "-" * len(hdr)]
    for label, key in (("p50", "p50_ms"), ("p95", "p95_ms")):
        out.append(
            f"{label:<12}"
            + "".join(
                f"{('dead' if res['arms'][n]['status'] == 'dead' else _ms(res['arms'][n][key])):>16}"
                for n in ARMS
            )
        )
    out.append("(vector_only and candidate latency include the embed call)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Output files (mode 600, never inside the repo or the agent home)
# ---------------------------------------------------------------------------


def check_out_dir(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser().resolve()
    banned = [Path.home() / ".skcapstone", Path(__file__).resolve().parents[2]]
    for b in banned:
        b = b.resolve()
        if p == b or b in p.parents:
            raise ReadOnlyViolation(f"refusing to write output under {b}")
    return p


def write_outputs(out_dir: str | os.PathLike, res: dict, table: str, stem: str) -> list[Path]:
    d = check_out_dir(out_dir)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = []
    for name, body in ((f"{stem}.table.txt", table + "\n"), (f"{stem}.rows.json", None)):
        path = d / name
        text = body if body is not None else json.dumps(res, indent=2, sort_keys=True, default=str)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Read-only wrappers over the real backends
# ---------------------------------------------------------------------------


def _blocked(name: str):
    def method(self, *a, **kw):
        raise ReadOnlyViolation(f"{type(self).__name__}.{name} is blocked in the live bench")

    method.__name__ = name
    return method


def _make_read_only(cls, names: Sequence[str]) -> None:
    for n in names:
        if hasattr(cls, n):
            setattr(cls, n, _blocked(n))


def _set_session_read_only(self, conn):
    if getattr(self, "_ro_conn_id", None) != id(conn):
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        self._ro_conn_id = id(conn)
    return conn


def make_backends(dsn: str | None, agent: str, sqlite_dir: str):
    """Construct (pg, age, sqlite) read-only wrappers. Imports are lazy."""
    from skmemory.backends.age_backend import AGEGraphBackend
    from skmemory.backends.pgvector_backend import PGVectorBackend
    from skmemory.backends.sqlite_backend import SQLiteBackend

    class ReadOnlyPG(PGVectorBackend):
        def _connection(self):
            return _set_session_read_only(self, super()._connection())

    class ReadOnlyAGE(AGEGraphBackend):
        def _conn(self):
            conn = super()._conn()
            return None if conn is None else _set_session_read_only(self, conn)

    class ReadOnlySQLite(SQLiteBackend):
        def __init__(self, base_path: str) -> None:  # no super().__init__: it mkdirs + migrates
            self.base_path = Path(base_path)
            self._db_path = self.base_path / "index.db"
            self._conn = None
            self._unloadable_seen: set[str] = set()
            self.last_import_report = None

        def _get_conn(self):
            if self._conn is None:
                uri = f"file:{urllib.parse.quote(str(self._db_path))}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout=30000")
                self._conn.execute("PRAGMA query_only=ON")
            return self._conn

        def _recover_from_corruption(self, err):
            raise ReadOnlyViolation(f"index needs repair, refusing to rebuild live: {err}")

    _make_read_only(ReadOnlyPG, ["save", "delete", "remove", "build_forget_plan", "forget_plan"])
    _make_read_only(
        ReadOnlyAGE,
        [
            "save", "delete", "remove_memory", "index_memory", "ensure_graph",
            "ensure_property_indexes", "delete_orphaned_aux_nodes", "convert_supersedes_to_lineage",
        ],
    )  # fmt: skip
    _make_read_only(
        ReadOnlySQLite,
        [
            "save", "delete", "reindex", "import_backup", "_index_memory", "_init_schema",
            "_ensure_db", "_ensure_dirs", "_quarantine_corrupt_db", "_bump_access",
        ],
    )  # fmt: skip
    pg = ReadOnlyPG(**({"dsn": dsn} if dsn else {}), agent=agent)
    age = ReadOnlyAGE(dsn=dsn, agent=agent)
    return pg, age, ReadOnlySQLite(sqlite_dir)


def verify_read_only(pg, age, sqlite_backend) -> dict:
    """Prove each guard by attempting a write that MUST fail. Raises otherwise."""
    report = {}
    conn = sqlite_backend._get_conn()
    try:
        conn.execute("CREATE TABLE _skmem_ro_probe (x INTEGER)")
    except sqlite3.Error:
        report["sqlite"] = "write refused"
    else:
        conn.execute("DROP TABLE _skmem_ro_probe")
        raise ReadOnlyViolation("sqlite accepted a write")
    for label, c in (("pg", pg._connection()), ("age", age._conn())):
        if c is None:
            report[label] = "no connection"
            continue
        if c.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise ReadOnlyViolation(f"{label} session is not read-only")
        try:
            c.execute("CREATE TEMP TABLE _skmem_ro_probe (x int)")
        except Exception:  # noqa: BLE001 - psycopg ReadOnlySqlTransaction
            report[label] = "write refused"
        else:
            c.execute("DROP TABLE _skmem_ro_probe")
            raise ReadOnlyViolation(f"{label} accepted a write")
    return report


class _NullWAL:
    """Stands in for the store's write-ahead log, which would mkdir under the agent home."""

    def __init__(self, *a, **kw) -> None:
        pass

    def __getattr__(self, name):
        return lambda *a, **kw: None


def build_store(pg, age, sqlite_backend):
    import skmemory.store as store_mod

    orig = store_mod.WriteAheadLog
    store_mod.WriteAheadLog = _NullWAL
    try:
        return store_mod.MemoryStore(primary=sqlite_backend, vector=pg, graph=age)
    finally:
        store_mod.WriteAheadLog = orig


def probe_arms(pg, age, sqlite_backend) -> dict[str, tuple[bool, str]]:
    """Health of each arm from the backends themselves (never from a recall of zero)."""
    alive: dict[str, tuple[bool, str]] = {}
    h = pg.health_check()
    pg_up = bool(h.get("memories")) and "error" not in h
    embed_ok = (h.get("embed_probe") or {}).get("ok")
    why = f"pg memories={h.get('memories')} unparseable={h.get('unparseable_rows')}"
    if h.get("error"):
        why = f"pg error: {str(h['error'])[:120]}"
    alive["bm25_only"] = (pg_up, why + (" bm25" if pg_up and pg._has_bm25_index() else " ts_rank"))
    probe = h.get("embed_probe") or {}
    alive["vector_only"] = (
        pg_up and embed_ok is True,
        f"embed_ok={embed_ok} latency_ms={probe.get('latency_ms')} {probe.get('error', '')}".strip(),
    )
    g = age.health_check()
    alive["graph_only"] = (bool(g.get("ok")), f"nodes={g.get('node_count')} {g.get('error', '')}")
    try:
        n = sqlite_backend._get_conn().execute("SELECT count(*) FROM memories").fetchone()[0]
        alive["sqlite_like"] = (n > 0, f"index rows={n} (opened mode=ro)")
    except sqlite3.Error as exc:
        alive["sqlite_like"] = (False, f"sqlite error: {exc}")
    alive[CANDIDATE] = (
        alive["bm25_only"][0] or alive["vector_only"][0] or alive["graph_only"][0],
        "fused over whichever of the above are alive",
    )
    return alive


def build_arms(pg, age, sqlite_backend, store, telemetry: dict) -> dict[str, Arm]:
    from skmemory.query_sanitizer import sanitize_query

    def candidate(q: str) -> list[str]:
        out = store.search_fused(
            sanitize_query(q), limit=LIMIT, return_report=True, record_access=False
        )
        mems, report = out
        for a in report.arms:
            telemetry.setdefault(a.name, Counter())[a.status] += 1
        return [m.id for m in (mems or [])]

    return {
        "bm25_only": lambda q: [m.id for m in pg.search_lexical(q, limit=LIMIT)],
        "vector_only": lambda q: [m.id for m in pg.search_semantic(q, limit=LIMIT)],
        "sqlite_like": lambda q: [m.id for m in sqlite_backend.search_text(q, limit=LIMIT)],
        "graph_only": lambda q: [
            str(r["id"]) for r in age.search_by_entity_tokens(q, limit=LIMIT) if r.get("id")
        ],
        CANDIDATE: candidate,
    }


def sample_from_pg(pg, n: int, seed: int) -> list[tuple[str, str, str]]:
    conn = pg._connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM memories WHERE agent=%s", (pg.agent,))
        ids = [r[0] for r in cur.fetchall()]
    out = []
    for mid in sample_ids(ids, n, seed):
        m = pg.load(mid)
        if m is not None:
            out.append((m.id, m.title or "", m.content or ""))
    return out


def run(
    *,
    out_dir: str,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    agent: str | None = None,
    embed_interval_s: float = DEFAULT_EMBED_INTERVAL_S,
) -> dict:
    require_live()
    check_out_dir(out_dir)
    from skmemory.agents import get_active_agent, require_memory_profile
    from skmemory.backend_select import resolve_pg_dsn

    agent = require_memory_profile(agent or get_active_agent()).profile_id
    home = Path.home() / ".skcapstone" / "agents" / agent / "memory"
    pg, age, sq = make_backends(resolve_pg_dsn(), agent, str(home))
    guards = verify_read_only(pg, age, sq)
    alive = probe_arms(pg, age, sq)
    store = build_store(pg, age, sq)
    mems = sample_from_pg(pg, n, seed)
    queries = build_queries(mems)
    telemetry: dict = {}
    arms = build_arms(pg, age, sq, store, telemetry)
    res = run_live(arms, queries, alive=alive, embed_interval_s=embed_interval_s)
    res["meta"] = {
        "agent": agent,
        "sample": len(mems),
        "seed": seed,
        "embed_interval_s": embed_interval_s,
        "read_only_guards": guards,
        "candidate_fusion_arm_status": {k: dict(v) for k, v in telemetry.items()},
    }
    for c in (pg, age):
        with contextlib.suppress(Exception):
            (getattr(c, "_conn_obj", None) or getattr(c, "_conn", None)).close()
    return res


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="skmemory live self-retrieval bench (read-only)")
    p.add_argument("--out", required=True, help="output dir (outside the repo and ~/.skcapstone)")
    p.add_argument("-n", type=int, default=DEFAULT_N)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--agent")
    p.add_argument("--embed-interval", type=float, default=DEFAULT_EMBED_INTERVAL_S)
    args = p.parse_args()
    res = run(
        out_dir=args.out,
        n=args.n,
        seed=args.seed,
        agent=args.agent,
        embed_interval_s=args.embed_interval,
    )
    meta = res["meta"]
    table = format_table(
        res,
        {
            "agent": meta["agent"],
            "sample": f"{meta['sample']} memories, seed {meta['seed']}",
            "read-only guards": ", ".join(
                f"{k}: {v}" for k, v in meta["read_only_guards"].items()
            ),
            "candidate fused-arm status": json.dumps(meta["candidate_fusion_arm_status"]),
        },
    )
    stamp = time.strftime("%Y%m%dT%H%M%S")
    paths = write_outputs(args.out, res, table, f"live-{meta['agent']}-{stamp}")
    print(table)
    print("\nwrote: " + ", ".join(str(x) for x in paths))


if __name__ == "__main__":
    main()
