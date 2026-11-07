"""Retrieval observability: a per-search JSONL log and arm liveness from it.

One line per fused search in ``<agent>/logs/retrieval.jsonl``: per-arm status,
hit count and latency, plus the spread of the fused (RRF) score over the
returned ids. Query text is NEVER logged, only a short hash and counts.

Writes never touch the search path: ``record`` puts a small dict on a bounded
queue and a daemon thread appends it. A full queue or a failing disk drops the
record. The file is the state, so liveness works across short-lived CLI runs
(there are no per-process counters).

Liveness (see ``arm_liveness``) departs from "zero hits for 100 searches means
degraded": an arm that abstains or finds no match is normal. Degraded is based
on errors and timeouts; a zero-hit streak only degrades when the arm's backend
probe also fails.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import fcntl
import hashlib
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_NAME = "retrieval.jsonl"
WINDOW = 100
MAX_BYTES = 5 * 1024 * 1024
KEEP = 3
#: degraded when RECENT_BAD of the last RECENT attempts were bad, or the last TAIL all were
RECENT = 20
RECENT_BAD = 3
TAIL = 3
_QUEUE_MAX = 1000
_ENV = "SKMEMORY_RETRIEVAL_LOG"

_q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_idle = threading.Event()
_idle.set()


_measuring: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "skmemory_retrieval_log_measuring", default=False
)


@contextlib.contextmanager
def measurement():
    """Searches made inside this block write no retrieval record.

    Benchmarks enter it so their queries do not pad the 100-record window
    ``arm_liveness`` reads. A ContextVar is per thread and per asyncio task, so a
    normal search running concurrently elsewhere still logs.
    """
    token = _measuring.set(True)
    try:
        yield
    finally:
        _measuring.reset(token)


def enabled() -> bool:
    return os.environ.get(_ENV, "on").strip().casefold() not in ("off", "0", "false", "no")


def query_hash(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8", "replace")).hexdigest()[:12]


def build_record(query: str, report) -> dict:
    """One log record from a FusionResult. Holds no query text."""
    from .fusion import RRF_K

    scores = sorted(
        (
            sum(1.0 / (RRF_K + r) for r in report.provenance.get(i, {}).values())
            for i in report.ids
        ),
        reverse=True,
    )
    dist = None
    if scores:
        dist = {
            "n": len(scores),
            "max": round(scores[0], 5),
            "median": round(scores[len(scores) // 2], 5),
            "min": round(scores[-1], 5),
        }
    return {
        "ts": round(time.time(), 3),
        "q": query_hash(query),
        "qlen": len((query or "").split()),
        "results": len(report.ids),
        "elapsed_ms": round(report.elapsed_s * 1000, 1),
        "arms": {
            a.name: {
                "status": a.status,
                "hits": len(a.ids),
                "ms": round(a.elapsed_s * 1000, 1),
            }
            for a in report.arms
        },
        "fused": dist,
    }


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for n in range(KEEP - 1, 0, -1):
        src = path.with_name(f"{path.name}.{n}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{n + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def _write(p: Path, lines: list[str]) -> None:
    """Append under an flock so two processes cannot interleave or clobber a rotation."""
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(p.parent, 0o700)
    lock_fd = os.open(str(p) + ".lock", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _rotate(p)
        fd = os.open(p, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    finally:
        os.close(lock_fd)


def _drain() -> None:
    while True:
        path, rec = _q.get()
        batch: dict[Path, list[str]] = {path: [json.dumps(rec, separators=(",", ":"))]}
        while True:
            try:
                p, r = _q.get_nowait()
            except queue.Empty:
                break
            batch.setdefault(p, []).append(json.dumps(r, separators=(",", ":")))
        for p, lines in batch.items():
            try:
                _write(p, lines)
            except Exception as exc:  # noqa: BLE001 - best effort by design
                logger.debug("retrieval log write dropped: %s", exc)
        if _q.empty():
            _idle.set()


def record(log_dir: Path | None, rec: dict) -> None:
    """Queue ``rec`` for writing. Never raises, never blocks."""
    global _thread
    if log_dir is None or not enabled() or _measuring.get():
        return
    try:
        _idle.clear()
        _q.put_nowait((Path(log_dir) / LOG_NAME, rec))
        with _lock:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_drain, name="skmem-retrieval-log", daemon=True)
                _thread.start()
    except Exception:  # noqa: BLE001 - queue full or thread failure: drop
        pass


def flush(timeout: float = 2.0) -> None:
    """Wait (bounded) for queued records to reach disk."""
    _idle.wait(timeout)


atexit.register(flush, 1.0)


def read_window(log_dir: Path | None, n: int = WINDOW) -> list[dict]:
    """The last ``n`` records, oldest first. Reads only the file tail."""
    if log_dir is None:
        return []
    flush()
    path = Path(log_dir) / LOG_NAME
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


#: arm -> which backend health block vouches for it
ARM_BACKEND = {
    "semantic": "vector",
    "lexical": "vector",
    "lexical_local": "primary",
    "graph": "graph",
    "temporal": "primary",
}


def arm_liveness(records: list[dict], states: dict[str, dict], backend_ok: dict) -> dict:
    """Per-arm liveness over ``records``.

    ``states``: store arm config states (a ``disabled`` arm never degrades).
    ``backend_ok``: role -> True/False/None from the health probes.
    Returns ``{arm: {state, searches, attempts, hits, errors, timeouts, reason}}``
    where state is ok, quiet, disabled, unavailable, or degraded.
    """
    names: list[str] = []
    for rec in records:
        for name in rec.get("arms", {}):
            if name not in names:
                names.append(name)
    out: dict[str, dict] = {}
    for name in names:
        cfg = (states.get(name) or {}).get("state")
        rows = [r["arms"][name] for r in records if name in r.get("arms", {})]
        attempts = [x for x in rows if x.get("status") in ("ok", "abstained", "failed", "timeout")]
        errors = sum(1 for x in attempts if x["status"] == "failed")
        timeouts = sum(1 for x in attempts if x["status"] == "timeout")
        hits = sum(int(x.get("hits", 0)) for x in attempts)
        info = {
            "state": "ok",
            "searches": len(rows),
            "attempts": len(attempts),
            "hits": hits,
            "errors": errors,
            "timeouts": timeouts,
            "reason": None,
        }
        if cfg == "disabled" or (rows and all(x.get("status") == "disabled" for x in rows)):
            info.update(state="disabled", reason="disabled by config")
        elif attempts:
            flags = [x["status"] in ("failed", "timeout") for x in attempts]
            recent = flags[-RECENT:]
            if len(flags) >= TAIL and all(flags[-TAIL:]):
                info.update(state="degraded", reason=f"last {TAIL} attempts bad")
            elif sum(recent) >= RECENT_BAD:
                info.update(
                    state="degraded", reason=f"{sum(recent)} of last {len(recent)} attempts bad"
                )
            elif hits == 0 and len(attempts) >= WINDOW:
                if backend_ok.get(ARM_BACKEND.get(name, "primary")) is False:
                    info.update(state="degraded", reason="zero hits and backend probe failing")
                else:
                    info.update(state="quiet", reason="zero hits, backend probe ok")
        elif rows:
            info.update(state="unavailable", reason="arm not attached")
        out[name] = info
    return out
