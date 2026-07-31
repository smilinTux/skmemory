# CLAUDE.md (skmemory)

Agent guidance for working in this repo. Keep it green, keep installs seamless.

## What this is

Universal AI memory system. Flat JSON files under
`~/.skcapstone/agents/<agent>/memory/{short,mid,long}-term/` are the **source of
truth** (synced via Syncthing). SQLite (`index.db`) is a local rebuilt index.
`skmem-pg` (Postgres: pgvector + ParadeDB BM25 + Apache AGE) is the production
backend, run LOCAL per node on `127.0.0.1:5432` (no replication, no SPOF); its
`memories` table is a derived cache rebuilt from the flat store by
`deploy/skmem-pg/skmem_reconcile.py`.

Active agent resolves from `SKAGENT` -> `SKCAPSTONE_AGENT` -> `SKMEMORY_AGENT`.

## Writing style (HARD RULE)

**NEVER use em dashes (—) or en dashes (–)** anywhere: code, comments, docstrings,
docs, commit messages. Restructure with commas, parentheses, colons, or a new
sentence. Regular hyphens `-` are always fine, including ranges (`5-10`). This is
the single biggest AI tell and it is banned outright.

## Dev / CI commands (this is what CI gates on)

```bash
# tests (CI runs on py3.11 + py3.12):
~/.skenv/bin/python -m pytest tests/ -q

# lint + format (CI: ruff check + ruff format --check over skmemory/ AND tests/):
~/.skenv/bin/ruff check skmemory/ tests/
~/.skenv/bin/ruff format --check skmemory/ tests/
```

CI (`.github/workflows/ci.yml`) has three jobs: `test` (pytest + coverage),
`lint` (`ruff format --check` then `ruff check`, both over `skmemory/` and
`tests/`), and `build` (wheel + sdist + `twine check`). All must be green.
Config lives in `pyproject.toml` (`[tool.ruff]` line-length 99, rules
`E,F,W,I,UP,B,SIM`, `E501` ignored). Before claiming green, run BOTH the lint and
format checks over `tests/` too, not just `skmemory/` (the README's older snippet
only lints `skmemory/`; CI is stricter).

Common lint gotchas here: `B904` (in an `except`, use `raise ... from exc`), `UP045`
(`X | None` not `Optional[X]`), `E731` (`def` not `lambda =`).

## skmem-pg: seamless out of the box

A fresh `docker compose up -d --build` yields a **fully-migrated** instance. The
compose init wrapper (`deploy/skmem-pg/initdb/00-run-init.sh`) applies `schema.sql`
then every forward migration in `deploy/skmem-pg/migrations.txt`, in order,
`ON_ERROR_STOP`. `migrations.txt` is the single source of truth read by BOTH the
init wrapper and `skmemory pg migrate`, so fresh-boot and live-node paths never
drift.

- **Fresh node:** `export SKMEM_PG_PASSWORD=... ; docker compose up -d --build`.
- **Live node:** `skmemory pg migrate` (idempotent, pre-dump + verify, one guarded
  transaction). Preview with `skmemory pg migrate <name>.sql --dry-run`. CI never
  applies migrations.
- **ops roles:** `skmemory pg roles` binds `skbrain_projector`/`skbrain_reader`
  LOGIN roles from skvault (`SKBRAIN_PG_PROJECTOR_PW` / `SKBRAIN_PG_READER_PW` via
  `~/.config/environment.d/skbrain.conf`; template `deploy/skmem-pg/skbrain.conf.example`).
- **Verify:** `skmemory health`, `skmemory operator observe`, and
  `docker exec -i skmem-pg psql -U postgres -d skmemory < deploy/skmem-pg/verify-ops.sql`.

**Never** add the historical migrations (`02-enable-bm25-age.sql`,
`03-cutover-mxbai.sql`) to `migrations.txt`: they are already baked into
`schema.sql`, and `03-cutover-mxbai.sql` is not idempotent (its `RENAME COLUMN`
fails on a fresh schema). Adding a new migration = drop `NN-name.sql` in
`deploy/skmem-pg/` and add one line to `migrations.txt`; it then auto-applies
everywhere. Full detail: `deploy/skmem-pg/README.md`.

## Embeddings

`mxbai-embed-large` at **1024 dimensions** everywhere (Ollama `:11434`, `ctx=512`),
network fallback `mixedbread-ai/mxbai-embed-large-v1`. `EMBED_URL` /`EMBED_MODEL`
override the reconcile backend. Every `vector(…)` column (`schema.sql` + the `ops`
namespace) is `vector(1024)`; the dimension must match across the mesh or
cross-collection recall breaks.

## Secrets

No secret ever lives in the repo, image, argv, or logs. Reference skvault entries
by name (`SKMEM_PG_PASSWORD`, `SKBRAIN_PG_PROJECTOR_PW`, `SKBRAIN_PG_READER_PW`).
`docker compose` refuses to start without `SKMEM_PG_PASSWORD` (no default).

## Conventions

- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- SK* packages install into `~/.skenv/` (CLIs in `~/.skenv/bin/`).
- Structural tests (`tests/test_compose_initdb_wiring.py`, `tests/test_pg_migrate.py`,
  `tests/test_ops_namespace_migration.py`) validate the OOTB wiring WITHOUT a DB or
  docker (pure plan objects + a parsed-SQL assertion), so they run in CI. Behavioural
  live-DB validation is the operator `verify` step, deliberately not automated.
