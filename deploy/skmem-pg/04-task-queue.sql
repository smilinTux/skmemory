-- =====================================================================
-- 04-task-queue.sql  --  durable task queue for background memory maintenance (R7)
-- =====================================================================
-- WHAT THIS DOES: creates a single `task_queue` table in the EXISTING skmem-pg
-- instance (public schema, same DB as `memories`) so background memory
-- maintenance work (reconcile, promotion, consolidation) has somewhere durable
-- to enqueue work instead of each job inventing its own ad hoc coordination.
--
-- WHY: `skmem_reconcile.py` already had to scope its scratch table name by
-- agent AND pid (`memories_bf_<agent>_<pid>`, see skmemory/reconcile.py and
-- tests/test_reconcile_scratch_table.py) because two overlapping reconcile
-- runs raced on one shared table name. That is a workaround for the thing
-- this migration actually provides: a place for competing/overlapping workers
-- to claim work without colliding, and for a dead worker's claim to expire
-- instead of being stuck forever. See
-- docs/postmortems/2026-10-16-sqlite-only-orphans.md for the operational
-- history that motivates "nothing silent": every terminal transition on a
-- task below records why, the same shape as skmemory/fusion.py's ArmOutcome.
--
-- SHAPE:
--   * `status` is one of pending, claimed, failed, completed, dead.
--       - pending:   ready to claim, never failed.
--       - claimed:   a worker holds the lease (owner + lease_expires_at set).
--       - failed:    a previous attempt failed; waiting out its backoff before
--                    it becomes claimable again (available_at in the future).
--       - completed: terminal, success.
--       - dead:      terminal, exhausted max_attempts. last_error explains why.
--   * `idempotency_key` is UNIQUE, so enqueueing the same logical work twice
--     (same key) returns the existing row instead of creating a second task.
--   * Claiming is `SELECT ... FOR UPDATE SKIP LOCKED` (wrapped in a single
--     atomic UPDATE, see skmemory/taskqueue.py), so N concurrent workers never
--     collide and a slow worker never blocks the others.
--   * Orphan recovery (a claimed task whose lease expired) reuses the exact
--     same retry-with-backoff-or-dead transition as an explicit failure, so
--     a worker that died leaves its task reclaimable, not stuck forever.
--
-- ADDITIVE + IDEMPOTENT: nothing in `memories` or any existing table changes;
-- every statement is guarded (IF NOT EXISTS) so a re-run is a no-op.
--
-- APPLY: auto-applied on a FRESH compose boot via migrations.txt +
-- initdb/00-run-init.sh (no live data on first initdb). On a LIVE node it is
-- operator-initiated via `skmemory pg migrate` (pre-dump + apply + verify in
-- one guarded command). Do NOT auto-apply from CI.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL search_path = public;

CREATE TABLE IF NOT EXISTS task_queue (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'failed', 'completed', 'dead')),
    priority integer NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    owner text,
    lease_expires_at timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT task_queue_idempotency_key_key UNIQUE (idempotency_key)
);

COMMENT ON TABLE task_queue IS
    'Durable work queue for background memory maintenance (R7). See '
    'skmemory/taskqueue.py for the claim/complete/fail/recover_orphans API '
    'and the adoption note for reconcile/promotion.';

-- Claim query: only pending/failed rows whose backoff has elapsed are
-- claimable. A partial index keeps this cheap as completed/dead rows pile up.
CREATE INDEX IF NOT EXISTS task_queue_claimable_idx
    ON task_queue (priority DESC, available_at ASC)
    WHERE status IN ('pending', 'failed');

-- Orphan recovery query: only claimed rows have a lease to expire.
CREATE INDEX IF NOT EXISTS task_queue_lease_idx
    ON task_queue (lease_expires_at)
    WHERE status = 'claimed';

-- Operational filtering/inspection by task type.
CREATE INDEX IF NOT EXISTS task_queue_type_idx
    ON task_queue (task_type, status);

COMMIT;
