-- Post-apply checks for 05-ops-graph-grants.sql. Expect labels=9 (2 base, 7 here), then true, true, false.
SELECT 'labels', count(*)::text
  FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
  WHERE g.name = 'ops_brain'
UNION ALL SELECT 'rw_can_use_ag_catalog', has_schema_privilege('skbrain_ops_rw', 'ag_catalog', 'USAGE')::text
UNION ALL SELECT 'rw_can_insert_entity', has_table_privilege('skbrain_ops_rw', 'ops_brain."OpsEntity"', 'INSERT')::text
UNION ALL SELECT 'public_can_read_entity', has_table_privilege('public', 'ops_brain."OpsEntity"', 'SELECT')::text;
