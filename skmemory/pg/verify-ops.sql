-- =====================================================================
-- verify-ops.sql  --  post-apply verification for 03-ops-namespace.sql
-- =====================================================================
-- Extracted from README.md step 3 (card OPS1.3) so the exact same query is
-- run by the operator, the `skmemory pg migrate` runner (--verify), and the
-- compose first-boot init wrapper. Read-only: SELECTs only, changes nothing.
--
-- Expected on a correctly-applied ops namespace:
--   schema=1, tables=3, hnsw=1, bm25=1, fn=1, graph=1, public_can_use_ops=f
-- =====================================================================

SELECT 'schema'   AS obj, count(*)::text AS n
  FROM information_schema.schemata WHERE schema_name = 'ops'
UNION ALL SELECT 'tables', count(*)::text
  FROM information_schema.tables  WHERE table_schema = 'ops'
UNION ALL SELECT 'hnsw',   count(*)::text
  FROM pg_indexes WHERE schemaname = 'ops' AND indexname = 'ops_chunks_hnsw'
UNION ALL SELECT 'bm25',   count(*)::text
  FROM pg_indexes WHERE schemaname = 'ops' AND indexname = 'ops_chunks_bm25'
UNION ALL SELECT 'fn',     count(*)::text
  FROM pg_proc WHERE proname = 'hybrid_search_ops'
UNION ALL SELECT 'graph',  count(*)::text
  FROM ag_catalog.ag_graph WHERE name = 'ops_brain'
UNION ALL SELECT 'public_can_use_ops',
  has_schema_privilege('public', 'ops', 'USAGE')::text;
