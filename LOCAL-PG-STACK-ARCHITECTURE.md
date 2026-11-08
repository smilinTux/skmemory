# SKMemory storage: the two-tier stack

This document describes `main` as it is. The only decisions it records are ones the
code enforces. For the full design see [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md);
for operations see [`SOP.md`](./SOP.md).

## The rule

There are exactly two tiers, and flat JSON files are the source of truth in both.

```
                 flat JSON memory files (truth, synced between nodes)
                  short-term/  mid-term/  long-term/  intentions/  ledger/
                         |                                  |
          rebuildable    |                                  |    rebuildable
   +---------------------v----------------+   +-------------v-------------------+
   | LOCAL tier (always on, offline)       |   | SHARED tier: skmem-pg           |
   |  SQLite index.db                      |   |  one Postgres container per     |
   |   memories table                      |   |  node, bound to loopback        |
   |   FTS5 index  -> `lexical_local` arm  |   |   pgvector  -> `semantic` arm   |
   |   intentions, ledger_events tables    |   |   pg_search -> `lexical` arm    |
   |  no services, no network              |   |   Apache AGE -> `graph` arm     |
   +---------------------------------------+   +---------------------------------+
```

| | Local tier | Shared tier (skmem-pg) |
|---|---|---|
| Contents | SQLite `index.db` with FTS5 | Postgres 17 with `vector`, `pg_search` and `age` |
| Retrieval arms | `lexical_local` (bm25 over title, content, summary, tags) | `semantic` (mxbai cosine), `lexical` (BM25), `graph` (AGE, off by default) |
| Needs | Python and the flat files | a running container, `SKMEMORY_PG_DSN`, an embed endpoint |
| When it is down | n/a, always on | search degrades to the local arm and `health` says so |
| Rebuild | `skmemory reindex` | `skmemory sync --vector --graph` |

"Shared" means the same schema and the same corpus scope on every node, not one
central database. Each node runs its OWN writable skmem-pg on loopback and agents point
only at it. There is no streaming replication, no remote primary and no failover: the
`memories` rows and their embeddings are a deterministic function of the flat files plus
the embedding model, so any node regenerates them. Losing a node's Postgres costs a
rebuild, never data.

## What is retired

ChromaDB (the old embedded vector store), Qdrant (skvector) and FalkorDB (skgraph) are
deleted from the code, along with the setup wizard and HA endpoint routing that drove
them.

- `SKMEMORY_VECTOR_BACKEND=chroma|skvector|qdrant|skgraph|falkordb` fails fast with a
  retirement message (non-zero exit on the CLI, red `health`). It never falls back to
  another store, because a different store gives different answers.
- A stale `chroma_*`, `skvector_*`, `skgraph_*` or endpoint key in a `skmemory.yaml`,
  and stale `SKMEMORY_SKVECTOR_*` or `SKMEMORY_SKGRAPH_URL` variables, are ignored with
  a one-time warning so an old node config cannot break a session hook.
- No data directory of a retired backend is touched; delete them yourself.

## The shared tier in detail

**Image.** `docker-compose.yml` builds `skmem-pg:pg17-bm25-age` from
`deploy/skmem-pg/Dockerfile`: `pgvector/pgvector:pg17` plus ParadeDB `pg_search` (real
BM25) plus Apache AGE. The server runs with `shared_preload_libraries=pg_search,age`.

**Schema.** `deploy/skmem-pg/schema.sql` is a post-cutover snapshot. Forward migrations
are listed in `deploy/skmem-pg/migrations.txt` and apply on top of it, in order, both on a
fresh `docker compose up` (through `deploy/skmem-pg/initdb/00-run-init.sh`) and on a live
node (`skmemory pg migrate`). The two read the same manifest, so they cannot drift.
`deploy/skmem-pg/06-memory-schema-v2.sql` adds the typed memory-v2 columns; a node that
has not run it keeps working because `memory_json` carries every field.

**Embeddings.** `mxbai-embed-large`, 1024 dimensions, from the endpoint named by
`SKMEMORY_EMBED_URL` (an ordered failover list in `SKMEMORY_EMBED_URLS`). Every
`vector(...)` column is `vector(1024)`. The write path never stores a null or zero
vector: if no endpoint answers it raises rather than poison recall. Changing the model or
dimension means rebuilding the vector tier before trusting semantic results.

**Search functions.** `hybrid_search_memories` and `hybrid_search_docs` fuse a vector rank
with a BM25 rank (RRF). `hybrid_search_docs_public` is the fail-closed variant that ranks
over the `docs_public` view; every non-owner reader of the `docs` table must use it.

**Graph.** One AGE graph per agent, `<agent>_knowledge`, mirrors each memory with its
tags, source, relations (`RELATED_TO`, `PROMOTED_FROM`, `SUPERSEDES`, `EXTENDS`), and,
for decomposed documents, entities, citations, claims and sections. It is a retrieval arm
only when `SKMEMORY_GRAPH_ARM=on`; the `skmemory graph` commands and `memory_graph` work
regardless.

**Connection.** `SKMEMORY_PG_DSN` (environment) wins over `skmemory.yaml`, which wins over
a local default. A DSN that points at a non-loopback host is flagged loudly. Do not put
the DSN or password in a file that is synced between nodes.

## The local tier in detail

`<agent memory dir>/index.db` is a SQLite database rebuilt from the flat files at any
time. It holds the `memories` table, an FTS5 full-text index, the `intentions` table and
the `ledger_events` table.

- **FTS5.** `search_lexical_local` ranks with bm25 over the full title, content, summary
  and tags, with the same layer, tag, source, context-tag and `as_of` filters as the other
  arms. New saves are indexed immediately; existing rows are backfilled lazily in small
  time-bounded slices, or all at once with `skmemory fts-backfill`. It is disabled, and any existing
  FTS tables are scrubbed, for the encrypted (vaulted) backend.
- **WAL bound.** `journal_size_limit` (64 MB) and `wal_autocheckpoint` are set on every
  connection, with a throttled passive checkpoint after index writes, so a burst cannot
  leave a multi-GB `-wal` behind. `memory_health` reports the WAL size and degrades at
  256 MB.
- **Drift.** `skmemory health` reports flat-versus-index drift. `skmemory sync` exports
  SQLite-only memories to flat files first and then reindexes, so a rebuild never destroys
  an orphan.

## Reading the result

`memory_health` and `skmemory health` report each retrieval arm as `ok`, `abstained`,
`unavailable`, `failed`, `disabled` or `timeout`. A config-disabled arm (the graph arm by
default) is never a failure. Run the sync timers (`systemd/README.md`) so the shared tier
tracks the flat files. Backup and restore are drilled in
[`docs/RUNBOOK-restore.md`](./docs/RUNBOOK-restore.md).

## Rotating the skmem-pg password

Use `deploy/skmem-pg/rotate-password.sh` (try `--dry-run` first). It rotates the postgres
superuser password and, with `--bind-loopback`, recreates the container bound to
`127.0.0.1` only. That second part matters: a container can drift to `0.0.0.0:5432` while
`docker-compose.yml` and the node configuration standard both say loopback, and a
rotation is the natural moment to fix it. The script never prints a password or a DSN.

```bash
deploy/skmem-pg/rotate-password.sh --dry-run --bind-loopback          # read-only plan
deploy/skmem-pg/rotate-password.sh --bind-loopback                    # do it, restart nothing
deploy/skmem-pg/rotate-password.sh --bind-loopback --restart-units    # also restart SK services
deploy/skmem-pg/rotate-password.sh --remove-old <name>-old-<stamp>    # after the soak
```

Parameters: `--env-file` (default `~/.config/skmemory/pg.env`), `--container` (default
`skmem-pg`), `--port`, `--user`, `--db`, `--backup-dir`, `--propagate`, `--canonical-env-file`. Run it
as the node owner, not root. It refuses to run under `bash -x`, which would print the
passwords. Propagation (step 6) is opt-in and refused unless `--env-file` is the canonical
env file.

### The procedure

1. **Backups**, all mode 600 in a 700 directory: a copy of the env file, `docker inspect`,
   `pg_dump -Fc`, and `pg_dumpall --globals-only`.
2. **New password** generated into a 600 file. `ALTER USER ... PASSWORD ...` goes to
   `docker exec -i ... psql` on stdin, never on a command line (argv is world-readable
   in `ps`). The same session first runs `SET log_statement = 'none'`, because with
   `log_statement` set to `ddl` or `all` the ALTER, password included, would otherwise land
   in the server log. Check `log_min_duration_statement` and any log shipping too.
3. **Env file**: only the `:<password>@` slot of `SKMEMORY_PG_DSN` is replaced, matched
   exactly against the old value, and the script asserts the old value is gone and the
   new one present before it swaps the file in.
4. **Recreate (with `--bind-loopback`)**: stop, rename the old container to
   `<name>-old-<stamp>`, set its restart policy to `no`, then `docker run` with the same
   volume, shm size, command (`-c shared_preload_libraries=pg_search,age`), custom env,
   labels and network, and `-p 127.0.0.1:PORT:5432`. `POSTGRES_PASSWORD` is deliberately NOT
   passed: the data directory is already initialised, so the image entrypoint never reads
   it (verified on a throwaway container: it starts and accepts the rotated password
   without it), and passing it would store the new secret in `docker inspect` of the new
   container. The remaining custom env goes through a temporary `--env-file` that is
   deleted afterwards. If `docker run` fails, the old container is renamed back, its
   restart policy restored and it is started. Note that the backup directory's
   `docker-inspect.json` is of the OLD container and does hold the old password: keep it
   at mode 600 and delete it once the soak is over.
5. **Verify**: `pg_isready`, loopback-only `docker port`, extensions `vector`, `pg_search`
   and `age`, the `memories` row count, the new password logs in, and the OLD password is
   refused. A login test that only proves the new password works would pass even if the
   rotation had changed nothing.
6. **Propagate (opt-in, `--propagate`)** the new DSN to the systemd user manager and the
   dbus activation environment (see the first lesson below). It refuses unless `--env-file`
   resolves to the canonical env file, because it writes into the live manager. Without the
   flag, do it yourself from the canonical file.
7. **Consumers**: the script lists user processes whose environment still holds the old
   DSN, maps them to units, and prints the SK service candidates. It restarts them only
   with `--restart-units`. Desktop session services are never restarted, and interactive
   agent sessions started before the rotation keep the old DSN: their owner restarts them.
8. **Soak, then remove**: keep the old container stopped for a while, then `--remove-old`.
   Both containers share ONE data volume, so the old container is a rollback for the
   container configuration (ports, env, flags) only, not for data. Never start both at
   once. The data rollback is the `pg_dump -Fc` backup.

### Lessons

- **`systemctl --user daemon-reload` does not re-read `environment.d`.** The user manager
  keeps the environment it started with. After changing `SKMEMORY_PG_DSN`, export the new
  value in a shell and run `systemctl --user import-environment SKMEMORY_PG_DSN` (the
  name only, so the value stays off argv), plus
  `dbus-update-activation-environment --systemd SKMEMORY_PG_DSN`. Then restart the
  services. Without this, units restarted after the rotation come back with the old
  password and run keyword-only with no error.
- **The image tag can be missing locally while the old container runs.** Do not rely on
  the tag: read the old container's image ID (`docker inspect --format '{{.Image}}'`) and
  re-tag it before `docker run`. The script does this.
- **Running processes keep their environment.** Find them by reading
  `/proc/<pid>/environ`, restart the SK services among them, and leave desktop and
  interactive sessions to their owners.
- **Testing must not touch the live manager.** Step 6 is off by default. Any test or manual
  run that uses `--propagate` must put stub `systemctl` and
  `dbus-update-activation-environment` first on `PATH`.
- **Compose drift.** Check `docker port skmem-pg` after any recreate. Only `127.0.0.1`
  should appear.

Rollback of the container configuration: stop the new container, rename the old one back,
`docker start` it. The password is stored in the shared volume, so it is already the NEW
password; to go back to the old one run `ALTER USER` with the value from the backed-up env
file copy and restore that copy. Rolling back data means restoring the `pg_dump -Fc` backup.

## Where each piece lives

| Concern | File |
|---|---|
| Backend selection, retired-backend messages | `skmemory/backend_select.py` |
| SQLite index and FTS5 | `skmemory/backends/sqlite_backend.py`, `skmemory/fts_local.py` |
| WAL bound | `skmemory/wal_guard.py` |
| pgvector and BM25 | `skmemory/backends/pgvector_backend.py` |
| AGE graph | `skmemory/backends/age_backend.py` |
| Fusion and routing | `skmemory/fusion.py`, `skmemory/store.py` |
| Flat-to-pg reconcile | `skmemory/reconcile.py` |
| Image, schema, migrations | `deploy/skmem-pg/` |
| Password rotation, loopback rebind | `deploy/skmem-pg/rotate-password.sh` |
