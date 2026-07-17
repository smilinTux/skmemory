# skmem-pg image build (vendored from ~/skmem-build, 2026-07-17)

This directory makes the production skmem-pg image reproducible from the repo.
Previously it was buildable only from `~/skmem-build/` on .158, which was not a
git repo (DR gap, coord card a4b414c9).

## Contents

| File | Purpose |
| --- | --- |
| `Dockerfile` | `pgvector/pgvector:pg17` + ParadeDB `pg_search` 0.24.0 (real BM25) + Apache AGE PG17/v1.7.0-rc0 (built from source). Produces `skmem-pg:pg17-bm25-age`. |
| `schema.sql` | `pg_dump --schema-only --no-owner --no-privileges` of the LIVE .158 `skmemory` DB, taken 2026-07-17 (post mxbai cutover, includes the 2026-07-06 ParadeDB query-shape fix). Contains `hybrid_search_docs`, `hybrid_search_memories`, all HNSW + BM25 index DDL. Loaded automatically on first `docker compose up`. |
| `02-enable-bm25-age.sql` | Historical migration: first enable of pg_search/AGE + original hybrid functions. Kept for provenance; superseded by `schema.sql` for fresh installs. |
| `03-cutover-mxbai.sql` | Historical migration: bge to mxbai embedding-column cutover (bge kept as `emb_bge_legal`). Superseded by `schema.sql` for fresh installs. |
| `skmem_reconcile.py` | Daily-cron reconciler that rebuilds the `memories` derived cache from the synced flat JSON source of truth. |

## Build and run

From the repo root:

```sh
export SKMEM_PG_PASSWORD=change-me   # never committed; compose refuses to start without it
docker compose up -d --build
docker exec skmem-pg psql -U postgres -d skmemory -c "SELECT extname FROM pg_extension;"
```

Server must run with `shared_preload_libraries=pg_search,age` (the compose file
sets this via `command:`).

## Vendoring notes and caveats

- `schema.sql` was edited in exactly one way after dumping: the
  `CREATE SCHEMA ag_catalog;` and `CREATE SCHEMA paradedb;` lines were removed,
  because `CREATE EXTENSION age / pg_search` creates those schemas itself and a
  pre-existing schema aborts extension creation on a fresh initdb.
- AGE graph label tables (`lumina_knowledge`, `opus_knowledge`,
  `personal_history` schemas) are recreated as plain tables by this dump, but a
  schema-only dump does NOT restore the `ag_catalog.ag_graph` /
  `ag_label` registry rows (extension config data). To fully restore graphs on
  a fresh node, either restore a data dump or re-register the graphs with
  `ag_catalog.create_graph()` and re-import from source.
- No credentials are committed anywhere in this directory; the password is
  env-parametrized (`SKMEM_PG_PASSWORD`) with no default.
- To refresh the snapshot after schema changes on the live DB:
  `docker exec skmem-pg pg_dump --schema-only --no-owner --no-privileges -U postgres skmemory > deploy/skmem-pg/schema.sql`
  then re-apply the two CREATE SCHEMA deletions above.

## Related

- `~/.skcapstone/docs/MEMORY_STORES.md` (store map)
- `docs/deploy-plan/skmemory-bulletproof-deploy.md` (deploy plan)
- skingest is the sole ingestion home and talks to this DB; its SOP points here.
