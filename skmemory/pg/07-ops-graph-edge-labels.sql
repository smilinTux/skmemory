-- =====================================================================
-- 07-ops-graph-edge-labels.sql  --  an ops_brain label for every page edge type
-- =====================================================================
-- 05-ops-graph-grants.sql pre-created six edge labels. The skbrain page front
-- matter schema (skbrain page-frontmatter-v1) allows nine edge types, and the
-- projector uppercases the type to name the label. HOSTS, DOCUMENTS, ABOUT and
-- PROMOTED were missing, so the first canon whose pages used one (skrsi lessons
-- use `about`) failed every sync with "permission denied for schema ops_brain":
-- a least-privilege projector cannot create a label at MERGE time.
--
-- Create the missing labels as owner and re-grant the new tables to the ops
-- roles. Additive and idempotent. Live nodes: `skmemory pg migrate`.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL search_path = ag_catalog, public;

DO $$
DECLARE
  edge text;
BEGIN
  FOREACH edge IN ARRAY ARRAY['HOSTS', 'DOCUMENTS', 'ABOUT', 'PROMOTED']
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

REVOKE ALL ON ALL TABLES IN SCHEMA ops_brain FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops_brain TO skbrain_ops_rw;

COMMIT;
