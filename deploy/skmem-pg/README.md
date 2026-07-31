# skmem-pg image build (vendored from ~/skmem-build, 2026-07-17)

This directory makes the production skmem-pg image reproducible from the repo.
Previously it was buildable only from `~/skmem-build/` on .158, which was not a
git repo (DR gap, coord card a4b414c9).

## Contents

| File | Purpose |
| --- | --- |
| `Dockerfile` | `pgvector/pgvector:pg17` + ParadeDB `pg_search` 0.24.0 (real BM25) + Apache AGE PG17/v1.7.0-rc0 (built from source). Produces `skmem-pg:pg17-bm25-age`. |
| `schema.sql` | `pg_dump --schema-only --no-owner --no-privileges` of the LIVE .158 `skmemory` DB, taken 2026-07-17 (post mxbai cutover, includes the 2026-07-06 ParadeDB query-shape fix). Contains `hybrid_search_docs`, `hybrid_search_memories`, all HNSW + BM25 index DDL. Applied first by the compose init wrapper on a fresh `docker compose up`. |
| `migrations.txt` | **Ordered manifest (card OPS1.3):** the single source of truth for the additive, idempotent FORWARD migrations that apply ON TOP of `schema.sql`. Read by BOTH the compose init wrapper and `skmemory pg migrate`, so fresh-boot and live-node paths never drift. Historical/superseded migrations are deliberately NOT listed. |
| `initdb/00-run-init.sh` | **Compose first-boot wrapper (card OPS1.3):** runs once during initdb; applies `schema.sql` then every migration in `migrations.txt`, in order, `ON_ERROR_STOP`. A clean `docker compose up` therefore yields a fully-migrated instance (BM25 + AGE + ops namespace). |
| `verify-ops.sql` | Read-only verification query for `03-ops-namespace.sql` (extracted from this README, card OPS1.3). Run by the operator, by `skmemory pg migrate --verify`, and by the init wrapper. |
| `02-enable-bm25-age.sql` | Historical migration: first enable of pg_search/AGE + original hybrid functions. Kept for provenance; superseded by `schema.sql` for fresh installs. NOT auto-applied (not in `migrations.txt`). |
| `03-cutover-mxbai.sql` | Historical migration: bge to mxbai embedding-column cutover (bge kept as `emb_bge_legal`). Superseded by `schema.sql`; NOT idempotent (its `ALTER TABLE ... RENAME COLUMN` fails on a fresh snapshot), so it must never auto-run. Provenance only. |
| `03-ops-namespace.sql` | **Additive migration (card SB0.3):** creates the `ops` schema + `ops_brain` AGE graph for the skbrain operations corpus. Auto-applied on a fresh compose boot (via `migrations.txt`); on a LIVE node apply with `skmemory pg migrate`. See "The ops namespace migration" below. |
| `skbrain.conf.example` | Env drop-in TEMPLATE for the ops role passwords (card OPS1.3). Copy to `~/.config/environment.d/skbrain.conf`, fill from skvault; consumed by `skmemory pg roles`. No secrets committed. |
| `skmem_reconcile.py` | Daily-cron reconciler that rebuilds the `memories` derived cache from the synced flat JSON source of truth. Prune step is guardrailed against cold-boot wipes (see below). |

## Build and run

From the repo root:

```sh
export SKMEM_PG_PASSWORD=change-me   # never committed; compose refuses to start without it
docker compose up -d --build
docker exec skmem-pg psql -U postgres -d skmemory -c "SELECT extname FROM pg_extension;"
```

A fresh `docker compose up` now comes up FULLY MIGRATED: the init wrapper
(`initdb/00-run-init.sh`) applies `schema.sql` and then every forward migration
in `migrations.txt` (currently the skbrain `ops` namespace), in order, during
the one-time initdb phase. No manual migration step is needed for a fresh DB.
The one thing still done by hand on a fresh DB is binding the ops LOGIN roles
(passwords cannot live in the image); see "Role login binding" below.

Server must run with `shared_preload_libraries=pg_search,age` (the compose file
sets this via `command:`).

## Migrations: fresh boot vs live node (card OPS1.3)

`migrations.txt` is the ordered, single source of truth for the additive,
idempotent forward migrations that apply on top of the `schema.sql` snapshot.

- **Fresh DB (compose first boot):** applied automatically by
  `initdb/00-run-init.sh`. Zero risk: there is no live data on first initdb.
- **Live node (existing DB):** applied by the guarded runner, operator-initiated:

  ```sh
  # apply every pending forward migration (idempotent), with pre-dump + verify:
  skmemory pg migrate

  # or one named migration; preview first with --dry-run:
  skmemory pg migrate 03-ops-namespace.sql --dry-run
  skmemory pg migrate 03-ops-namespace.sql
  ```

- **CI:** never applies migrations. Unchanged.

`skmemory pg migrate` contract (the interface the skos provisioner, OPS1.2,
calls):

| Flag | Default | Meaning |
| --- | --- | --- |
| `SCRIPT` (arg) | all | A manifest name (`03-ops-namespace.sql`) or a path. Omit to apply every forward migration in order. |
| `--container NAME` | `skmem-pg` (env `SKMEM_PG_CONTAINER`) | `docker exec -i <container> psql` transport (the README apply command verbatim). |
| `--dsn DSN` | env `SKMEMORY_PG_DSN` | Use a local/remote `psql` with a libpq DSN instead of docker exec. |
| `--db` / `--user` | `skmemory` / `postgres` | Target DB and superuser. |
| `--pre-dump` / `--no-pre-dump` | on | `pg_dump -Fc` insurance to `--dump-dir` (default `~/skmem-backups`) before applying. |
| `--verify` / `--no-verify` | on | Run the migration's verify script (`verify-ops.sql`) after applying (non-fatal). |
| `--dry-run` | off | Print the plan and exit; touch nothing. |

Every apply runs with `ON_ERROR_STOP=1`; each migration is one guarded
transaction, so a re-run is a safe no-op and a partial apply cannot occur.

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

## The ops namespace migration (card SB0.3 + OPS1.3)

`03-ops-namespace.sql` is an ADDITIVE, IDEMPOTENT migration that adds the skbrain
operations namespace to the skmem-pg instance: an `ops` schema
(`ops.wiki_nodes` / `ops.wiki_chunks` / `ops.links` + own HNSW + own BM25 +
`ops.hybrid_search_ops`), a dedicated `ops_brain` AGE graph, and a REVOKE-from-PUBLIC
privacy wall granting only the `skbrain_ops_ro` / `skbrain_ops_rw` roles. Nothing in
`public` changes.

- **Fresh DB:** applied automatically at compose first boot (it is listed in
  `migrations.txt`, run by `initdb/00-run-init.sh`). Nothing to do.
- **Live node:** applied by the operator (not CI, not the app), .158-first,
  via `skmemory pg migrate` (which does the pre-dump + apply + verify below in
  one guarded command). skmem-pg is LOCAL per node, so it runs once per node.

The manual `docker exec` steps below remain valid for a hands-on apply; the
runner wraps exactly these.

**Preconditions:** the instance is on `skmem-pg:pg17-bm25-age` with
`shared_preload_libraries=pg_search,age` (so `hnsw`, `bm25`, and `ag_catalog` exist).

### 1. Dump first (rollback insurance)

```sh
# On .158, full instance dump BEFORE touching anything:
docker exec skmem-pg pg_dump -U postgres -Fc skmemory \
  > ~/skmem-backups/skmemory-pre-ops-$(date +%Y%m%d-%H%M).dump
```

### 2. Apply on .158 first, in a transaction

```sh
docker exec -i skmem-pg psql -U postgres -d skmemory -v ON_ERROR_STOP=1 \
  < deploy/skmem-pg/03-ops-namespace.sql
```

The whole migration is one `BEGIN; ... COMMIT;` with `ON_ERROR_STOP`; any error
rolls the entire thing back, leaving the DB untouched.

### 3. Verify (.158)

```sh
docker exec skmem-pg psql -U postgres -d skmemory -c "
  SELECT 'schema'   AS obj, count(*) FROM information_schema.schemata WHERE schema_name='ops'
  UNION ALL SELECT 'tables',   count(*) FROM information_schema.tables WHERE table_schema='ops'
  UNION ALL SELECT 'hnsw',     count(*) FROM pg_indexes WHERE schemaname='ops' AND indexname='ops_chunks_hnsw'
  UNION ALL SELECT 'bm25',     count(*) FROM pg_indexes WHERE schemaname='ops' AND indexname='ops_chunks_bm25'
  UNION ALL SELECT 'fn',       count(*) FROM pg_proc WHERE proname='hybrid_search_ops'
  UNION ALL SELECT 'graph',    count(*) FROM ag_catalog.ag_graph WHERE name='ops_brain';"

# Privacy wall: PUBLIC must have NO usage on ops (expect 'f').
docker exec skmem-pg psql -U postgres -d skmemory -c \
  "SELECT has_schema_privilege('public','ops','USAGE') AS public_can_use_ops;"
```

Expected: schema=1, tables=3, hnsw=1, bm25=1, fn=1, graph=1, `public_can_use_ops = f`.
This is the exact query in `verify-ops.sql`. Now bind the login roles (below).

### Role login binding (card OPS1.3, closes the G2 credential gap)

`03-ops-namespace.sql` creates only the NOLOGIN GROUP roles `skbrain_ops_rw`
(projector: read+write ops) and `skbrain_ops_ro` (Atlas/operator retrieval:
read-only). A projector cannot connect until a LOGIN role is bound into a group.
The convention (no password ever in the repo, image, argv, or logs):

| Login role | Group | Password env var | skvault entry |
| --- | --- | --- | --- |
| `skbrain_projector` | `skbrain_ops_rw` | `SKBRAIN_PG_PROJECTOR_PW` | `SKBRAIN_PG_PROJECTOR_PW` |
| `skbrain_reader` | `skbrain_ops_ro` | `SKBRAIN_PG_READER_PW` | `SKBRAIN_PG_READER_PW` |

1. Source the passwords from skvault into the env drop-in. Copy the template
   `skbrain.conf.example` to `~/.config/environment.d/skbrain.conf` and fill the
   two values from the skvault entries above (create them on first install if
   absent). The template shows the exact `skvault creds-get` commands and the
   DSN drop-ins.

2. Bind the roles (idempotent: create-if-missing else ALTER, then GRANT the
   group; the password is fed to psql on STDIN only):

   ```sh
   skmemory pg roles              # both roles from env
   skmemory pg roles --dry-run    # preview (password redacted); touch nothing
   ```

3. Consumers connect with the DSN drop-in, e.g. the projector:
   `postgresql://skbrain_projector:${SKBRAIN_PG_PROJECTOR_PW}@127.0.0.1:5432/skmemory`.

The skos provisioner (OPS1.2) calls `skmemory pg migrate` then `skmemory pg
roles` to reach the same state non-interactively (`--skip-missing` tolerates a
role whose password is not yet provisioned).

### 4. Fleet out (only after .158 is green)

skmem-pg is per-node local; repeat steps 1-3 on each node (`.41`, `.100`, ...) that
serves skbrain. Re-running on an already-migrated node is a safe no-op (every
statement is `IF NOT EXISTS` / `CREATE OR REPLACE` / catalog-guarded). Record the
apply as an ITIL change (one per node per the epic's deploy discipline).

### Rollback

Additive, so rollback is a clean drop (do the dump-restore only if a public object
was somehow disturbed, which this migration never touches):

```sh
docker exec skmem-pg psql -U postgres -d skmemory -v ON_ERROR_STOP=1 -c "
  SELECT ag_catalog.drop_graph('ops_brain', true);   -- drops the ops_brain schema
  DROP SCHEMA IF EXISTS ops CASCADE;                  -- drops tables/indexes/function
  -- roles are cluster-global; drop only if no other DB uses them:
  DROP ROLE IF EXISTS skbrain_ops_ro;
  DROP ROLE IF EXISTS skbrain_ops_rw;"
```

`drop_graph(..., true)` cascades the graph's label tables and removes the
`ops_brain` schema; `DROP SCHEMA ops CASCADE` removes the relational side. Nothing
in `public` is affected either way. Full restore fallback:
`docker exec -i skmem-pg pg_restore -U postgres -d skmemory --clean < <the pre-ops dump>`.

### Testing

`tests/test_ops_namespace_migration.py` is a STRUCTURAL (DB-independent) test that
parses this SQL and asserts the objects, the ops-only scoping of `hybrid_search_ops`,
the embedding dim, idempotence markers, and the REVOKE-from-PUBLIC wall. It runs in
CI without a database. Behavioural (live-DB) validation is the operator "verify" step
above, deliberately NOT automated, since this migration is never applied from CI.
`psql` is not available in this repo's dev image, so the SQL was not executed here;
apply-time `ON_ERROR_STOP` + the one-transaction wrapper are the execution safety net.

`tests/test_pg_migrate.py` covers the `skmemory pg migrate` / `pg roles` runner:
manifest ordering, historical-migration exclusion, pre-dump/verify wiring, the two
transports, the idempotent role DDL, and password redaction. `tests/test_compose_initdb_wiring.py`
structurally validates the compose init wiring (wrapper mounted, source dir mounted,
schema not double-mounted, manifest excludes the non-idempotent historical migrations).
Both are DB/docker-independent: the runner is built as pure plan objects with an
injectable executor, so ordering + idempotence are asserted with a mock, never real psql.

## Related

- `~/.skcapstone/docs/MEMORY_STORES.md` (store map)
- `docs/deploy-plan/skmemory-bulletproof-deploy.md` (deploy plan)
- skingest is the sole ingestion home and talks to this DB; its SOP points here.
