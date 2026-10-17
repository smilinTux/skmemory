"""Durable Postgres-backed task queue for background memory maintenance (R7).

Why this exists
----------------
Background memory maintenance (reconcile, promotion, consolidation) has no
durable work queue today, and the workaround is already visible in git
history: ``skmem_reconcile.py`` has to scope its scratch table by BOTH agent
and PID (``memories_bf_<agent>_<pid>``, see ``skmemory/reconcile.py`` and
``tests/test_reconcile_scratch_table.py``) because two overlapping reconcile
runs raced on one shared table name. A scratch table scoped by PID is a
coordination primitive improvised because there was nowhere durable to claim
work. See ``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` for the
broader operational cost of silent, unobserved background-job failure in this
house: the single most-repeated defect in that document is a failure that
looks identical to success. This queue is built so that cannot happen here --
every terminal transition on a task records why (see :class:`TaskResult`,
modelled on ``skmemory/fusion.py``'s ``ArmOutcome``: explicit statuses plus a
``detail`` string, never a bare truthy/falsy return).

Design, modelled on Hindsight's (vectorize-io) worker poller
--------------------------------------------------------------
* **Claim in batches with ``SELECT ... FOR UPDATE SKIP LOCKED``.** Wrapped in
  a single atomic ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED LIMIT
  n)`` statement (see :meth:`TaskQueue.claim_batch`), so N concurrent workers
  partition the claimable rows instead of colliding, and a worker that is slow
  to start its transaction never blocks the others (SKIP LOCKED, not blocking
  lock wait).
* **Leases.** A claimed task records ``owner`` (the claiming worker's
  identity) and ``lease_expires_at``. Nothing renews a lease automatically; a
  worker that wants more time must finish before its lease window, or this
  queue intentionally treats it as gone.
* **Orphan recovery.** :meth:`TaskQueue.recover_orphans` finds claimed tasks
  whose lease has expired and runs them through the exact same
  retry-with-backoff-or-dead transition as an explicit :meth:`TaskQueue.fail`.
  Call it on worker startup and on a periodic timer; a worker that dies mid
  task leaves it reclaimable, never stuck forever.
* **Retry with backoff, terminal dead state, failure reason recorded.** Every
  :meth:`TaskQueue.fail` call increments ``attempts`` and either reschedules
  the task (status ``failed``, ``available_at`` pushed out by
  :func:`compute_backoff_seconds`) or, once ``attempts >= max_attempts``,
  marks it ``dead`` -- a terminal state a human or dashboard can actually see,
  with ``last_error`` explaining the last failure. A task is never silently
  retried forever.
* **Idempotency key.** ``idempotency_key`` is UNIQUE. Enqueueing the same key
  twice returns the existing row (``EnqueueOutcome.status == "duplicate"``)
  rather than creating a second task. Callers that want this for free can
  derive a key from the task type and payload with
  :func:`derive_idempotency_key`; callers that want a logically-identical task
  to run again later (e.g. a daily job) should fold a time bucket into the key
  themselves (the key has no retention/expiry logic of its own -- this queue
  deliberately does not invent one).
* **Status enum.** ``pending`` (never failed, claimable now), ``claimed``
  (a worker holds the lease), ``failed`` (a previous attempt failed; waiting
  out backoff, claimable again once ``available_at`` passes -- a distinct,
  visible state from "fresh pending"), ``completed`` (terminal, success),
  ``dead`` (terminal, retries exhausted).

Adoption note for reconcile / promotion (NOT done in this change)
-------------------------------------------------------------------
This PR ships the queue only. Reconcile and promotion are left untouched
(another agent may be editing them concurrently); the path to adopt this is:

1. Replace the per-agent-per-PID scratch table dance in
   ``skmemory/reconcile.py`` with one enqueued task per reconcile run:
   ``enqueue("reconcile", {"agent": agent}, idempotency_key=f"reconcile:{agent}:{window}")``
   where ``window`` is whatever cadence key (e.g. a date or run id) makes two
   enqueues for the same agent in the same window collapse into one task
   instead of racing on a shared table name.
2. The reconcile cron entrypoint becomes a worker loop:
   ``claim_batch(owner=f"{hostname}:{pid}", task_type="reconcile")``, run the
   existing reconcile logic over the claimed task's payload, then
   ``complete(...)`` or ``fail(..., reason=str(exc))``. ``recover_orphans()``
   runs once at process start and on the same periodic cadence as the
   reconcile cron itself.
3. ``promotion.py`` follows the same shape with ``task_type="promotion"``.
4. Both can then drop their own PID-scoping entirely: the queue's unique
   ``idempotency_key`` plus ``SELECT ... FOR UPDATE SKIP LOCKED`` claiming is
   the thing that was being hand-rolled.

Measured
--------
See ``skmemory/eval/queue_bench.py`` for concurrency-safety, lease-recovery,
and idempotency numbers run against a real (throwaway) database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: Env var carrying the skmem-pg DSN. Never hardcode a DSN or put one in argv
#: or a log line (repo-wide convention, see skmemory/dsn_psql.py).
DSN_ENV_VAR = "SKMEMORY_PG_DSN"

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_CLAIM_BATCH_SIZE = 10
DEFAULT_ORPHAN_BATCH_SIZE = 100
DEFAULT_ORPHAN_REASON = "lease expired: owner presumed gone"

#: Backoff formula shared by the SQL in claim/fail/recover_orphans and the
#: pure Python helper below, so unit tests exercise the exact arithmetic the
#: database applies: ``min(cap, base ** attempt)``, attempt counted from 1.
BACKOFF_BASE = 2.0
BACKOFF_CAP_SECONDS = 300.0


class TaskStatus(str, Enum):
    """Where one task is in its lifecycle. See module docstring for meaning."""

    PENDING = "pending"
    CLAIMED = "claimed"
    FAILED = "failed"
    COMPLETED = "completed"
    DEAD = "dead"


#: Statuses a claim query may pick up (available_at permitting).
CLAIMABLE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.FAILED})
#: Statuses nothing can transition out of.
TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.DEAD})


# =============================================================================
# Pure logic: no DB, no I/O. Exercised directly by tests/test_taskqueue.py.
# =============================================================================


def compute_backoff_seconds(
    attempt: int, *, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP_SECONDS
) -> float:
    """Exponential backoff, capped: ``min(cap, base ** attempt)``.

    ``attempt`` is 1-indexed (the attempt count AFTER the failure being
    scheduled for retry, matching ``task_queue.attempts`` post-increment).
    The SQL in :meth:`TaskQueue.fail` and :meth:`TaskQueue.recover_orphans`
    computes the identical expression server-side so a worker crash between
    the Python call and the write cannot leave the two out of sync; this
    function exists so that formula has a standalone, DB-free unit test.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if base <= 1:
        raise ValueError(f"base must be > 1 for backoff to grow, got {base}")
    return min(cap, base**attempt)


def lease_deadline(now: datetime, lease_seconds: int) -> datetime:
    """When a lease claimed at ``now`` for ``lease_seconds`` expires."""
    if lease_seconds <= 0:
        raise ValueError(f"lease_seconds must be > 0, got {lease_seconds}")
    return now + timedelta(seconds=lease_seconds)


def is_lease_expired(lease_expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a claimed task's lease has expired as of ``now``.

    A task with no lease at all (``lease_expires_at is None``, i.e. not
    currently claimed) is never "expired" by this check; that is a different
    question (its status), not this one.
    """
    if lease_expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return lease_expires_at <= now


def next_status_after_failure(attempts_after: int, max_attempts: int) -> TaskStatus:
    """The status one failed attempt transitions to.

    ``attempts_after`` is the attempt count AFTER incrementing for this
    failure. Dead once attempts reach the cap; otherwise scheduled for retry
    (status ``failed``, not ``pending``, so a dashboard can tell "has never
    failed" apart from "failed once and is waiting out backoff").
    """
    return TaskStatus.DEAD if attempts_after >= max_attempts else TaskStatus.FAILED


def derive_idempotency_key(task_type: str, payload: dict[str, Any] | None = None) -> str:
    """Stable key for ``task_type`` + ``payload``, so identical logical work
    enqueued twice collapses into one task (see :meth:`TaskQueue.enqueue`).

    Canonicalized by sorting keys before hashing, so key order in the caller's
    dict never changes the derived key. Callers that need two enqueues of the
    SAME task_type/payload to be treated as different work (e.g. a recurring
    job) must pass an explicit ``idempotency_key`` instead of relying on this.
    """
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{task_type}:{canonical}".encode()).hexdigest()
    return f"{task_type}:{digest}"


# =============================================================================
# Result / outcome types (the ArmOutcome shape: explicit status + detail)
# =============================================================================

EnqueueStatus = Literal["created", "duplicate"]
ResultStatus = Literal["completed", "retry_scheduled", "dead", "not_found", "owner_mismatch"]


@dataclass(frozen=True)
class Task:
    """One row of ``task_queue``."""

    id: int
    task_type: str
    payload: dict[str, Any]
    idempotency_key: str
    status: TaskStatus
    priority: int
    attempts: int
    max_attempts: int
    owner: str | None
    lease_expires_at: datetime | None
    available_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Task:
        return cls(
            id=row["id"],
            task_type=row["task_type"],
            payload=row["payload"] or {},
            idempotency_key=row["idempotency_key"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            owner=row["owner"],
            lease_expires_at=row["lease_expires_at"],
            available_at=row["available_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )


@dataclass(frozen=True)
class EnqueueOutcome:
    """What :meth:`TaskQueue.enqueue` actually did."""

    task: Task
    status: EnqueueStatus
    detail: str = ""

    @property
    def created(self) -> bool:
        return self.status == "created"


@dataclass(frozen=True)
class TaskResult:
    """What :meth:`TaskQueue.complete` or :meth:`TaskQueue.fail` actually did.

    ``not_found`` and ``owner_mismatch`` are distinguished from a bare "no
    rows updated" so a caller can tell "this task never existed" apart from
    "this task exists but you no longer hold its lease" (the lease expired
    and someone else, or :meth:`TaskQueue.recover_orphans`, already reclaimed
    it). Nothing here is swallowed into a generic failure.
    """

    task_id: int
    status: ResultStatus
    detail: str = ""
    task: Task | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class OrphanRecoveryResult:
    """What one :meth:`TaskQueue.recover_orphans` pass actually did."""

    reclaimed_ids: tuple[int, ...] = ()
    dead_ids: tuple[int, ...] = ()
    detail: str = ""

    @property
    def reclaimed_count(self) -> int:
        return len(self.reclaimed_ids)

    @property
    def dead_count(self) -> int:
        return len(self.dead_ids)


# =============================================================================
# TaskQueue: the Postgres-backed API
# =============================================================================


class TaskQueue:
    """Durable queue over the ``task_queue`` table (migration
    ``deploy/skmem-pg/04-task-queue.sql``).

    One instance per worker/process is the intended usage; the connection is
    opened lazily on first use and reused (autocommit, matching the rest of
    this repo's pg backends, e.g. ``skmemory/backends/pgvector_backend.py``).
    Every method is its own atomic statement (a single ``UPDATE`` or a CTE
    feeding one), so autocommit is safe: there is no multi-statement
    transaction a caller could half-complete.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get(DSN_ENV_VAR, "")
        if not self.dsn:
            raise RuntimeError(f"{DSN_ENV_VAR} is required to use TaskQueue; never hardcode a DSN")
        self._conn = None

    def _connection(self):
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> TaskQueue:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # --- enqueue -------------------------------------------------------------

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> EnqueueOutcome:
        """Enqueue one task, or return the existing one for a duplicate key.

        ``idempotency_key`` defaults to :func:`derive_idempotency_key` over
        ``task_type``/``payload`` when the caller does not pass one.
        """
        from psycopg.rows import dict_row
        from psycopg.types.json import Json

        payload = payload or {}
        key = idempotency_key or derive_idempotency_key(task_type, payload)
        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO task_queue (task_type, payload, idempotency_key, priority, max_attempts)
                VALUES (%(task_type)s, %(payload)s, %(idempotency_key)s, %(priority)s, %(max_attempts)s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *;
                """,
                {
                    "task_type": task_type,
                    "payload": Json(payload),
                    "idempotency_key": key,
                    "priority": priority,
                    "max_attempts": max_attempts,
                },
            )
            row = cur.fetchone()
            if row is not None:
                return EnqueueOutcome(task=Task.from_row(row), status="created")

            cur.execute("SELECT * FROM task_queue WHERE idempotency_key = %s;", (key,))
            existing = cur.fetchone()
            if existing is None:
                # The INSERT hit the unique conflict (so a row with this key
                # exists) but the follow-up SELECT found nothing. That is a
                # real inconsistency, not an empty result to paper over.
                raise RuntimeError(
                    f"enqueue race on idempotency_key {key!r}: conflict reported "
                    "but no row found by the follow-up read"
                )
            return EnqueueOutcome(
                task=Task.from_row(existing),
                status="duplicate",
                detail=f"idempotency_key {key!r} already enqueued as task {existing['id']}",
            )

    # --- claim -----------------------------------------------------------------

    def claim_batch(
        self,
        owner: str,
        *,
        batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        task_type: str | None = None,
    ) -> list[Task]:
        """Claim up to ``batch_size`` claimable tasks for ``owner``.

        One atomic ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)``
        statement: concurrent callers partition the claimable rows (SKIP
        LOCKED means a row already being claimed by another caller is simply
        excluded, never waited on), so this is safe to call from any number
        of workers at once. Returns only the tasks THIS call claimed.
        """
        if batch_size <= 0:
            return []
        from psycopg.rows import dict_row

        conn = self._connection()
        params: dict[str, Any] = {
            "owner": owner,
            "lease_seconds": lease_seconds,
            "batch_size": batch_size,
        }
        type_filter = ""
        if task_type is not None:
            type_filter = "AND task_type = %(task_type)s"
            params["task_type"] = task_type

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                WITH claimable AS (
                    SELECT id FROM task_queue
                    WHERE status IN ('pending', 'failed')
                      AND available_at <= now()
                      {type_filter}
                    ORDER BY priority DESC, available_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %(batch_size)s
                )
                UPDATE task_queue t
                SET status = 'claimed',
                    owner = %(owner)s,
                    lease_expires_at = now() + (%(lease_seconds)s * interval '1 second'),
                    updated_at = now()
                FROM claimable
                WHERE t.id = claimable.id
                RETURNING t.*;
                """,
                params,
            )
            rows = cur.fetchall()
        return [Task.from_row(r) for r in rows]

    # --- complete / fail ---------------------------------------------------

    def complete(self, task_id: int, owner: str, *, detail: str = "") -> TaskResult:
        """Mark a claimed task completed. Only the current lease holder may."""
        from psycopg.rows import dict_row

        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE task_queue
                SET status = 'completed',
                    completed_at = now(),
                    updated_at = now(),
                    owner = NULL,
                    lease_expires_at = NULL
                WHERE id = %(id)s AND owner = %(owner)s AND status = 'claimed'
                RETURNING *;
                """,
                {"id": task_id, "owner": owner},
            )
            row = cur.fetchone()
            if row is not None:
                return TaskResult(
                    task_id=task_id, status="completed", task=Task.from_row(row), detail=detail
                )
            return self._diagnose_miss(cur, task_id, owner)

    def fail(
        self,
        task_id: int,
        owner: str,
        reason: str,
        *,
        base: float = BACKOFF_BASE,
        cap: float = BACKOFF_CAP_SECONDS,
    ) -> TaskResult:
        """Record a failed attempt: reschedule with backoff, or go dead.

        ``reason`` is required and stored in ``last_error`` -- a task that
        fails forever must end up visibly dead with a reason attached, never
        silently retried in a loop.
        """
        if not reason:
            raise ValueError("fail() requires a non-empty reason; nothing silent")
        from psycopg.rows import dict_row

        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE task_queue t
                SET attempts = t.attempts + 1,
                    status = CASE
                        WHEN t.attempts + 1 >= t.max_attempts THEN 'dead'
                        ELSE 'failed'
                    END,
                    last_error = %(reason)s,
                    owner = NULL,
                    lease_expires_at = NULL,
                    available_at = CASE
                        WHEN t.attempts + 1 >= t.max_attempts THEN t.available_at
                        ELSE now() + (LEAST(%(cap)s, power(%(base)s, t.attempts + 1)) * interval '1 second')
                    END,
                    updated_at = now()
                WHERE t.id = %(id)s AND t.owner = %(owner)s AND t.status = 'claimed'
                RETURNING t.*;
                """,
                {"id": task_id, "owner": owner, "reason": reason, "base": base, "cap": cap},
            )
            row = cur.fetchone()
            if row is None:
                return self._diagnose_miss(cur, task_id, owner)
            task = Task.from_row(row)
            status: ResultStatus = "dead" if task.status == TaskStatus.DEAD else "retry_scheduled"
            return TaskResult(task_id=task_id, status=status, task=task, detail=reason)

    def _diagnose_miss(self, cur: Any, task_id: int, owner: str) -> TaskResult:
        """Explain why a complete/fail UPDATE matched zero rows.

        Reads the row again (outside the failed UPDATE's WHERE) to tell
        "never existed" apart from "exists, but you don't hold it any more"
        instead of returning one undifferentiated failure.
        """
        cur.execute("SELECT * FROM task_queue WHERE id = %s;", (task_id,))
        row = cur.fetchone()
        if row is None:
            return TaskResult(
                task_id=task_id, status="not_found", detail=f"task {task_id} does not exist"
            )
        task = Task.from_row(row)
        return TaskResult(
            task_id=task_id,
            status="owner_mismatch",
            task=task,
            detail=(
                f"task {task_id} is status={task.status.value} owner={task.owner!r}; "
                f"caller owner={owner!r} no longer holds it (lease likely reclaimed)"
            ),
        )

    # --- orphan recovery -----------------------------------------------------

    def recover_orphans(
        self,
        *,
        batch_size: int = DEFAULT_ORPHAN_BATCH_SIZE,
        reason: str = DEFAULT_ORPHAN_REASON,
        base: float = BACKOFF_BASE,
        cap: float = BACKOFF_CAP_SECONDS,
    ) -> OrphanRecoveryResult:
        """Reclaim claimed tasks whose lease has expired.

        Runs the same retry-with-backoff-or-dead transition as
        :meth:`fail`, through one atomic ``UPDATE ... FROM (SELECT ... FOR
        UPDATE SKIP LOCKED)`` statement so a periodic recovery pass can run
        concurrently with itself (e.g. on more than one node) without double
        counting an attempt. Call this on worker startup and on a periodic
        timer.
        """
        from psycopg.rows import dict_row

        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH orphans AS (
                    SELECT id FROM task_queue
                    WHERE status = 'claimed' AND lease_expires_at < now()
                    ORDER BY lease_expires_at ASC
                    LIMIT %(batch_size)s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE task_queue t
                SET attempts = t.attempts + 1,
                    status = CASE
                        WHEN t.attempts + 1 >= t.max_attempts THEN 'dead'
                        ELSE 'failed'
                    END,
                    last_error = %(reason)s,
                    owner = NULL,
                    lease_expires_at = NULL,
                    available_at = CASE
                        WHEN t.attempts + 1 >= t.max_attempts THEN t.available_at
                        ELSE now() + (LEAST(%(cap)s, power(%(base)s, t.attempts + 1)) * interval '1 second')
                    END,
                    updated_at = now()
                FROM orphans
                WHERE t.id = orphans.id
                RETURNING t.*;
                """,
                {"batch_size": batch_size, "reason": reason, "base": base, "cap": cap},
            )
            rows = cur.fetchall()
        reclaimed = tuple(r["id"] for r in rows if r["status"] == "failed")
        dead = tuple(r["id"] for r in rows if r["status"] == "dead")
        return OrphanRecoveryResult(
            reclaimed_ids=reclaimed,
            dead_ids=dead,
            detail=f"recovered {len(rows)} orphaned task(s): {len(reclaimed)} rescheduled, {len(dead)} dead",
        )

    # --- inspection ------------------------------------------------------------

    def get(self, task_id: int) -> Task | None:
        """Fetch one task by id, or ``None`` if it does not exist."""
        from psycopg.rows import dict_row

        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM task_queue WHERE id = %s;", (task_id,))
            row = cur.fetchone()
        return Task.from_row(row) if row else None

    def count_by_status(self, task_type: str | None = None) -> dict[str, int]:
        """Task counts grouped by status, for health checks and the bench."""
        from psycopg.rows import dict_row

        conn = self._connection()
        with conn.cursor(row_factory=dict_row) as cur:
            if task_type is not None:
                cur.execute(
                    "SELECT status, count(*) AS n FROM task_queue WHERE task_type = %s GROUP BY status;",
                    (task_type,),
                )
            else:
                cur.execute("SELECT status, count(*) AS n FROM task_queue GROUP BY status;")
            rows = cur.fetchall()
        return {r["status"]: r["n"] for r in rows}
