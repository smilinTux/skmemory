# skmemory Bulletproof Deployment Plan

Date: 2026-07-09
Initiative: bulletproof-deploy
Repo: `~/clawd/skcapstone-repos/skmemory`

Definition of bulletproof used here: reproducible from scratch on a new machine, secrets never in git, HA with no single point of failure ("if you need one, get two"), CI-gated, observable, self-recovering, and documented well enough that a cold machine can stand it up.

## 1. Current State

The skmemory Python package itself is strong: flat JSON files are the source of truth, the SQLite index and vector/graph stores are rebuildable (`reindex`, `sync`, resumable `scripts/migrate-flat-to-pgvector.py`), there are 63 test files with roughly 1,157 test functions, three CI workflows (ci.yml matrix + coverage + ruff + build check, pytest.yml, tag-gated publish.yml), a fortress integrity layer with SHA-256 verify and alert hooks, and templated per-agent systemd units with an idempotent installer. No secrets are committed; publish uses GitHub secrets and sealing uses an env var.

The deployed memory stack is a different story. Production is skmem-pg on .158:5432 (pgvector + ParadeDB BM25 + Apache AGE) driven by `skmemory/backends/pgvector_backend.py` and `age_backend.py`, with mxbai embeddings served only from .100. But:

- The machinery that keeps production alive is not in git. `~/skmem-build/` (verified: not a git repo) holds the skmem-pg Dockerfile, `02-enable-bm25-age.sql`, `03-cutover-mxbai.sql`, and `skmem_reconcile.py` (the daily flat-to-pg reconcile that ARCHITECTURE.md itself names as THE sync path). `skmem-pg-backup.sh` lives in `~/.skcapstone/scripts/` and `skmem-health.sh` in `~/.hermes/scripts/`. The `hybrid_search_memories` SQL function (with the 2026-07-06 ParadeDB query-shape fix) exists only in the live DB and that build dir.
- The .41 mirror on :5433 is empty. There is zero replication tooling anywhere in the repo, only aspirational comments in `pgvector_backend.py:5-11`.
- Everything the repo ships for sync and HA targets retired backends: `cli.py sync --vector/--graph` reconciles ChromaDB and FalkorDB (zero pgvector references in cli.py, verified), `systemd/skmemory-sync@.service` therefore no-ops against the live stack, `docker-compose.yml` stands up Qdrant + FalkorDB, and `endpoint_selector.py` (391 lines of real failover logic) has zero pgvector or embed-URL coverage.
- Config is drifted: `config.py` still has chroma_* fields and no pgvector/AGE fields; `mcp_server.py` defaults the vector backend to pgvector while `openclaw.py` defaults to off; `pgvector_backend.py:34` defaults to port 5433 while `age_backend.py:60` defaults to 5432, and both embed the trivial default password `skmemory` in a committed DSN.
- CI never exercises the live path: pytest.yml excludes the integration tests and installs no Postgres, and the pgvector/AGE tests skip when no DSN is reachable.

Operationally on .158 today the stack IS reconciled (04:15), reindexed (04:30), dumped (03:15, 14 retained), tarballed (02:45 GFS), and health-monitored to sk-alert daily. The gap is that almost none of that lives in this repo, dumps land on the same box they protect, restore has never been drilled, and reconcile covers agent lumina only.

## 2. Target: What Bulletproof Means for This Repo

1. **Reproducible.** `git clone` plus one documented bootstrap path stands up the full live stack (skmem-pg image build, init SQL, hybrid search functions, index DDL, reconcile, backup, health monitor) on a cold machine. Nothing production-critical exists only in `~/skmem-build/`, `~/.skcapstone/scripts/`, or `~/.hermes/scripts/`.
2. **No secrets in git.** The default-password DSN is gone from source; credentials come from env/skvault/capauth with a documented provisioning step. The live pg password is rotated off the trivial default.
3. **No single point of failure - by node self-sufficiency, not replication.** skmem-pg is LOCAL, per-node, and rebuildable from source. It is NOT streaming-replicated, NOT a central/shared system of record, and NOT a SPOF. Each node runs its OWN writable skmem-pg on `localhost:5432`; agents connect only to `localhost`. The `memories` table is a DERIVED cache (same class as `index.db`): rebuilt from the Syncthing-synced flat JSON by `reconcile.py` (idempotent, agent-scoped), and embeddings are a deterministic function of flat content + mxbai on .100, so any node regenerates them locally. HA/DR = node self-sufficiency + rebuild-from-source (flat files + git wiki, both replicated) + the daily `pg_dump` that lands in the synced tree as a backup. There is NO primary/replica/failover for skmem-pg. Streaming replication (.158 -> .41 standby on :5433) was abandoned: ParadeDB Community cannot serve `pg_search` reads in recovery, so the standby broke, bloated primary WAL, and made .41 depend on .158 (see prb-6f069c5e; hardened in 0.11.3). Embedding still has a failover path or at minimum fails loudly instead of silently writing NULL embeddings. Any primary/replica wording below applies only to the retired SKVector(Qdrant)/SKGraph(FalkorDB) recall endpoints.
4. **CI-gated.** The production code path (pgvector backend, AGE backend, hybrid search) runs against a real skmem-pg service container in GitHub Actions on every PR.
5. **Observable.** In-repo, deployable alerting for the failure modes that actually happened: NULL-embedding growth, flat-vs-pg row drift, embed endpoint down, replication lag, per-agent drift. Wired to sk-alert.
6. **Self-recovering.** Reconcile is versioned, runs for all agents, and re-embeds NULLs; systemd timers in the repo drive the live backend, not retired ones.
7. **Documented.** README, ARCHITECTURE.md, HA.md, docker-compose, and systemd/README.md describe the pgvector/AGE world. A restore drill is documented, scripted, and has been executed at least once with the result recorded.

## 3. Gap Analysis (severity-ordered)

| # | Area | Severity | Summary |
|---|------|----------|---------|
| G1 | skmem-pg per-node self-sufficiency / HA | critical | The failure to actually retire the central model is the SPOF: every synced `skmemory.yaml` still hardcodes `pgvector_dsn=192.168.0.158:5432`, so agents depend on .158 even where a local pg exists. The fix is NOT replication (the .158 -> .41 streaming standby on :5433 was abandoned; ParadeDB Community cannot serve `pg_search` reads in recovery). The fix is node self-sufficiency: each node runs its OWN writable skmem-pg on `localhost:5432`, rebuilds `memories` from synced flat JSON via `reconcile.py` and `docs` from the git wiki via skingest, and agents point only at `localhost`. Resolution = re-home DSN to `localhost` + per-node reconcile timers, plus off-box `pg_dump` as a backup safety net for the 33k-node AGE graph. |
| G2 | Un-versioned production glue | critical | Dockerfile, init SQL, `hybrid_search_memories` definition, `skmem_reconcile.py`, `skmem-pg-backup.sh`, `skmem-health.sh` all live outside git (`~/skmem-build/`, `~/.skcapstone/scripts/`, `~/.hermes/scripts/`). Losing .158 destroys the only copies of most of them. A cold machine cannot stand up skmem-pg from this repo. |
| G3 | Embed endpoint failover | high | Single embed_url on the .100 GPU box with known VRAM-flapping history. On failure `_embed()` returns `[]` and `save()` writes embedding NULL: silent recall degradation, the exact past incident. `endpoint_selector.py` does not cover embeds or pgvector at all. |
| G4 | Shipped sync targets retired backends | high | `cli.py sync` reconciles chroma/FalkorDB (both retired); `systemd/skmemory-sync@.service` no-ops against production. Live reconcile depends entirely on the un-versioned cron. |
| G5 | Config drift / dual defaults | medium (high blast radius) | chroma fields in config.py, no pgvector/AGE fields; mcp_server vs openclaw disagree on default backend; pgvector defaults port 5433 while AGE defaults 5432; docs vs live split across env vars, pg.env, and per-agent yaml. This exact failure class broke recall for weeks. |
| G6 | CI never tests the live backend | medium | pgvector/AGE/hybrid tests skip without a reachable DSN; pytest.yml installs no Postgres. Production path has no CI gate. |
| G7 | Backup/restore ceremony | medium | Backup script out-of-repo, dumps land on the same box, restore never drilled, hybrid function and index DDL undocumented, recovery time on a cold machine unknown. |
| G8 | Observability in-repo | medium | No in-repo alerting for NULL-embedding growth, flat-vs-pg drift, embed endpoint down, or non-lumina agent drift. No metrics instrumentation. |
| G9 | Weak default credentials | medium | Default password `skmemory` committed in two source files and used by the live deployment, which listens on 0.0.0.0:5432 on the LAN. Any LAN device can read or poison all agent memories. |
| G10 | Reconcile covers lumina only | medium | Other agents writing via MCP can drift in pg indefinitely with no self-heal. |
| G11 | Docs drift | low | README, ARCHITECTURE diagrams, HA.md, docker-compose, systemd/README describe the retired stack; a cold reader deploys the wrong thing. |
| G12 | No automated drift invariant | low | flat-count == sqlite-count == pg-count is a manual shell snippet in an out-of-repo doc, not an automated check. |

## 4. Remediation Roadmap

### Phase 0: Vendor the production truth into git (unblocks everything)

Nothing else is trustworthy until the real production artifacts are versioned. Two tasks, fully parallelizable:

- **P0a (T1):** Vendor the skmem-pg image build: Dockerfile, init SQL, and a dump of the LIVE schema DDL (hybrid_search_docs/memories functions, HNSW and BM25 index definitions) into `deploy/skmem-pg/`. Replace the Qdrant/FalkorDB docker-compose with a skmem-pg compose.
- **P0b (T2):** Vendor the ops scripts: `skmem_reconcile.py`, `skmem-pg-backup.sh`, `skmem-health.sh` into `scripts/` (or `deploy/ops/`) with light tests and no host-specific hardcoding.

Addresses G2, half of G7. No dependencies.

### Phase 1: Make the code honest (config, failover, sync path)

- **P1a (T3):** Unify config: add pgvector/AGE fields to config.py, fix the 5433 vs 5432 default mismatch, single source for the backend default (mcp_server vs openclaw), strip the hardcoded password from the default DSN. Addresses G5 and the in-git half of G9.
- **P1b (T4):** Embed failover and fail-loud: multiple embed URLs with health-probing (extend or reuse endpoint_selector), and NULL-embedding writes counted and surfaced instead of silent. Addresses G3. Parallel with P1a.
- **P1c (T5):** Retarget `cli.py sync` and the systemd sync unit at pgvector/AGE; retire the chroma/FalkorDB sync paths. Addresses G4. Depends on P1a (needs the unified config).

### Phase 2: Redundancy (the mantra) - node self-sufficiency, not replication

- **P2a (T6):** Per-node writable skmem-pg. Each node runs its OWN `localhost:5432` skmem-pg, rebuilt from source (flat JSON via `reconcile.py`, docs via skingest from the git wiki), with agents re-homed off the hardcoded `192.168.0.158` DSN to `localhost`. NO streaming/logical replication and NO failover: the `.158 -> .41` standby was abandoned (ParadeDB Community cannot serve `pg_search` reads in recovery). Redundancy comes from every node having a full, independently-rebuildable copy of a store that is a derived cache, plus the replicated source (flat files + git wiki). Depends on P0a (versioned image/schema so every node stands up an identical pg). Addresses G1.
- **P2b (T7):** Backup-only safety net. Off-box `pg_dump` shipping into the synced tree plus a scripted cold-machine restore (image build, schema, data, hybrid function, indexes), executed once for real with timing recorded. This is a backup, not a live replica: the primary rebuild path is reconcile-from-flat + skingest-from-wiki; the dump is the fast restore and the only off-node copy of the 33k-node AGE graph. Depends on P0a and P0b. Addresses G7. Parallel with P2a.

### Phase 3: Gates and eyes

- **P3a (T8):** CI service container: run the vendored skmem-pg image in GitHub Actions, un-skip pgvector/AGE/hybrid tests. Depends on P0a. Addresses G6. Parallelizable with Phase 2.
- **P3b (T9):** Drift and NULL-embedding observability: in-repo drift detector (flat vs sqlite vs pg counts per agent, NULL-embedding count, embed endpoint probe) wired to sk-alert via a per-node systemd timer. There is no replication-lag check (no replica exists); the per-node flat-vs-pg drift count is the primary signal that a silent rebuild produced a partial table. Depends on P0b (builds on vendored health script). Addresses G8, G12.
- **P3c (T10) - CORE HA MECHANISM:** Multi-agent, multi-node reconcile. This is the HA/DR mechanism for skmem-pg (not replication). Every node schedules the vendored `reconcile.py` for each agent whose flat files it serves (not just lumina), driven by the per-agent systemd template, so `memories` is continuously rebuilt-from-flat on each node's local `localhost:5432` pg. Combined with per-node skingest for `docs`, this makes each node self-sufficient and removes the .158 SPOF. Depends on P0b and P1c. Addresses G1 and G10.

### Phase 4: Lockdown and docs

- **P4a (T11):** Credential rotation: pg password to skvault/capauth-sourced secret, rotate the live password, restrict listen address or add pg_hba scoping. Because skmem-pg is now node-local and agents connect over `localhost`, prefer binding the container to `127.0.0.1` (no LAN listener needed), which closes G9 more directly than pg_hba alone. Depends on P1a (config must read from env/secret first). No replication-auth coordination is required (there is no replica). Addresses the live half of G9.
- **P4b (T12):** Docs overhaul: README, ARCHITECTURE.md, HA.md, systemd/README.md rewritten for the live stack, cold-machine bootstrap guide, MEMORY_STORES content mirrored into the repo. Depends on everything above being true; last. Addresses G11.

Critical path: T1 -> T6 -> T11 -> T12. Widest parallel wave after Phase 0: T3, T4, T6, T7, T8 can all run at once.

## 5. Task List

1. **skmemory: vendor skmem-pg image build (Dockerfile, init SQL, live schema DDL) into repo** (critical) - deps: none
2. **skmemory: vendor production ops scripts (skmem_reconcile.py, skmem-pg-backup.sh, skmem-health.sh) into repo** (critical) - deps: none
3. **skmemory: unify pgvector/AGE config, fix port default mismatch, remove hardcoded default DSN password** (high) - deps: none
4. **skmemory: embed endpoint failover and fail-loud NULL-embedding handling in PGVectorBackend** (high) - deps: none
5. **skmemory: retarget CLI sync and systemd units to pgvector/AGE, retire chroma/FalkorDB sync paths** (high) - deps: 3
6. **skmemory: per-node writable skmem-pg (re-home agent DSN to localhost; no replication)** (critical) - deps: 1
7. **skmemory: restore drill and cold-machine recovery ceremony (documented and scripted)** (high) - deps: 1, 2
8. **skmemory: CI gate for the live pgvector/AGE path via skmem-pg service container** (medium) - deps: 1
9. **skmemory: drift and NULL-embedding observability wired to sk-alert** (medium) - deps: 2
10. **skmemory: extend reconcile to all agents, not just lumina** (medium) - deps: 2, 5
11. **skmemory: move pg credentials to skvault/capauth and rotate the default password** (medium) - deps: 3, 6
12. **skmemory: docs and docker-compose overhaul to describe the live pgvector/AGE stack** (low) - deps: 1, 5, 6, 7
