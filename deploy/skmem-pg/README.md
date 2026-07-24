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
| `skmem_reconcile.py` | Daily-cron reconciler that rebuilds the `memories` derived cache from the synced flat JSON source of truth. Prune step is guardrailed against cold-boot wipes (see below). |

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

## Reconcile prune guardrail + cold-boot ordering (card 6b8b3ced)

`skmem_reconcile.py` step 2 prunes pg rows whose flat file is gone. On a freshly
wiped or mid-Syncthing-sync machine the flat store can be empty or nearly empty
*before* it is restored; without a guard the reconcile then deletes every pg row
for that agent and reports success. Two independent, defense-in-depth guards:

**1. In-script guardrail (always on).** `prune_guard()` refuses a destructive
prune when, absent an explicit force:

- the flat source count is below `SKMEMORY_RECONCILE_PRUNE_FLOOR` (default `1`,
  so an empty flat store never prunes), or
- the prune would remove more than `SKMEMORY_RECONCILE_MAX_PRUNE_FRACTION` of the
  current pg rows (default `0.20`) -- applied only once pg holds at least
  `SKMEMORY_RECONCILE_PRUNE_MIN_SAMPLE` rows (default `20`), since on a tiny store
  a single legitimate delete trivially exceeds any fraction. The floor guard still
  protects the true cold-boot wipe (large pg, empty flat) at any size.

Refusals log `PRUNE REFUSED: ...` and fire a `crit` sk-alert. An allowed prune of
`>= SKMEMORY_RECONCILE_PRUNE_ALERT_ROWS` rows (default `50`) fires a `warn`
sk-alert. Override intentionally with `--force` or `SKMEMORY_RECONCILE_FORCE=1`
(e.g. a genuine bulk deletion). Same logic is vendored in both the standalone
`skmem_reconcile.py` and the in-package `skmemory/reconcile.py`.

**2. Restore-complete sentinel (systemd ordering).** The in-script guard stops a
wipe; the sentinel stops the reconcile/sync units from even *running* on an
unrestored machine. Gate the per-node reconcile and Syncthing-dependent timers on
a sentinel file that only exists once the flat store + agent home are restored:

```ini
# skmem-reconcile@.service (drop-in)
[Unit]
# Do not reconcile until the machine's flat memory tree is confirmed restored.
ConditionPathExists=%h/.skcapstone/RESTORE_COMPLETE
After=syncthing.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 %h/clawd/skcapstone-repos/skmemory/deploy/skmem-pg/skmem_reconcile.py %i
```

Cold-boot runbook order on a fresh/restored node:

1. Bring up skmem-pg from this dir (`docker compose up -d --build`) and load schema.
2. Join Syncthing and let the flat memory tree (`~/.skcapstone/agents/*/memory/`)
   finish syncing. Do NOT create the sentinel yet.
3. Verify the flat store is non-empty and complete for each agent this node serves
   (spot-check file counts vs. another node).
4. Only then `touch ~/.skcapstone/RESTORE_COMPLETE`. The reconcile timers, gated by
   `ConditionPathExists`, now fire and rebuild `memories` from the (present) flat
   source. If a timer fires before the sentinel exists it no-ops; if it somehow
   runs against a still-empty flat store, the in-script guardrail refuses the prune
   and alerts rather than wiping pg.
5. Remove the sentinel before any deliberate wipe/re-restore so units re-gate.

## Related

- `~/.skcapstone/docs/MEMORY_STORES.md` (store map)
- `docs/deploy-plan/skmemory-bulletproof-deploy.md` (deploy plan)
- skingest is the sole ingestion home and talks to this DB; its SOP points here.
