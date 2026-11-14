-- Post-apply checks for 07-ops-graph-edge-labels.sql. Expect labels=13 (2 base, 7 from 05, 4 here), then true, false.
SELECT 'labels', count(*)::text
  FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
  WHERE g.name = 'ops_brain'
UNION ALL SELECT 'rw_can_insert_about', has_table_privilege('skbrain_ops_rw', 'ops_brain."ABOUT"', 'INSERT')::text
UNION ALL SELECT 'public_can_read_about', has_table_privilege('public', 'ops_brain."ABOUT"', 'SELECT')::text;
