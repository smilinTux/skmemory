"""Tests for the durable task queue (R7, skmemory/taskqueue.py).

Structural/pure-logic tests (always run in CI, no database): the migration is
listed in migrations.txt, the migration's SQL actually defines the claimed
shape (parsed-SQL assertions, mirroring tests/test_ops_namespace_migration.py
and tests/test_pg_migrate.py), and the backoff/lease/idempotency-key/status
logic is pure Python exercised directly.

Live tests (module-scoped, skip cleanly when no database is reachable): two
tests exercise the real claim/complete/fail/recover_orphans lifecycle against
a THROWAWAY database (``skmemory_queuetest``), created and dropped by this
file's own fixture. They never touch the real `skmemory` database: the
fixture asserts the dbname it is about to DROP/CREATE is the throwaway name
before doing anything destructive, matching the hard rule that real agent
data must never be touched by this work. The broader concurrency-safety
measurement (N claimers over M tasks, proving exactly-once claim) lives in
skmemory/eval/queue_bench.py, not here.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skmemory.taskqueue import (
    BACKOFF_BASE,
    BACKOFF_CAP_SECONDS,
    DSN_ENV_VAR,
    EnqueueOutcome,
    Task,
    TaskQueue,
    TaskResult,
    TaskStatus,
    compute_backoff_seconds,
    derive_idempotency_key,
    is_lease_expired,
    lease_deadline,
    next_status_after_failure,
)

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy" / "skmem-pg"
MIGRATION = DEPLOY_DIR / "04-task-queue.sql"
MANIFEST = DEPLOY_DIR / "migrations.txt"
VERIFY = DEPLOY_DIR / "verify-task-queue.sql"

THROWAWAY_DB_NAME = "skmemory_queuetest"


def _sql() -> str:
    assert MIGRATION.exists(), f"migration file missing: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


# =============================================================================
# Structural: manifest wiring (DB-independent)
# =============================================================================


def test_migration_listed_in_manifest_with_verify_script():
    raw = MANIFEST.read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(line.startswith("04-task-queue.sql") for line in lines), (
        "04-task-queue.sql must be listed in migrations.txt so it auto-applies"
    )
    matching = next(line for line in lines if line.startswith("04-task-queue.sql"))
    assert "verify-task-queue.sql" in matching


def test_migration_and_verify_files_exist_on_disk():
    assert MIGRATION.exists()
    assert VERIFY.exists()


def test_historical_migrations_still_excluded():
    """Guard against accidentally widening the manifest beyond this PR's scope."""
    names = [
        line.split("|")[0].strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "02-enable-bm25-age.sql" not in names
    assert "03-cutover-mxbai.sql" not in names


# =============================================================================
# Structural: parsed-SQL assertions on the migration itself (DB-independent)
# =============================================================================


def test_creates_task_queue_table_idempotently():
    assert re.search(r"CREATE TABLE IF NOT EXISTS task_queue\b", _sql())


def test_status_check_constraint_covers_required_statuses():
    sql = _sql()
    m = re.search(r"status\s+text[^,]*CHECK\s*\(status IN \(([^)]+)\)\)", sql, re.IGNORECASE)
    assert m, "status column must carry a CHECK (status IN (...)) constraint"
    values = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert values == {"pending", "claimed", "failed", "completed", "dead"}


def test_idempotency_key_is_unique():
    sql = _sql()
    assert re.search(r"UNIQUE\s*\(idempotency_key\)", sql, re.IGNORECASE), (
        "idempotency_key must carry a UNIQUE constraint so duplicate enqueues collapse"
    )


def test_claim_index_is_partial_on_claimable_statuses():
    sql = _sql()
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS task_queue_claimable_idx\s+ON task_queue[^;]*"
        r"WHERE status IN \('pending', 'failed'\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    ), "claim index must be partial on the two claimable statuses"


def test_lease_index_is_partial_on_claimed():
    sql = _sql()
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS task_queue_lease_idx\s+ON task_queue[^;]*"
        r"WHERE status = 'claimed'",
        sql,
        re.IGNORECASE | re.DOTALL,
    ), "lease (orphan-recovery) index must be partial on claimed rows only"


def test_type_index_present():
    sql = _sql()
    assert re.search(r"CREATE INDEX IF NOT EXISTS task_queue_type_idx\s+ON task_queue", sql)


def test_migration_is_additive_and_transactional():
    sql = _sql()
    assert "\\set ON_ERROR_STOP on" in sql
    assert re.search(r"^BEGIN;", sql, re.MULTILINE)
    assert re.search(r"^COMMIT;", sql, re.MULTILINE)
    # additive only: no DDL against any pre-existing table.
    assert not re.search(r"ALTER TABLE\s+(?!task_queue)\w", sql, re.IGNORECASE)
    assert not re.search(r"DROP TABLE", sql, re.IGNORECASE)


def test_verify_script_checks_the_claimed_objects():
    text = VERIFY.read_text(encoding="utf-8")
    assert "task_queue_claimable_idx" in text
    assert "task_queue_lease_idx" in text
    assert "task_queue_idempotency_key_key" in text


# =============================================================================
# Pure logic: backoff schedule
# =============================================================================


def test_backoff_grows_exponentially_before_the_cap():
    got = [compute_backoff_seconds(a) for a in (1, 2, 3, 4, 5)]
    assert got == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_is_capped():
    # 2 ** 9 == 512, above the 300s cap.
    assert compute_backoff_seconds(9) == BACKOFF_CAP_SECONDS
    assert compute_backoff_seconds(30) == BACKOFF_CAP_SECONDS


def test_backoff_honours_custom_base_and_cap():
    assert compute_backoff_seconds(3, base=3.0, cap=100.0) == 27.0
    assert compute_backoff_seconds(10, base=3.0, cap=100.0) == 100.0


def test_backoff_rejects_nonpositive_attempt():
    with pytest.raises(ValueError):
        compute_backoff_seconds(0)
    with pytest.raises(ValueError):
        compute_backoff_seconds(-1)


def test_backoff_rejects_base_too_small_to_grow():
    with pytest.raises(ValueError):
        compute_backoff_seconds(1, base=1.0)


def test_default_backoff_constants_match_module_defaults():
    assert compute_backoff_seconds(1) == min(BACKOFF_CAP_SECONDS, BACKOFF_BASE**1)


# =============================================================================
# Pure logic: lease arithmetic
# =============================================================================


def test_lease_deadline_adds_seconds():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert lease_deadline(now, 300) == now + timedelta(seconds=300)


def test_lease_deadline_rejects_nonpositive_seconds():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        lease_deadline(now, 0)
    with pytest.raises(ValueError):
        lease_deadline(now, -5)


def test_is_lease_expired_none_means_not_claimed_not_expired():
    assert is_lease_expired(None) is False


def test_is_lease_expired_true_in_the_past():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    past = now - timedelta(seconds=1)
    assert is_lease_expired(past, now=now) is True


def test_is_lease_expired_false_in_the_future():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = now + timedelta(seconds=1)
    assert is_lease_expired(future, now=now) is False


def test_is_lease_expired_boundary_is_expired():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert is_lease_expired(now, now=now) is True


# =============================================================================
# Pure logic: status transitions
# =============================================================================


def test_next_status_after_failure_retries_below_max():
    assert next_status_after_failure(1, max_attempts=5) == TaskStatus.FAILED
    assert next_status_after_failure(4, max_attempts=5) == TaskStatus.FAILED


def test_next_status_after_failure_dead_at_or_past_max():
    assert next_status_after_failure(5, max_attempts=5) == TaskStatus.DEAD
    assert next_status_after_failure(9, max_attempts=5) == TaskStatus.DEAD


# =============================================================================
# Pure logic: idempotency key derivation
# =============================================================================


def test_idempotency_key_stable_for_same_logical_work():
    k1 = derive_idempotency_key("reconcile", {"agent": "lumina", "day": "2026-10-17"})
    k2 = derive_idempotency_key("reconcile", {"day": "2026-10-17", "agent": "lumina"})
    assert k1 == k2, "key order in the payload dict must not change the derived key"


def test_idempotency_key_differs_for_different_payload():
    k1 = derive_idempotency_key("reconcile", {"agent": "lumina"})
    k2 = derive_idempotency_key("reconcile", {"agent": "jarvis"})
    assert k1 != k2


def test_idempotency_key_differs_for_different_task_type():
    k1 = derive_idempotency_key("reconcile", {"agent": "lumina"})
    k2 = derive_idempotency_key("promotion", {"agent": "lumina"})
    assert k1 != k2


def test_idempotency_key_is_prefixed_with_task_type():
    key = derive_idempotency_key("reconcile", {"agent": "lumina"})
    assert key.startswith("reconcile:")


# =============================================================================
# Dataclasses: row round trip and outcome shapes
# =============================================================================


def _fake_row(**overrides) -> dict:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "id": 1,
        "task_type": "demo",
        "payload": {"x": 1},
        "idempotency_key": "demo:abc",
        "status": "pending",
        "priority": 0,
        "attempts": 0,
        "max_attempts": 5,
        "owner": None,
        "lease_expires_at": None,
        "available_at": now,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def test_task_from_row_round_trip():
    task = Task.from_row(_fake_row())
    assert task.id == 1
    assert task.status == TaskStatus.PENDING
    assert task.payload == {"x": 1}


def test_task_from_row_coerces_status_string_to_enum():
    task = Task.from_row(_fake_row(status="dead"))
    assert task.status == TaskStatus.DEAD


def test_task_from_row_handles_null_payload_as_empty_dict():
    task = Task.from_row(_fake_row(payload=None))
    assert task.payload == {}


def test_enqueue_outcome_created_property():
    task = Task.from_row(_fake_row())
    assert EnqueueOutcome(task=task, status="created").created is True
    assert EnqueueOutcome(task=task, status="duplicate").created is False


def test_task_result_ok_property():
    assert TaskResult(task_id=1, status="completed").ok is True
    assert TaskResult(task_id=1, status="dead").ok is False
    assert TaskResult(task_id=1, status="not_found").ok is False


# =============================================================================
# TaskQueue construction (DB-independent: no connection attempted)
# =============================================================================


def test_taskqueue_requires_a_dsn(monkeypatch):
    monkeypatch.delenv(DSN_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        TaskQueue()


def test_taskqueue_accepts_explicit_dsn(monkeypatch):
    monkeypatch.delenv(DSN_ENV_VAR, raising=False)
    q = TaskQueue(dsn="postgresql://u:p@h/db")
    assert q.dsn == "postgresql://u:p@h/db"


def test_taskqueue_reads_dsn_from_env(monkeypatch):
    monkeypatch.setenv(DSN_ENV_VAR, "postgresql://u:p@h/envdb")
    q = TaskQueue()
    assert q.dsn == "postgresql://u:p@h/envdb"


def test_fail_requires_a_nonempty_reason(monkeypatch):
    monkeypatch.setenv(DSN_ENV_VAR, "postgresql://u:p@h/envdb")
    q = TaskQueue()
    with pytest.raises(ValueError):
        q.fail(1, "owner", "")


# =============================================================================
# Live tests: real database, throwaway DB only. Skip cleanly when unreachable.
# =============================================================================


def _base_dsn() -> str:
    return os.environ.get(DSN_ENV_VAR, "postgresql://postgres:skmemory@localhost:5432/skmemory")


def _dsn_with_db(dsn: str, dbname: str) -> str:
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def _pg_reachable() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(_dsn_with_db(_base_dsn(), "postgres"), connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


_LIVE_SKIP = not _pg_reachable()
_LIVE_REASON = "skmem-pg is unreachable at SKMEMORY_PG_DSN's host; skipping live taskqueue tests."
live = pytest.mark.skipif(_LIVE_SKIP, reason=_LIVE_REASON)


@pytest.fixture(scope="module")
def queuetest_dsn():
    """Create a THROWAWAY database, apply the migration, yield its DSN, drop it.

    Refuses to touch anything whose name is not the throwaway name, and never
    opens a connection to the real `skmemory` database for anything other
    than the one-time reachability probe above.
    """
    import psycopg

    admin_dsn = _dsn_with_db(_base_dsn(), "postgres")
    throwaway_dsn = _dsn_with_db(_base_dsn(), THROWAWAY_DB_NAME)
    assert psycopg.conninfo.conninfo_to_dict(throwaway_dsn)["dbname"] == THROWAWAY_DB_NAME, (
        "refusing to run live taskqueue tests against a non-throwaway database"
    )

    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {THROWAWAY_DB_NAME};")
            cur.execute(f"CREATE DATABASE {THROWAWAY_DB_NAME};")
    finally:
        conn.close()

    migration_sql = "\n".join(
        line for line in _sql().splitlines() if not line.strip().startswith("\\")
    )
    conn = psycopg.connect(throwaway_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(migration_sql)
    finally:
        conn.close()

    yield throwaway_dsn

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


@pytest.fixture()
def queue(queuetest_dsn):
    q = TaskQueue(dsn=queuetest_dsn)
    with q._connection().cursor() as cur:
        cur.execute("TRUNCATE task_queue RESTART IDENTITY;")
    yield q
    q.close()


@live
def test_live_claim_complete_fail_lifecycle(queue):
    # Idempotency: same key enqueued twice collapses into one task.
    created = queue.enqueue("demo", {"n": 1}, idempotency_key="lifecycle-k1")
    duplicate = queue.enqueue("demo", {"n": 1}, idempotency_key="lifecycle-k1")
    assert created.status == "created"
    assert duplicate.status == "duplicate"
    assert duplicate.task.id == created.task.id

    # Claim: exactly one task comes back, status flips to claimed.
    claimed = queue.claim_batch("worker-a", batch_size=5)
    assert [t.id for t in claimed] == [created.task.id]
    assert claimed[0].status == TaskStatus.CLAIMED
    assert claimed[0].owner == "worker-a"

    # A second claim call finds nothing claimable: the task is already held.
    assert queue.claim_batch("worker-b", batch_size=5) == []

    # Complete by the wrong owner is refused and explained, not silently a no-op.
    wrong_owner = queue.complete(claimed[0].id, "worker-b")
    assert wrong_owner.status == "owner_mismatch"
    assert str(claimed[0].id) in wrong_owner.detail or "worker-b" in wrong_owner.detail

    # Complete by the real owner succeeds.
    done = queue.complete(claimed[0].id, "worker-a")
    assert done.status == "completed"
    assert queue.get(claimed[0].id).status == TaskStatus.COMPLETED

    # A fresh task that fails repeatedly ends up dead with a reason, never
    # retried forever (max_attempts=2 here to keep the test fast).
    enq2 = queue.enqueue("demo", {"n": 2}, idempotency_key="lifecycle-k2", max_attempts=2)
    claimed2 = queue.claim_batch("worker-a", batch_size=5)
    assert [t.id for t in claimed2] == [enq2.task.id]

    first_fail = queue.fail(enq2.task.id, "worker-a", "boom 1")
    assert first_fail.status == "retry_scheduled"
    assert first_fail.task.status == TaskStatus.FAILED
    assert first_fail.task.attempts == 1
    assert first_fail.task.last_error == "boom 1"

    # Not claimable yet (backoff still running); recover_orphans is irrelevant
    # here since the task was explicitly failed, not abandoned mid-lease.
    assert queue.claim_batch("worker-a", batch_size=5, task_type="demo") == []

    # Push the backoff into the past directly, then claim and fail again to
    # exhaust max_attempts=2 and land on the terminal dead state.
    with queue._connection().cursor() as cur:
        cur.execute(
            "UPDATE task_queue SET available_at = now() - interval '1 second' WHERE id = %s;",
            (enq2.task.id,),
        )
    reclaimed = queue.claim_batch("worker-a", batch_size=5, task_type="demo")
    assert [t.id for t in reclaimed] == [enq2.task.id]
    second_fail = queue.fail(enq2.task.id, "worker-a", "boom 2")
    assert second_fail.status == "dead"
    assert second_fail.task.status == TaskStatus.DEAD
    assert second_fail.task.attempts == 2
    assert second_fail.task.last_error == "boom 2"

    # Dead is terminal: no further claim ever picks it up.
    assert queue.claim_batch("worker-a", batch_size=5, task_type="demo") == []


@live
def test_live_orphan_recovery_lease_expiry(queue):
    enq = queue.enqueue("orphan-demo", {}, idempotency_key="lease-k1")
    claimed = queue.claim_batch("worker-dying", batch_size=5, lease_seconds=1)
    assert [t.id for t in claimed] == [enq.task.id]

    # Before the lease expires: not an orphan, nothing reclaimed.
    before = queue.recover_orphans()
    assert enq.task.id not in before.reclaimed_ids
    assert queue.get(enq.task.id).status == TaskStatus.CLAIMED

    time.sleep(1.5)

    # After the lease expires: reclaimed, attempt recorded, reason recorded.
    # It is scheduled for retry (not stuck claimed forever: the whole point),
    # waiting out the same backoff an explicit fail() would apply.
    after = queue.recover_orphans()
    assert enq.task.id in after.reclaimed_ids
    recovered = queue.get(enq.task.id)
    assert recovered.status == TaskStatus.FAILED
    assert recovered.attempts == 1
    assert recovered.owner is None
    assert recovered.lease_expires_at is None
    assert recovered.last_error
    assert "lease" in recovered.last_error.lower()
