#!/usr/bin/env python3
"""Live measurement harness for the durable task queue (R7).

Why this file exists
---------------------
``tests/test_taskqueue.py`` proves the queue's pure logic (backoff schedule,
lease arithmetic, status transitions) offline, and two live tests exercise
the lifecycle against a real throwaway database. Neither one actually
STRESSES the concurrency claim: "N workers never collide" is the whole
reason this queue exists (see the module docstring of
``skmemory/taskqueue.py`` and the scratch-table race it replaces), so it
needs a number, not an assertion that happens to pass with two threads.
That is the same gap ``skmemory/eval/fusion_bench.py`` closes for retrieval:
a benchmark that can actually fail is worth more than a benchmark that
cannot. This harness runs real concurrent claimers against a real (throwaway)
Postgres database and prints what happened.

What it measures
-----------------
1. **Concurrency safety.** M tasks enqueued, N worker threads (independent
   connections) call ``claim_batch`` concurrently until the queue is empty.
   Passes only if every task is claimed EXACTLY once: no duplicates across
   workers, none left unclaimed.
2. **Lease recovery.** A task is claimed with a short lease and abandoned
   (never completed or failed). ``recover_orphans()`` must find nothing
   before the lease expires and must find it after.
3. **Idempotency.** The same idempotency key enqueued twice must produce
   exactly one row.

Safety
------
Operates ONLY on a throwaway database (``skmemory_queuetest``), created and
dropped by this script. It reuses the host/port/user/password from
``SKMEMORY_PG_DSN`` (or the documented local default) but ALWAYS overrides
the database name to the throwaway one; there is no flag to point it at any
other database, and it refuses (raises) if that substitution ever produces
anything but the throwaway name. Skips cleanly, printing why, when no
database is reachable at all (so this is safe to run offline/in CI without
turning into a failure).

Run:
    python -m skmemory.eval.queue_bench
    python -m skmemory.eval.queue_bench --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

THROWAWAY_DB_NAME = "skmemory_queuetest"
DEPLOY_DIR = Path(__file__).resolve().parent.parent.parent / "deploy" / "skmem-pg"
MIGRATION = DEPLOY_DIR / "04-task-queue.sql"


def _base_dsn() -> str:
    return os.environ.get(
        "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory"
    )


def _dsn_with_db(dsn: str, dbname: str):
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def _pg_reachable(dsn: str) -> bool:
    try:
        import psycopg

        conn = psycopg.connect(dsn, connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


def _require_throwaway(dsn: str) -> str:
    import psycopg

    dbname = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
    if dbname != THROWAWAY_DB_NAME:
        raise RuntimeError(
            f"refusing to operate on database {dbname!r}; this harness only ever "
            f"touches the throwaway database {THROWAWAY_DB_NAME!r}"
        )
    return dsn


def _setup_throwaway_db() -> str:
    """Create (fresh) the throwaway DB and apply the queue migration to it."""
    import psycopg

    admin_dsn = _dsn_with_db(_base_dsn(), "postgres")
    throwaway_dsn = _require_throwaway(_dsn_with_db(_base_dsn(), THROWAWAY_DB_NAME))

    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {THROWAWAY_DB_NAME};")
            cur.execute(f"CREATE DATABASE {THROWAWAY_DB_NAME};")
    finally:
        conn.close()

    migration_sql = "\n".join(
        line
        for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("\\")
    )
    conn = psycopg.connect(throwaway_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(migration_sql)
    finally:
        conn.close()

    return throwaway_dsn


def _teardown_throwaway_db() -> None:
    import psycopg

    admin_dsn = _dsn_with_db(_base_dsn(), "postgres")
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid();",
                (THROWAWAY_DB_NAME,),
            )
            cur.execute(f"DROP DATABASE IF EXISTS {THROWAWAY_DB_NAME};")
    finally:
        conn.close()


# =============================================================================
# Measurements
# =============================================================================


def measure_concurrency(dsn: str, *, num_tasks: int = 500, num_workers: int = 8) -> dict:
    """N concurrent claimers over M tasks: exactly-once claim, or it fails loudly."""
    from skmemory.taskqueue import TaskQueue

    with TaskQueue(dsn=dsn) as q:
        with q._connection().cursor() as cur:
            cur.execute("TRUNCATE task_queue RESTART IDENTITY;")
        enqueued_ids = []
        for i in range(num_tasks):
            out = q.enqueue("bench", {"i": i}, idempotency_key=f"bench-concurrency-{i}")
            enqueued_ids.append(out.task.id)

    def worker(worker_index: int) -> list[int]:
        claimed: list[int] = []
        wq = TaskQueue(dsn=dsn)
        try:
            while True:
                batch = wq.claim_batch(f"bench-worker-{worker_index}", batch_size=10)
                if not batch:
                    break
                claimed.extend(t.id for t in batch)
        finally:
            wq.close()
        return claimed

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        per_worker = list(pool.map(worker, range(num_workers)))
    elapsed = time.monotonic() - start

    all_claimed = [tid for worker_claims in per_worker for tid in worker_claims]
    unique_claimed = set(all_claimed)
    enqueued_set = set(enqueued_ids)

    duplicates = len(all_claimed) - len(unique_claimed)
    lost = len(enqueued_set - unique_claimed)
    extra = len(unique_claimed - enqueued_set)

    return {
        "tasks_enqueued": num_tasks,
        "concurrent_workers": num_workers,
        "total_claims": len(all_claimed),
        "unique_tasks_claimed": len(unique_claimed),
        "duplicates": duplicates,
        "lost": lost,
        "unexpected_extra": extra,
        "per_worker_claim_counts": [len(c) for c in per_worker],
        "elapsed_seconds": round(elapsed, 3),
        "pass": duplicates == 0 and lost == 0 and extra == 0,
    }


def measure_lease_recovery(dsn: str, *, lease_seconds: int = 2) -> dict:
    """A claimed-then-abandoned task is reclaimable only after its lease expires."""
    from skmemory.taskqueue import TaskQueue

    with TaskQueue(dsn=dsn) as q:
        with q._connection().cursor() as cur:
            cur.execute("TRUNCATE task_queue RESTART IDENTITY;")
        enq = q.enqueue("bench", {}, idempotency_key="bench-lease-1")
        claimed = q.claim_batch("bench-worker-dying", batch_size=1, lease_seconds=lease_seconds)
        assert [t.id for t in claimed] == [enq.task.id]

        before = q.recover_orphans()
        reclaimed_before = enq.task.id in before.reclaimed_ids

        time.sleep(lease_seconds + 0.5)

        after = q.recover_orphans()
        reclaimed_after = enq.task.id in after.reclaimed_ids

    return {
        "lease_seconds": lease_seconds,
        "reclaimed_before_expiry": reclaimed_before,
        "reclaimed_after_expiry": reclaimed_after,
        "pass": (not reclaimed_before) and reclaimed_after,
    }


def measure_idempotency(dsn: str) -> dict:
    """The same idempotency key enqueued twice must yield exactly one task."""
    from skmemory.taskqueue import TaskQueue

    with TaskQueue(dsn=dsn) as q:
        with q._connection().cursor() as cur:
            cur.execute("TRUNCATE task_queue RESTART IDENTITY;")
        first = q.enqueue("bench", {"n": 1}, idempotency_key="bench-idempotency-1")
        second = q.enqueue("bench", {"n": 1}, idempotency_key="bench-idempotency-1")
        with q._connection().cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM task_queue WHERE idempotency_key = %s;",
                ("bench-idempotency-1",),
            )
            (row_count,) = cur.fetchone()

    return {
        "first_enqueue_status": first.status,
        "second_enqueue_status": second.status,
        "same_task_id": first.task.id == second.task.id,
        "row_count_for_key": row_count,
        "pass": (
            first.status == "created"
            and second.status == "duplicate"
            and first.task.id == second.task.id
            and row_count == 1
        ),
    }


def run_benchmark(*, num_tasks: int = 500, num_workers: int = 8) -> dict:
    dsn = _setup_throwaway_db()
    try:
        return {
            "throwaway_db": THROWAWAY_DB_NAME,
            "concurrency": measure_concurrency(dsn, num_tasks=num_tasks, num_workers=num_workers),
            "lease_recovery": measure_lease_recovery(dsn),
            "idempotency": measure_idempotency(dsn),
        }
    finally:
        _teardown_throwaway_db()


def _print_report(res: dict) -> None:
    c = res["concurrency"]
    lr = res["lease_recovery"]
    idem = res["idempotency"]

    print(f"\nskmemory taskqueue bench  |  throwaway db {res['throwaway_db']!r}\n")

    print("concurrency safety (SELECT ... FOR UPDATE SKIP LOCKED)")
    print(f"  tasks enqueued:          {c['tasks_enqueued']}")
    print(f"  concurrent claimers:     {c['concurrent_workers']}")
    print(f"  total claims issued:     {c['total_claims']}")
    print(f"  unique tasks claimed:    {c['unique_tasks_claimed']}")
    print(f"  duplicates:              {c['duplicates']}")
    print(f"  lost (never claimed):    {c['lost']}")
    print(f"  per-worker claim counts: {c['per_worker_claim_counts']}")
    print(f"  elapsed:                 {c['elapsed_seconds']}s")
    print(f"  result:                  {'PASS' if c['pass'] else 'FAIL'}\n")

    print("lease recovery")
    print(f"  lease_seconds:            {lr['lease_seconds']}")
    print(f"  reclaimed before expiry:  {lr['reclaimed_before_expiry']} (expected False)")
    print(f"  reclaimed after expiry:   {lr['reclaimed_after_expiry']} (expected True)")
    print(f"  result:                   {'PASS' if lr['pass'] else 'FAIL'}\n")

    print("idempotency")
    print(f"  first enqueue status:    {idem['first_enqueue_status']} (expected created)")
    print(f"  second enqueue status:   {idem['second_enqueue_status']} (expected duplicate)")
    print(f"  same task id:            {idem['same_task_id']}")
    print(f"  rows for that key:       {idem['row_count_for_key']} (expected 1)")
    print(f"  result:                  {'PASS' if idem['pass'] else 'FAIL'}\n")

    overall = c["pass"] and lr["pass"] and idem["pass"]
    print(f"overall: {'PASS' if overall else 'FAIL'}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="skmemory durable task queue bench (R7)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument(
        "--tasks", type=int, default=500, help="tasks for the concurrency measurement"
    )
    parser.add_argument("--workers", type=int, default=8, help="concurrent claimer threads")
    args = parser.parse_args()

    probe_dsn = _dsn_with_db(_base_dsn(), "postgres")
    if not _pg_reachable(probe_dsn):
        print(
            "skmemory.eval.queue_bench: SKIPPED, no skmem-pg reachable at the "
            "configured host (SKMEMORY_PG_DSN). This harness needs a real "
            "database; it never runs offline.",
            file=sys.stderr,
        )
        return 0

    res = run_benchmark(num_tasks=args.tasks, num_workers=args.workers)
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        _print_report(res)

    overall = (
        res["concurrency"]["pass"] and res["lease_recovery"]["pass"] and res["idempotency"]["pass"]
    )
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
