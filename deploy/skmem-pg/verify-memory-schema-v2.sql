-- Read-only verify for 06-memory-schema-v2.sql. Expected: columns=8, supersedes_idx=1
SELECT 'columns' AS obj, count(*)::text AS n
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'memories'
    AND column_name IN ('kind','outcome','event_at','valid_from','valid_to',
                        'supersedes_id','source_tier','schema_version')
UNION ALL SELECT 'supersedes_idx', count(*)::text
  FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'memories_supersedes_id';
