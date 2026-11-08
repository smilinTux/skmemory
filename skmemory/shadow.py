"""Shadow-read parity: ``SKMEMORY_SHADOW=rust`` (SKMEM-R-11).

With ``SKMEMORY_ENGINE=python`` (the default) and ``SKMEMORY_SHADOW=rust``, every
``MemoryStore.search`` ALSO runs the Rust engine (``skmem_core``) on a background
thread, always serves the Python result, and appends ONE row per query to
``<agent>/logs/shadow.jsonl``: top-5 id overlap, both latencies, per-arm status
for both engines. The row holds no query text and no memory content, only a
12 character query hash, a word count and memory ids (the same privacy rule as
``retrieval.jsonl``).

The shadow can never change or fail the served result:

* the Rust call runs on a daemon thread; the caller only pays for starting it;
* a skmem_core without ``RELEASES_GIL = True`` (a build that holds the GIL for the
  whole search, which would stall the served Python search and the watchdog below)
  is refused: one row with ``error: skmem_core_holds_gil``, then the shadow is off
  for the process;
* a watchdog timer (``SKMEMORY_SHADOW_TIMEOUT_S``, default 5) writes the row and
  gives up on a Rust call that has not returned;
* every Rust error, a missing ``skmem_core`` included, is a row with an ``error``
  field, never an exception;
* at most ``MAX_INFLIGHT`` shadow searches run at once, the rest are logged as
  ``error: busy`` and skipped;
* the Rust engine is opened READ-ONLY and asked for ``record_access=False``: no
  reinforcement, ledger or index write comes from the Rust side;
* ``reader`` goes to both engines unchanged (the process ceiling is read from
  ``SKMEMORY_READER`` by both).

``python -m skmemory.shadow`` (or ``skmemory-shadow-report``) summarises the file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import engine as _engine
from . import retrieval_log

logger = logging.getLogger(__name__)

ENV = "SKMEMORY_SHADOW"
TIMEOUT_ENV = "SKMEMORY_SHADOW_TIMEOUT_S"
LOG_NAME = "shadow.jsonl"
DEFAULT_TIMEOUT_S = 5.0
MAX_INFLIGHT = 4
TOP_K = 5

_inflight = threading.BoundedSemaphore(MAX_INFLIGHT)
_disabled = False  # set once skmem_core is found to hold the GIL


class _HoldsGil(Exception):
    """skmem_core predates GIL release: a shadow thread would stall the served search."""


def enabled() -> bool:
    """Shadow runs only for ``SKMEMORY_SHADOW=rust`` while the served engine is python."""
    if _disabled or os.environ.get(ENV, "").strip().lower() != "rust":
        return False
    try:
        return _engine.selected() == _engine.PYTHON
    except _engine.EngineConfigError:
        return False


def _timeout() -> float:
    try:
        v = float(os.environ.get(TIMEOUT_ENV, ""))
        return v if v > 0 else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S


def overlap(py_ids: list[str], rs_ids: list[str], k: int = TOP_K) -> float:
    """|top-k(py) & top-k(rs)| / max(len): 1.0 when both are empty."""
    a, b = list(py_ids[:k]), list(rs_ids[:k])
    if not a and not b:
        return 1.0
    return len(set(a) & set(b)) / max(len(a), len(b))


class _Engines:
    """Read-only ``skmem_core.Engine`` per (memory dir, ceiling, pg, index identity)."""

    _cache: dict[tuple, Any] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, memory_dir: str | Path, pg: bool) -> Any:
        mod = _engine.rust_module()
        if getattr(mod, "RELEASES_GIL", False) is not True:
            raise _HoldsGil
        os.environ.setdefault(_engine.AUDIENCE_ENV, str(_engine.packaged_audience_config()))
        cfg = str(_engine.packaged_audience_config())
        key = (str(memory_dir), _engine.ceiling(), pg, _engine._index_identity(memory_dir))
        with cls._lock:
            eng = cls._cache.get(key)
            if eng is None:
                eng = mod.Engine(str(memory_dir), cfg, read_only=True, pg=pg)
                cls._cache[key] = eng
                # keep Python's SQLite from being the last connection out (see engine.py)
                _engine.RustEngines._keep_python_sqlite_open(memory_dir)
        return eng


class _Run:
    """One shadowed search. The row is written exactly once, by whoever finishes last."""

    def __init__(self, log_dir: Path | None, query: str):
        self.log_dir = log_dir
        self.qhash = retrieval_log.query_hash(query)
        self.qlen = len((query or "").split())
        self.lock = threading.Lock()
        self.emitted = False
        self.py: dict | None = None
        self.rs: dict | None = None
        self.timer: threading.Timer | None = None

    def set_py(self, ids: list[str], ms: float, arms: dict | None) -> None:
        with self.lock:
            self.py = {"ids": ids, "ms": ms, "arms": arms}
        self._maybe_emit()

    def set_rs(self, rs: dict) -> None:
        with self.lock:
            self.rs = rs
        self._maybe_emit()

    def _maybe_emit(self, force: bool = False) -> None:
        with self.lock:
            if self.emitted or (not force and (self.py is None or self.rs is None)):
                return
            self.emitted = True
            py, rs = self.py, self.rs
        if self.timer is not None:
            self.timer.cancel()
        try:
            self._write(py, rs)
        except Exception as exc:  # noqa: BLE001 - the shadow never raises
            logger.debug("shadow row dropped: %s", exc)

    def _write(self, py: dict | None, rs: dict | None) -> None:
        if self.log_dir is None:
            return
        error = None
        if rs is None:
            error = "timeout"
        elif rs.get("error"):
            error = rs["error"]
        if py is None:
            error = error or "python_no_result"
        row: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "q": self.qhash,
            "qlen": self.qlen,
            "k": TOP_K,
            "py_ids": (py or {}).get("ids", [])[:TOP_K],
            "rs_ids": (rs or {}).get("ids", [])[:TOP_K],
            "py_ms": round((py or {}).get("ms", 0.0), 1) if py else None,
            "rs_ms": round(rs["ms"], 1) if rs and rs.get("ms") is not None else None,
            "rs_open_ms": round(rs["open_ms"], 1) if rs and rs.get("open_ms") else None,
            "py_arms": (py or {}).get("arms"),
            "rs_arms": (rs or {}).get("arms"),
        }
        if error:
            row["error"] = error
            row["overlap"] = None
        else:
            row["overlap"] = round(overlap(row["py_ids"], row["rs_ids"]), 4)
        retrieval_log._write(
            Path(self.log_dir) / LOG_NAME, [json.dumps(row, separators=(",", ":"))]
        )

    def timed_out(self) -> None:
        self._maybe_emit(force=True)


def _rust_worker(run: _Run, store: Any, query: str, limit: int, kw: dict) -> None:
    started = time.perf_counter()
    result: dict[str, Any] = {"ids": [], "ms": None, "arms": None, "open_ms": None}
    try:
        base = getattr(store.primary, "base_path", None)
        eng = _Engines.get(base, pg=store.vector is not None)
        # engine open (first query only) is reported apart so it never pads rs_ms
        result["open_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        ctx = kw.get("context_tag")
        ctxs = set(ctx) if isinstance(ctx, (list, tuple, set)) else None
        layer = kw.get("layer")
        out = eng.search_report(
            query,
            limit,
            tags=kw.get("tags"),
            layer=getattr(layer, "value", layer),
            source=kw.get("source"),
            context_tag=None if ctxs is not None else ctx,
            record_access=False,  # read-only: no reinforcement from the Rust side
            reader=_engine.rust_reader(kw.get("reader")),
            as_of=kw.get("as_of"),
        )
        rows = out["results"]
        if ctxs is not None:
            rows = [r for r in rows if r.get("context_tag") in ctxs]
        result["ids"] = [r["id"] for r in rows]
        result["arms"] = {a["name"]: a["status"] for a in out["report"].get("arms", [])}
    except _HoldsGil:
        global _disabled
        _disabled = True  # one error row (this one), then the shadow is off for the process
        result["error"] = "skmem_core_holds_gil"
    except BaseException as exc:  # noqa: BLE001 - swallowed and logged, whatever it is
        # type name only: an exception message can quote query text
        result["error"] = (
            "skmem_core_missing"
            if isinstance(exc, (ImportError, _engine.EngineConfigError))
            else type(exc).__name__
        )
    finally:
        result["ms"] = (time.perf_counter() - started) * 1000
        _inflight.release()
        run.set_rs(result)


def begin(store: Any, query: str, limit: int, **kw: Any) -> _Run | None:
    """Start the Rust shadow for one search. Returns a handle or None. Never raises."""
    try:
        if not enabled() or retrieval_log._measuring.get():
            return None
        from .backends.sqlite_backend import SQLiteBackend

        if type(store.primary) is not SQLiteBackend:
            return None
        if getattr(store.primary, "base_path", None) is None:
            return None
        run = _Run(store._retrieval_log_dir(), query)
        if not _inflight.acquire(blocking=False):
            run.set_rs({"ids": [], "ms": None, "arms": None, "error": "busy"})
            return run
        store._search_tls.arms = None
        run.timer = threading.Timer(_timeout(), run.timed_out)
        run.timer.daemon = True
        run.timer.start()
        threading.Thread(
            target=_rust_worker,
            args=(run, store, query, limit, kw),
            name="skmem-shadow",
            daemon=True,
        ).start()
        return run
    except Exception as exc:  # noqa: BLE001
        logger.debug("shadow not started: %s", exc)
        return None


def python_done(run: _Run | None, store: Any, kept: list, started: float) -> None:
    """Hand the served Python result to the shadow. Never raises, never blocks."""
    if run is None:
        return
    try:
        arms = getattr(store._search_tls, "arms", None)
        run.set_py([m.id for m in kept], (time.perf_counter() - started) * 1000, arms)
    except Exception as exc:  # noqa: BLE001
        logger.debug("shadow python side dropped: %s", exc)


# -- report ----------------------------------------------------------------------------


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def summarize(rows: list[dict]) -> dict:
    """Mean top-5 overlap, p50/p95 latency per engine, and every divergence."""
    ok = [r for r in rows if not r.get("error") and r.get("overlap") is not None]
    errors: dict[str, int] = {}
    for r in rows:
        if r.get("error"):
            errors[r["error"]] = errors.get(r["error"], 0) + 1
    div = [
        {
            "q": r["q"],
            "ts": r.get("ts"),
            "overlap": r["overlap"],
            "py_ids": r.get("py_ids"),
            "rs_ids": r.get("rs_ids"),
        }
        for r in ok
        if r["overlap"] < 1.0 or r.get("py_ids") != r.get("rs_ids")
    ]
    py_ms = [r["py_ms"] for r in ok if r.get("py_ms") is not None]
    rs_ms = [r["rs_ms"] for r in ok if r.get("rs_ms") is not None]
    return {
        "rows": len(rows),
        "compared": len(ok),
        "errors": errors,
        "mean_top5_overlap": (sum(r["overlap"] for r in ok) / len(ok)) if ok else None,
        "python_ms": {"p50": _pct(py_ms, 0.5), "p95": _pct(py_ms, 0.95)},
        "rust_ms": {"p50": _pct(rs_ms, 0.5), "p95": _pct(rs_ms, 0.95)},
        "divergences": len(div),
        "divergent": div,
    }


def read_rows(path: Path, since: float | None = None) -> list[dict]:
    """Rows of ``shadow.jsonl`` and its rotated siblings, oldest first."""
    files = [path.with_name(f"{path.name}.{n}") for n in (3, 2, 1)] + [path]
    out: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and (since is None or r.get("ts", 0) >= since):
                out.append(r)
    return out


def format_report(s: dict, days: float | None = None) -> str:
    def f(v, nd=4):
        return "n/a" if v is None else f"{v:.{nd}f}"

    span = f" (last {days:g} days)" if days else ""
    lines = [
        f"shadow-read parity{span}",
        f"rows {s['rows']}, compared {s['compared']}, errors {sum(s['errors'].values())}"
        + (f" {s['errors']}" if s["errors"] else ""),
        f"mean top-5 overlap {f(s['mean_top5_overlap'])}  (target >= 0.99)",
        f"python ms p50 {f(s['python_ms']['p50'], 1)}  p95 {f(s['python_ms']['p95'], 1)}",
        f"rust   ms p50 {f(s['rust_ms']['p50'], 1)}  p95 {f(s['rust_ms']['p95'], 1)}"
        "  (target rust p95 <= python p95)",
        f"divergences {s['divergences']}",
    ]
    for d in s["divergent"]:
        lines.append(f"  q={d['q']} overlap={d['overlap']} py={d['py_ids']} rs={d['rs_ids']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="skmemory-shadow-report", description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="shadow.jsonl (default: the active agent's logs dir)")
    ap.add_argument("--days", type=float, help="only the last N days")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    if a.file:
        path = Path(a.file)
    else:
        from .agents import get_agent_paths

        path = Path(get_agent_paths()["logs"]) / LOG_NAME
    since = time.time() - a.days * 86400 if a.days else None
    s = summarize(read_rows(path, since))
    print(json.dumps(s, indent=2) if a.json else format_report(s, a.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
