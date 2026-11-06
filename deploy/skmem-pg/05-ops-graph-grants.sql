-- =====================================================================
-- 05-ops-graph-grants.sql  --  let the skbrain projector write its graph
-- =====================================================================
-- 03-ops-namespace.sql created the ops_brain graph with no labels and never
-- granted ag_catalog, so every graph write by skbrain_projector failed with
-- "permission denied for schema ag_catalog".
--
-- AGE requires owning the parent label table to create a label, so a
-- least-privilege role cannot create one at MERGE time. Create the labels here
-- as owner. An edge type outside this list fails its sync loudly; add its label
-- here rather than widening the projector's rights.
--
-- Additive and idempotent. Live nodes: operator-initiated, via `skmemory pg migrate`.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL search_path = ag_catalog, public;

DO $$
DECLARE
  edge text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM ag_catalog.ag_label l
    JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
    WHERE g.name = 'ops_brain' AND l.name = 'OpsEntity'
  ) THEN
    PERFORM ag_catalog.create_vlabel('ops_brain', 'OpsEntity');
  END IF;
  FOREACH edge IN ARRAY ARRAY['LINKS_TO', 'TOUCHES', 'CONNECTS_TO', 'REMEDIATES', 'RUNS_ON', 'DEPENDS_ON']
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM ag_catalog.ag_label l
      JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
      WHERE g.name = 'ops_brain' AND l.name = edge
    ) THEN
      PERFORM ag_catalog.create_elabel('ops_brain', edge::cstring);
    END IF;
  END LOOP;
END $$;

GRANT USAGE ON SCHEMA ag_catalog TO skbrain_ops_rw;
REVOKE ALL ON ALL TABLES IN SCHEMA ops_brain FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops_brain TO skbrain_ops_rw;

COMMIT;
