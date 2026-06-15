#!/usr/bin/env python3
"""Migrate flat-file memories into the local skmem-pg (pgvector) store, embedding
each via the configured mxbai endpoint.

Resumable: skips memory ids that already have a non-null embedding in pg.
Run:  SKAGENT=lumina python scripts/migrate-flat-to-pgvector.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skmemory.backends.file_backend import FileBackend
from skmemory.backends.pgvector_backend import (
    DEFAULT_EMBED_MODEL,
    DEFAULT_EMBED_URL,
    PGVectorBackend,
)
from skmemory.models import MemoryLayer

AGENT = os.environ.get("SKAGENT", "lumina")
BASE = os.environ.get(
    "SKMEMORY_BASE",
    str(Path.home() / ".skcapstone" / "agents" / AGENT / "memory"),
)


def main() -> int:
    fb = FileBackend(base_path=BASE)
    pg = PGVectorBackend(agent=AGENT)
    h = pg.health_check()
    if not h.get("ok"):
        print("PG not healthy:", h, file=sys.stderr)
        return 1
    print(f"agent={AGENT} base={BASE}")
    print(f"embed: {DEFAULT_EMBED_MODEL} @ {DEFAULT_EMBED_URL}")

    # Resume set: ids already embedded.
    conn = pg._connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM memories WHERE agent=%s AND embedding IS NOT NULL",
            (AGENT,),
        )
        done = {r[0] for r in cur.fetchall()}
    print(f"already embedded in pg: {len(done)}")

    # Gather all flat memories (dedup by id).
    mems = {}
    for lyr in MemoryLayer:
        for m in fb.list_memories(layer=lyr, limit=10_000_000):
            mems[m.id] = m
    todo = [m for mid, m in mems.items() if mid not in done]
    print(f"flat total={len(mems)}  to-embed={len(todo)}")

    workers = int(os.environ.get("MIGRATE_WORKERS", "12"))
    print(f"workers={workers}")
    _local = threading.local()
    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "n": 0}
    t0 = time.time()

    def worker(m):
        be = getattr(_local, "be", None)
        if be is None:
            be = _local.be = PGVectorBackend(agent=AGENT)
        try:
            be.save(m)  # embeds via mxbai + upserts (ON CONFLICT id)
            res = "ok"
        except Exception as exc:  # noqa: BLE001
            res = "fail"
            if counters["fail"] < 10:
                print(f"  ! {m.id}: {exc}", file=sys.stderr)
        with lock:
            counters[res] += 1
            counters["n"] += 1
            n = counters["n"]
        if n % 200 == 0:
            rate = n / max(time.time() - t0, 1e-9)
            eta = (len(todo) - n) / max(rate, 1e-9)
            print(f"  {n}/{len(todo)} ok={counters['ok']} fail={counters['fail']} "
                  f"{rate:.1f}/s eta={eta/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, todo))
    ok, fail = counters["ok"], counters["fail"]

    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(embedding) FROM memories WHERE agent=%s", (AGENT,))
        total, emb = cur.fetchone()
    print(f"DONE ok={ok} fail={fail}  pg rows={total} embedded={emb}  ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
