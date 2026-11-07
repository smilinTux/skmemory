# SKMemory - Standard Operating Procedures

Sovereign, multi-layer, emotionally-aware memory for AI agents. Flat JSON files are
the **source of truth**. There are exactly two storage tiers: a **local** tier (SQLite
`index.db` with an FTS5 lexical arm, always on, offline capable) and a **shared** tier
(skmem-pg: pgvector, pg_search BM25, Apache AGE). ChromaDB, Qdrant and FalkorDB are
retired. Called by SKCapstone agents (Claude Code, Hermes) over the CLI, the Python API,
and the stdio MCP server.

> This SOP is the operational source of truth. Deeper design lives in
> [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) and [`LOCAL-PG-STACK-ARCHITECTURE.md`](./LOCAL-PG-STACK-ARCHITECTURE.md), the fortress/tamper model in
> [`docs/FORTRESS_SOP.md`](./docs/FORTRESS_SOP.md), provenance in
> [`docs/PROVENANCE_AND_CLOSURE_DESIGN.md`](./docs/PROVENANCE_AND_CLOSURE_DESIGN.md),
> and audience gating in [`docs/admission_policy.md`](./docs/admission_policy.md).

---

## 1. Overview

**Purpose.** Give an AI agent a memory that survives context resets: capture each
moment as a *polaroid* (content + emotional fingerprint + provenance + integrity
seal), organise it across three persistence tiers and four semantic quadrants, and
re-hydrate identity ("who was I?") before the first user message.

**What it owns.**
- The per-agent memory store under `~/.skcapstone/agents/$SKAGENT/memory/`.
- The flat-file ⇄ SQLite ⇄ skmem-pg sync topology.
- Reader-scoped search and fusion, the intention store and tick, and the outcome ledger.
- The rehydration ritual (soul blueprint + FEBs + seeds + strongest memories).
- The MCP tool surface consumed by every agent runtime.

**What it explicitly does NOT do.**
- It is **not** the ingestion service for external corpora - that is
  [skingest](https://github.com/smilinTux/skingest). skmemory stores *agent*
  memories; skingest ingests *documents* into the shared `docs` table.
- It does **not** run the embedding model - it calls an Ollama-compatible endpoint
  (`mxbai-embed-large`, 1024-dim).
- It does **not** manage the agent's identity keys - that is
  [capauth](https://github.com/smilinTux/capauth)/sksecurity. skmemory only *uses*
  a GPG key to seal the vaulted backend at rest.

---

## 2. Architecture

```mermaid
flowchart TD
    CLI["skmemory CLI (click)"]
    MCP["skmemory-mcp\n(stdio MCP server, 25 tools)"]
    API["Python API\nMemoryStore"]

    CLI --> Store
    MCP --> Store
    API --> Store

    Store["MemoryStore\n(facade / orchestrator)"]

    Store --> Hooks["Pre-write hook chain\nvalidation.py · schema_validator\n(schema-validated writes)"]
    Hooks --> Flat

    subgraph SoT["Source of truth"]
        Flat["Flat-file store\nshort/ mid/ long-term JSON\n(Syncthing-synced)"]
    end

    subgraph L0["Local tier - index (always on)"]
        SQLite["SQLite index\n(SQLiteBackend · index.db)"]
        Vaulted["VaultedSQLiteBackend\nPGP-sealed at rest"]
    end

    subgraph L1["Shared tier - skmem-pg (optional)"]
        PG["pgvector backend\nskmem-pg · docs+memories · BM25+HNSW"]
    end

    subgraph L2["Shared tier - graph (optional)"]
        Graph["AGEGraphBackend\nApache AGE lineage"]
    end

    Store --> Flat
    Flat -->|rebuildable index| SQLite
    Store --> SQLite
    Store --> Vaulted
    Store --> PG
    Store --> Graph

    Store --> Fortress["FortifiedMemoryStore\ntamper detection + audit log"]
    Store --> Ritual["Ritual\nsoul + FEBs + seeds → rehydration"]
    Store --> KYA["audience.py (KYA)\naudience-aware filtering"]

    subgraph Layers["Promotion ladder"]
        Short["short-term"] -->|promote| Mid["mid-term"] -->|promote| Long["long-term"]
    end
    Store --> Promo["Promotion engine\npromotion.py · PromotionEngine\n+ PromotionScheduler"]
    Promo --> Fresh["FreshContextRunner seam\nfresh_context.py\n(in-process default · SubprocessRunner)"]
    Promo --> Layers

    Store --> MOC["MOC generator\nmoc.py · read-side indexes\nby quadrant + tag cluster (no mutation)"]
    Flat -.read-only.-> MOC
```

**Key invariant:** the flat JSON files are canonical. SQLite, pgvector (skmem-pg),
and the AGE graph are all **derived, node-local projections** - delete any of them and
rebuild from source on that node (`skmemory health`, `skmemory reindex`, `reconcile.py`).
No dual-master. **skmem-pg specifically is per-node and rebuildable, never a remote
primary:** it is LOCAL and per-node (each node runs its own writable pg on
`localhost:5432`), NOT streaming-replicated, NOT a central/shared system of record, and
NOT a SPOF. The `memories` table rebuilds from synced flat JSON via `reconcile.py`; `docs`
rebuilds per-node via skingest from the git wiki. There is no remote primary/replica and
no failover for skmem-pg (a streaming standby was tried and abandoned; ParadeDB
Community cannot serve `pg_search` reads in recovery, prb-6f069c5e).

**Start here.** The five files to read before changing anything:

| File | Why it is the entry point |
|---|---|
| `skmemory/cli.py` | The `skmemory` console script (`skmemory.cli:main`). Every subcommand in §7 is a click command here, including `health` (`def health`, cli.py:809) and `ritual` (cli.py:1684). |
| `skmemory/mcp_server.py` | The `skmemory-mcp` console script (`skmemory.mcp_server:main`). The stdio MCP surface every agent runtime consumes. |
| `skmemory/store.py` | `MemoryStore`, the facade every caller funnels through. The pre-write hook chain, layer promotion and backend fan-out all land here. |
| `skmemory/config.py` + `skmemory/agents.py` | Agent resolution (`SKAGENT` → `SKCAPSTONE_AGENT` → `SKMEMORY_AGENT`) and the per-agent `config/skmemory.yaml` this SOP's §6 paths come from. |
| `skmemory/backends/` | One module per persistence tier (`pgvector_backend.py`, `age_backend.py`, SQLite, vaulted). Where a DSN or embed default drifts, it drifts here. |

**Endpoints / paths.**
- Embed endpoint: Ollama-compatible, `mxbai-embed-large`, default
  `http://localhost:11434/api/embed` (`SKMEMORY_EMBED_URL`, with an ordered failover list
  in `SKMEMORY_EMBED_URLS`).
- skmem-pg DSN: node-local, default `localhost:5432` (the local skmem-pg container;
  fleet-wide uniform port, per-node override `SKMEMORY_PG_DSN`). Agents connect only to
  `localhost`, never a remote host. An unreachable local pg is an operational fault that
  should surface loudly (or degrade to the always-on SQLite recency read path), not
  silently answer from an empty retired vector store.
- Per-agent home: `~/.skcapstone/agents/$SKAGENT/` (`SKAGENT`, `SKCAPSTONE_AGENT`,
  `SKMEMORY_AGENT` must agree; then `SK_DEFAULT_AGENT`; then the sole installed agent;
  otherwise it refuses. There is no implicit default).

---

## 3. Build

```bash
git clone https://github.com/smilinTux/skmemory.git
cd skmemory
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"      # all = pg, telegram, seed extras
```

**Package layout:** the importable package is the top-level `skmemory/` directory.
There is **no `src/` layout** in this repo; CI lints `skmemory/ tests/`
(`.github/workflows/ci.yml`), and `[tool.setuptools.packages.find] include = ["skmemory*"]`
in `pyproject.toml` is what selects it.

Artifacts: Python wheel/sdist (`skmemory` on PyPI) + an npm wrapper
(`@smilintux/skmemory`). Both are built and published by the single
`.github/workflows/publish.yml` (jobs `build`, `pypi-publish`, `publish-npm`).
There is no separate `npm-publish.yml`.

---

## 4. Test

```bash
pytest                            # unit + backend tests (tests/)
ruff format --check skmemory/ tests/
ruff check skmemory/ tests/
skmemory health                   # end-to-end: flat ⇄ index ⇄ vector liveness
```

**Green-bar gate (blocks release).** The gate is what CI actually runs, and both
workflows below fail the build on a red test (no `|| true` anywhere in the test path):

| Workflow | Trigger | Command |
|---|---|---|
| `.github/workflows/ci.yml` (job `test`) | push to `main`, PR to `main`; py3.11 + py3.12 | `python -m pytest tests/ -v --tb=short --cov=skmemory --cov-report=xml --cov-report=term-missing` |
| `.github/workflows/ci.yml` (job `lint`) | same | `ruff format --check skmemory/ tests/` then `ruff check skmemory/ tests/` |
| `.github/workflows/pytest.yml` | push/PR touching the package | `python -m pytest tests/ -k "not test_sharing" -v --tb=short` |

`pytest.yml` deliberately deselects the pgpy-sharing tests (unstable across CPython
versions); neither workflow has a Postgres or an embed endpoint, so every test is
hermetic (live-database tests skip themselves). `ci.yml` runs the full `tests/` tree with
coverage. `tests/test_skill_md.py` is the docs gate: it runs `--help` on every command
`SKILL.md` names and checks the paths and modules the public docs name. Locally, add
`skmemory health`: any LIVE/backend claim in README/CHANGELOG must be reproducible from
that output.

---

## 5. Release / Deploy

Library release (PyPI + npm).

**The git tag IS the version. Do not hand-edit a version number.** `pyproject.toml`
declares `dynamic = ["version"]` and `[tool.setuptools_scm]` derives it from the newest
tag matching `^v(\d+\.\d+\.\d+)$` (`tag_regex` is pinned precisely because these repos
also carry non-SemVer tags like `swarm-20260717`, which setuptools-scm would otherwise
pick and turn into a nonsense version). The `version` committed in `package.json` is
**not** authoritative: the `publish-npm` job overwrites it from the tag with
`npm version "${GITHUB_REF#refs/tags/v}" --no-git-tag-version` before publishing.

1. Add a dated `CHANGELOG.md` entry (Keep-a-Changelog + SemVer).
2. Run the §4 gate.
3. Merge to `main`. The `tag` job in `publish.yml` cuts the next patch tag
   automatically when HEAD is not already tagged, ranking **all** `v*` tags by version
   (`sort -V`), never `git describe`, so a release can never go backwards.
4. To release a minor/major instead, push the tag yourself:
   `git tag vX.Y.0 && git push origin vX.Y.0`.
5. The tag push runs `build` -> `pypi-publish` (OIDC trusted publishing, no token) and
   `publish-npm`. The full test suite is intentionally **not** a gate in `publish.yml`;
   that gate lives in `ci.yml` on PRs.
6. Verify the published version installs and `skmemory health` is green.

Service deploy (per-agent maintenance timers, units in `systemd/`):

| Unit | Timer | Base `ExecStart` in this repo | **Effective** `ExecStart` on a fleet node |
|---|---|---|---|
| `skmemory-sync@<agent>.service` | `OnBootSec=5min`, then `OnUnitActiveSec=6h` | `%h/.skenv/bin/skmemory sync --quiet --vector --graph` | the same command, wrapped by the node's `sk-cron-run` script |
| `skmemory-fortress-verify@<agent>.service` | `OnCalendar=*-*-* 03:00:00`, `RandomizedDelaySec=5min` | `scripts/fortress-verify.sh` from the checkout | the same command, wrapped by the node's `sk-cron-run` script |
| intentions tick (example units in `deploy/systemd/`, not installed for you) | every 5 minutes | `skmemory intentions tick` | the same command, wrapped; set `SKMEMORY_TICK_HOST` so exactly one host delivers |

⚠️ **Both units have their `ExecStart` REWRITTEN on live nodes.** skos installs a
`sk-cron-run.conf` drop-in (`~/.config/systemd/user/<unit>.d/sk-cron-run.conf`) that
clears `ExecStart=` and re-declares it wrapped in `sk-cron-run.sh`, so a failure produces
a run-ledger record, a GTD item and an sk-alert instead of silence. The bare `ExecStart=`
in the drop-in is required: `ExecStart` is list-typed, so without it systemd would append
and the job would run twice. **Never read the unit file to learn what runs.** Read the
effective command:

```bash
systemctl --user show skmemory-sync@<agent>.service -p ExecStart
systemctl --user cat  skmemory-sync@<agent>.service   # unit + every drop-in
```

### Node operations (vendored ops scripts)

The three scripts that keep a skmemory node alive are vendored in-repo (coord
`ce559215`) so any node is rebuildable from source. Previously they lived only on
one host outside any git repo, so losing that host destroyed the only copy. All
host-specifics (agent, DSN, container, backup dir, embed URL, alert lib) are
env-parametrized. Install as
daily cron entries / systemd timers per node, per agent. Full env contract, secrets
note, and an example crontab live in
[`deploy/ops/README.md`](./deploy/ops/README.md).

| Script | Purpose | Env contract (key vars) | Schedule |
|---|---|---|---|
| `skmemory/reconcile.py` (canonical; `deploy/skmem-pg/skmem_reconcile.py` and `scripts/skmem_reconcile.py` are thin shims into it) | Idempotent flat↔pg reconcile: backfill missing memories (embed + upsert, with a stub/shrink guard and the shared-id convergence fix), prune pg rows whose flat file is gone (guarded), re-embed NULL-vector rows. Rebuilds the derived `memories` cache from the Syncthing-synced flat JSON source of truth. `--dry-run` reports every count with zero writes. Invariant covered by `tests/test_reconcile_invariant.py`, `tests/test_reconcile_stub_and_shared_id_guard.py`, `tests/test_reconcile_dry_run.py`. | `SKAGENT`/argv[1] (`lumina`), `--dry-run`, `--force-stubs`, `EMBED_URL`, `EMBED_MODEL` (`mxbai-embed-large`) | daily |
| [`deploy/ops/skmem-pg-backup.sh`](./deploy/ops/skmem-pg-backup.sh) | Daily `pg_dump -Fc` of the node-local skmem-pg container to the agent's `backups/` dir; retains the newest N. Fast-recovery path (seconds vs full re-embed) complementing the rebuild-from-source guarantee. | `SKAGENT` (`lumina`), `SKMEM_BACKUP_DIR`, `SKMEM_PG_CONTAINER` (`skmem-pg`), `SKMEM_PG_USER` (`postgres`), `SKMEM_PG_DB` (`skmemory`), `SKMEM_BACKUP_RETAIN` (`14`) | daily |
| [`deploy/ops/skmem-health.sh`](./deploy/ops/skmem-health.sh) | Deterministic full-stack health probe (flat writes, SQLite index + functional query, skmem-pg reachability + functional vector/hybrid retrieval, backups lineage, skwhisper). Prints a `[PASS]/[WARN]/[FAIL]` digest, archives a dated report under `logs/skmem-health/`, persists run-over-run state, and fires `sk_alert` (deduped, 6h TTL) on WARN/FAIL. No LLM decides "healthy". | `SKAGENT` (`lumina`), `SKMEMORY_HEALTH_DSN` (node-local `localhost:5432`), `SKMEM_EMBED_URL`, `SKMEM_EMBED_MODEL`, `SKMEM_BACKUP_ROOT`, `SKALERT_LIB` (optional) | daily |

**Secrets note.** No secret values are committed. The one credential-shaped default,
`postgresql://postgres:skmemory@localhost:5432/skmemory`, is the node-local skmem-pg
dev password already used throughout the repo (see `tests/test_reconcile_invariant.py`,
`skmemory/backends/*`); it is not a production secret. Override it per node with
`SKMEMORY_HEALTH_DSN`. See [`deploy/skmem-pg/README.md`](./deploy/skmem-pg/README.md)
for the skmem-pg image build (schema + extensions) and
[SECURITY.md](./SECURITY.md) for the secret-handling rules.

### Front-end / Exposure

Per [sk-standards `UNIFIED_INGRESS_STANDARD.md`](https://github.com/smilinTux/sk-standards/blob/main/standards/UNIFIED_INGRESS_STANDARD.md):

**N/A - no network surface.** skmemory is a CLI / library + MCP (stdio) over **local
stores** (flat JSON files, SQLite `index.db`) and reaches its **node-local** `skmem-pg`
Postgres as a *client* over `localhost` (each node runs its own writable pg on
`localhost:5432`; no cross-host DB dependency). It exposes no public `:443` route and
binds no inbound listener; the `skmemory-sync@<agent>` timer is a local file/vector
reconciler, not a server.

---

## 6. Configuration / Usage

- Agent resolution: `SKAGENT` (preferred) → `SKCAPSTONE_AGENT` → `SKMEMORY_AGENT` →
  an explicitly configured `SK_DEFAULT_AGENT` → the sole installed agent. Multiple
  installed agents with no explicit selection are ambiguous and must fail rather than
  choosing a named identity.
- Vector backend defaults: pgvector when `skmem-pg` is reachable; otherwise the SQLite FTS5 local arm.

**Coding-agent MCP registration.** The shared registrar detects Codex and Pi and writes
absolute `~/.skenv/bin/skmemory-mcp` commands so stdio startup does not depend on the
client's inherited `PATH`. Pi reads the eager server entry from
`~/.pi/agent/mcp.json` through `pi-mcp-extension`. SKWhisper is intentionally not a
second default MCP: it produces context in the background, which SKMemory and the SK
context ritual consume.

**Where state lives.**

| What | Path | Notes |
|---|---|---|
| Per-agent home | `~/.skcapstone/agents/<agent>/` | `SKMEMORY_HOME` root; `agents.py` |
| Flat-file store (**canonical**) | `<home>/memory/{short-term,mid-term,long-term}/` | JSON, Syncthing-synced |
| SQLite index (derived) | `<home>/memory/index.db` | rebuildable; `skmemory reindex` |
| Per-agent config | `~/.skcapstone/agents/<agent>/config/skmemory.yaml` | `config.py`, `agents.py` |

**Postgres (skmem-pg) connection.** The code resolves the DSN from the **`SKMEMORY_PG_DSN`
environment variable only**, falling back to the node-local default
`postgresql://postgres:<pw>@localhost:5432/skmemory`
(`skmemory/backends/pgvector_backend.py:53`, `skmemory/backends/age_backend.py:73`).
skmemory itself reads no `pg.env` file; the environment is populated from files by the
node, per [SKMEMORY_NODE_CONFIG_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/SKMEMORY_NODE_CONFIG_STANDARD.md):

- `~/.config/skmemory/pg.env` (dir 700, file 600) is the **single owner** of the stack
  config: `SKMEMORY_VECTOR_BACKEND`, `SKMEMORY_PG_DSN` (with the password),
  `SKMEMORY_EMBED_URL`, `SKMEMORY_EMBED_URLS`, `SKMEMORY_EMBED_MODEL`.
- `~/.config/environment.d/skmemory.conf` is a **symlink** to it, so `systemd --user`
  services inherit it with no second copy of the secret. Verify with
  `/usr/lib/systemd/user-environment-generators/30-systemd-environment-d-generator | grep -c '^SKMEMORY_PG_DSN='` (expect 1).
- Shells derive the same file with the `_sk_derive` block in `~/.bashrc`.
- Rotating the password is one edit to `pg.env` **plus restarting every long-lived
  consumer**: a running process keeps the environment it started with. A rotation that
  skips the restart leaves processes on the old secret with `vector_ok: null` and
  keyword-only search, with no error.

An earlier revision of this SOP said skmemory "reads no `pg.env`, and the file carries no
DSN". That described a node before the standard; on a conforming node `pg.env` is the
owning file and carries the DSN.

**Active agent.** Declared in `~/.config/environment.d/10-skagent.conf`
(`SKAGENT`, `SKCAPSTONE_AGENT`, `SKMEMORY_AGENT`, `SK_DEFAULT_AGENT`). `get_active_agent()`
returns `None` and refuses when several agents are installed and nothing is declared.
Treat `base_path` ending in `-template`, or `vector_ok: null`, in `skmemory health` as a
failure, not as healthy.

Install the `pg` extra on skmem-pg clients: `pip install "skmemory[pg]"`. The
flat-to-Postgres reconcile engine selects a psycopg transport whenever
`SKMEMORY_PG_DSN` is present. The transport inherits the protected environment,
does not put the DSN in argv or logs, and requires no Docker socket access. SQL
transport failures are fatal; reconcile must never reinterpret a connection
failure as an empty Postgres cache.

AGE projection snapshots flat records by file name. If Syncthing or promotion
moves a record between tiers during projection, the projector resolves the
current tier and indexes it once. A record deleted after enumeration is no
longer canonical source truth and is skipped without a false graph error.

**Invalid memory IDs.** A memory ID must be a non-empty string. Model creation,
promotion, primary storage, pgvector, and AGE all reject empty or null
IDs before issuing a write. Reconcile and AGE projection quarantine an existing
active-tier `.json` file, or a payload carrying an explicit empty ID, under
`memory/quarantine/invalid-memory-id/<sha256>.json`. The adjacent `report.json`
is deterministic and records only the payload hash, original relative path,
reason, and quarantine path; it never copies memory content into the report.
Quarantine is recoverable evidence, not canonical memory. Inspect the hash and
source path, repair through a normal validated import if the payload is worth
retaining, and never rename the quarantined payload directly into an active
tier.

- **Secret handling:** never inline a live secret. The vaulted backend seals memory
  at rest to the agent's GPG key (`vault.py`); the passphrase is supplied via
  gpg-agent, never stored in the repo or config. See [SECURITY.md](./SECURITY.md).

```bash
skmemory snapshot "title" "content" --layer long-term --emotions joy --intensity 8
skmemory search "cloud9 seeds"        # fused arms: FTS5 always, plus pgvector and BM25 when skmem-pg is up
skmemory ritual                        # rehydrate identity
skmemory context --max-tokens 3000     # token-budgeted load for a new session
```

---

## 7. API / Reference

- **CLI:** `skmemory {snapshot,search,history,context,ritual,promote,sweep,sync,health,intentions,ledger,moc,songs,...}` (`skmemory --help` is authoritative; `tests/test_skill_md.py` checks every command `SKILL.md` names).
  - `skmemory moc` - auto-generate **Maps of Content**: read-side index documents
    grouping memories **by quadrant** (Core/Work/Soul/Wild) and **by tag cluster**.
    Deterministic (byte-identical Markdown per input) + bounded; renders to stdout or
    writes one `<key>.md` per index with `--out DIR`. Flags:
    `--kind {all,quadrants,tags}`, `--limit`, `--min-cluster-size`, `--max-clusters`,
    `--max-entries`. Pure aggregation - never mutates the store (`moc.py`).
- **MCP server:** `skmemory-mcp` (stdio) exposes 25 tools: the memory CRUD and search set
  (`memory_store`, `memory_search`, `memory_auto_context`, `memory_recall`,
  `memory_history`, `memory_list`, `memory_forget`, `memory_promote`, `memory_context`,
  `memory_health`, `memory_graph`, ...), the maintenance set (`memory_consolidate`,
  `memory_export`, `memory_import`, `memory_verify`, `memory_audit`,
  `memory_synthesize_*`, `memory_save_session`, `memory_pre_compact`, `memory_extract`)
  and the owner-only intention tools (`memory_remind`, `memory_intentions`,
  `memory_intention_done`). Each is classed filtered, owner-only or exempt by
  `TOOL_READER_POLICY` in `skmemory/mcp_server.py`.
- **Python:** `from skmemory import MemoryStore` - `store.snapshot()`, `store.search()`,
  `store.recall()`, `store.promote()`, `store.history()`, `store.load_context()`.
  - **Schema-validated writes (pre-write hooks):** every write runs a
    `(Memory) -> None` pre-write hook chain at the write boundary *before* any backend
    is touched (`validation.py`). The default `schema_validator` round-trips each memory
    (`model_dump → model_validate`) so fields mutated after construction are re-checked
    against the canonical `Memory` schema; malformed writes raise `SchemaValidationError`
    (subclasses `ValueError`, so existing `except ValueError`/WAL paths still work).
    Register more via `MemoryStore.register_pre_write_hook(...)`.
  - **Fresh-context runner seam:** `PromotionEngine.run_pass()` /
    `PromotionScheduler` / `skmemory sweep` route long consolidation/promotion sweeps
    through an injectable `FreshContextRunner` (`fresh_context.py`) so a chatty
    maintenance pass can run in an isolated context (spawned subagent/subprocess) instead
    of polluting the live agent's working window. Default `in_process_runner` is the
    identity element (behavior unchanged); `SubprocessRunner(spawn)` is the spawn seam.

Full tool/flag reference: [README.md](./README.md) §MCP Tools and §Usage, and
[SKILL.md](./SKILL.md).

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `search` returns nothing / falls back to SQLite | Is the embed endpoint up? `curl localhost:11434/api/embed`. Is skmem-pg reachable? `skmemory health`. |
| Vector results look domain-wrong | Confirm the query is embedded with **mxbai** (1024-dim), not a stale bge endpoint. |
| Rehydration ("ritual") empty | Soul blueprint missing at `~/.skcapstone/agents/$SKAGENT/soul/base.json`; seeds not imported (`skmemory import-seeds`). |
| Index drifts from flat files | Rebuild the index from the canonical flat files; check `skmemory-sync@<agent>` timer. |
| Vaulted backend can't read | gpg-agent not holding the passphrase; unlock then retry. |
| Wrong agent's memories | `SKAGENT`/`SKCAPSTONE_AGENT` not set as expected - echo it before launch. |
| `health` says `degraded` | Read `retrieval.arms`: an arm that errors or times out (latest attempt, or 10 percent of at least 5) degrades; a config-disabled arm (graph by default) never does. Details per search are in `<agent>/logs/retrieval.jsonl`. |
| A search is slow or an arm shows `timeout` | The arms share one deadline (`SKMEMORY_SEARCH_DEADLINE_S`, default 4). A timed-out arm cools down for 10 s. Check the embed endpoint and skmem-pg. |
| `health` reports a large WAL | `index.db-wal` over 256 MB degrades health. The bound is applied on every connection; a long-lived reader can pin it, so restart the process. |
| A non-owner reader sees "owner-only" | Intended: `SKMEMORY_READER` is a ceiling. Export, import, verify, audit, synthesis and the intention tools are owner only. |
| A reminder never fired | Only the host named by `SKMEMORY_TICK_HOST` delivers (`skmemory intentions tick`). Confirm the timer runs there and `sk-alert` is on its PATH; an undelivered intention stays due and retries. |
| `SKMEMORY_VECTOR_BACKEND` fails with "retired" | ChromaDB, Qdrant and FalkorDB are gone. Use `pgvector` or leave it unset (local tier only). |
| Existing memories missing from lexical search | Finish the FTS5 backfill: `skmemory fts-backfill`. |

---

## 9. Maturity-tier + Version reference

- **Crypto maturity tier: T0 - Classical.** skmemory stores key material only in the
  sense of **sealing memory at rest** via the operator's GPG key
  (`VaultedSQLiteBackend`, `vault.py`) - classical OpenPGP (typically Ed25519/RSA +
  AES-256). It performs **no** key exchange, KEM, or signature negotiation of its
  own, so the agile/hybrid tiers (T1-T4) do not yet apply. A **hybrid PQ posture**
  (`HKDF(X25519 ‖ ML-KEM-768)`, FIPS 203) is **not** integrated today; the migration
  path is to seal via [sk_pgp](https://github.com/smilinTux/sk-pgp)/sk_pqc when the
  PGP→PQC root cutover lands. **No claim of post-quantum protection is made here.**
- **VERSION_LIFECYCLE phase:** Active (v2). **SemVer:** there is **no version number to
  quote here, and none in `pyproject.toml`** (it declares `dynamic = ["version"]`). The
  version is derived by setuptools-scm from the newest git tag matching
  `^v(\d+\.\d+\.\d+)$`; ask the tree, not the docs:
  `git describe --tags --match 'v[0-9]*'`, or `pip show skmemory` for what is installed.
  The `version` field committed in `package.json` is stale by design and is overwritten
  from the tag at npm-publish time. History is in [CHANGELOG.md](./CHANGELOG.md). The MOC generator,
  schema-validated writes, and fresh-context runner (2026-07-03) are **additive,
  backward-compatible** - defaults preserve prior store/backend semantics.
- **Self-report / evidence:** `skmemory health` reports the live backends; the
  vaulted/fortress state is reported via the fortress audit log
  ([`docs/FORTRESS_SOP.md`](./docs/FORTRESS_SOP.md)). Every backend/LIVE claim in the
  docs is reproducible from that output (honest-claims gate, see
  [SECURITY.md](./SECURITY.md)).

---

<!-- docs-evidence
verified: 2026-11-07
checks:
  - name: SKILL.md commands and named paths are machine-checked against the code
    run: test -f tests/test_skill_md.py
  - name: three console-script entry points unchanged (skmemory, skmemory-mcp, skmemory-post-install)
    run: grep -qxF 'skmemory = "skmemory.cli:main"' pyproject.toml && grep -qxF 'skmemory-mcp = "skmemory.mcp_server:main"' pyproject.toml && grep -qxF 'skmemory-post-install = "skmemory.post_install:main"' pyproject.toml
  - name: package layout is top-level skmemory/, not src/, and CI lints that path
    run: test -d skmemory && test ! -d src && grep -qxF '        run: ruff check skmemory/ tests/' .github/workflows/ci.yml
  - name: pg DSN comes from SKMEMORY_PG_DSN with a node-local localhost:5432 default in both pg backends
    run: grep -qE '^\s+"SKMEMORY_PG_DSN", "postgresql://[^"]+@localhost:5432/skmemory"$' skmemory/backends/pgvector_backend.py && grep -qE '^\s+"SKMEMORY_PG_DSN", "postgresql://[^"]+@localhost:5432/skmemory"$' skmemory/backends/age_backend.py
  - name: no code path reads a pg.env file (SOP section 6 documents this absence)
    run: ! grep -rqF 'pg.env' skmemory/
  - name: version is setuptools-scm derived from the git tag, never hardcoded in pyproject.toml
    run: grep -qxF 'dynamic = ["version"]' pyproject.toml && grep -qxF 'tag_regex = "^v(?P<version>[0-9]+\\.[0-9]+\\.[0-9]+)$"' pyproject.toml && ! grep -qE '^version *=' pyproject.toml
  - name: systemd base ExecStart matches the command documented in section 5
    run: grep -qxF 'ExecStart=%h/.skenv/bin/skmemory sync --quiet --vector --graph' systemd/skmemory-sync@.service && grep -qxF 'ExecStart=%h/clawd/skcapstone-repos/skmemory/scripts/fortress-verify.sh' systemd/skmemory-fortress-verify@.service
  - name: skmemory health self-report command still exists
    run: grep -qE '^def health\(ctx' skmemory/cli.py
  - name: mxbai-embed-large at 1024 dims is still the documented embed default
    run: grep -qxF 'DEFAULT_EMBED_MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")' skmemory/backends/pgvector_backend.py && grep -qxF 'VECTOR_DIM = 1024' skmemory/backends/pgvector_backend.py
  - name: Codex and Pi MCP registration resolve commands from the shared skenv
    run: grep -qF 'envs.append("pi")' skmemory/register.py && grep -qF 'return str(candidate) if candidate.is_file() else command' skmemory/register.py && grep -qF '"pi": home / ".pi" / "agent" / "mcp.json"' skmemory/register.py
-->
