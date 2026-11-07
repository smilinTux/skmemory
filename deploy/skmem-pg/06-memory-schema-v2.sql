-- =====================================================================
-- 06-memory-schema-v2.sql  --  typed columns for Memory schema v2 (SKMEM-P3-01)
-- =====================================================================
-- Adds kind, outcome, event_at, valid_from, valid_to, supersedes_id,
-- source_tier and schema_version to public.memories. memory_json already
-- carries every field, so these columns are a query convenience; readers
-- keep using memory_json. Rows written before this migration have NULL in
-- all eight columns (NULL schema_version means a v1 row). No backfill and no
-- table rewrite: every column is nullable with no default.
--
-- ADDITIVE + IDEMPOTENT: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
-- APPLY: fresh compose boot via migrations.txt; live nodes via
-- `skmemory pg migrate`. Not applied from CI.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL search_path = public;

ALTER TABLE memories ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS event_at timestamptz;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_from timestamptz;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_to timestamptz;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS supersedes_id text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_tier text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS schema_version integer;

CREATE INDEX IF NOT EXISTS memories_supersedes_id ON memories (supersedes_id)
    WHERE supersedes_id IS NOT NULL;

COMMIT;
