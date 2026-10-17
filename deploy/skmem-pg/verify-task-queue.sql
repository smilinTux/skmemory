-- =====================================================================
-- verify-task-queue.sql  --  post-apply verification for 04-task-queue.sql
-- =====================================================================
-- Run by the operator, by `skmemory pg migrate --verify`, and by the compose
-- first-boot init wrapper. Read-only: SELECTs only, changes nothing.
--
-- Expected on a correctly-applied task queue:
--   table=1, claimable_idx=1, lease_idx=1, type_idx=1, idempotency_unique=1
-- =====================================================================

SELECT 'table' AS obj, count(*)::text AS n
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_name = 'task_queue'
UNION ALL SELECT 'claimable_idx', count(*)::text
  FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'task_queue_claimable_idx'
UNION ALL SELECT 'lease_idx', count(*)::text
  FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'task_queue_lease_idx'
UNION ALL SELECT 'type_idx', count(*)::text
  FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'task_queue_type_idx'
UNION ALL SELECT 'idempotency_unique', count(*)::text
  FROM pg_constraint WHERE conname = 'task_queue_idempotency_key_key';
