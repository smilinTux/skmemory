# SKCapstone Changelog

*Auto-generated from the coordination board - 2026-02-24 07:12 UTC*

**Total completed: 87** across 8 agents

## [Unreleased]

### Fixed

- **SOP correction: the pg connection never came from `~/.config/skmemory/pg.env`.**
  SOP §6 claimed it did. Nothing in `skmemory/` reads that file, and on the current
  fleet it no longer carries a DSN at all (only `SKMEMORY_VECTOR_BACKEND`,
  `SKMEMORY_EMBED_URL`, `SKMEMORY_EMBED_MODEL`). The code resolves the DSN from the
  `SKMEMORY_PG_DSN` env var only (`backends/pgvector_backend.py:53`,
  `backends/age_backend.py:73`); on fleet nodes that var is exported by
  `~/.config/environment.d/skmemory.conf`, which is host state outside this repo.
- **SOP correction: there is no version to bump.** §5 said to bump `version` in
  `pyproject.toml`; `pyproject.toml` declares `dynamic = ["version"]` and
  setuptools-scm derives it from the git tag. §9 quoted a stale `0.10.4`. Both now say
  where the version comes from instead of quoting one.
- **SOP correction: `npm-publish.yml` does not exist.** Both PyPI and npm are published
  by the single `publish.yml`.
- **SOP correction: the systemd units do not run what their unit files say.** A skos
  `sk-cron-run.conf` drop-in clears and re-declares `ExecStart` for both
  `skmemory-sync@` and `skmemory-fortress-verify@`. §5 now documents the effective
  command and how to read it (`systemctl --user show ... -p ExecStart`).

### Added

- **`docs-evidence` block + `docs-check` CI gate.** SOP.md now ends with an executable
  evidence block (8 hermetic, repo-local checks pinning the console-script entry points,
  the non-`src/` package layout, the `SKMEMORY_PG_DSN` / `localhost:5432` default, the
  *absence* of any `pg.env` read, setuptools-scm versioning, the systemd base
  `ExecStart` strings, `skmemory health`, and the mxbai-embed-large/1024-dim embed
  default). `.github/workflows/docs-check.yml` runs tiers 1 and 2 on every push and PR.
- SOP §2 gained a "Start here" entry-point table; §4 now names the exact CI commands
  that form the green-bar gate.

- **`deploy/ops/` production ops scripts (coord `ce559215`).** Vendored the two
  scripts that keep a skmemory node alive but previously lived only on `.158`
  outside any repo (`~/.skcapstone/scripts/`, `~/.hermes/scripts/`), so losing that
  host destroyed the only copy. Both are fully env-parametrized; the defaults
  reproduce the original `.158` behavior so each runs unchanged there.
  - `skmem-pg-backup.sh` - daily `pg_dump -Fc` of the node-local skmem-pg container
    to the agent's `backups/` dir, retaining the newest N (default 14,
    `SKMEM_BACKUP_RETAIN`). skmem-pg is a rebuildable derived cache, so the dump is a
    fast-recovery path (seconds vs a full re-embed), not the system of record.
  - `skmem-health.sh` - deterministic full-stack health probe (flat-file writes,
    SQLite index freshness + WAL, skmem-pg reachability + functional vector/hybrid
    retrieval, backups lineage, skwhisper ingestion). Emits a
    `[PASS]/[WARN]/[FAIL]` digest to stdout, archives a dated report under
    `logs/skmem-health/`, persists run-over-run state, and fires `sk_alert`
    (deduped, 6h TTL) on WARN/FAIL when the optional alert lib is present. No LLM
    decides "healthy".
  - `deploy/ops/README.md` - purpose, per-script env contract, secrets note, and an
    example daily crontab; cross-links `deploy/skmem-pg/` reconcile tooling.

### Changed

- **License metadata made consistent (`__license__` AGPL -> GPL-3.0-or-later,
  coord `cd1ae924`).** `skmemory/__init__.py` was the lone outlier declaring
  AGPL-3.0 while `LICENSE` (GNU GPL v3), `pyproject.toml`
  (`license = {text = "GPL-3.0-or-later"}` + GPLv3+ classifier), and the README
  badge/footer all say GPL. `LICENSE` is the source of truth; corrected the outlier
  so every declaration agrees. No relicensing - metadata only.
- **ruff `check` + `format` now clean across `skmemory/` + `tests/` (coord
  `296da789`).** The CI lint job was red: 151 lint errors and 73 files needing
  formatting. Auto-fixed the safe import/annotation modernizations (I001/UP/F401)
  and applied the remaining SIM/B905/UP031/E402/E741/F821 fixes by hand, then ran
  `ruff format`. Both `ruff check` and `ruff format --check` now exit 0, so the CI
  lint gate is green. Changes are whitespace/style + safe refactors; no runtime
  semantics changed.

### Fixed

- **Duplicate test-method shadow (`test_proud`).** The ruff sweep surfaced a real
  F811 bug: `tests/test_extractor.py` defined `test_proud` twice, so the second
  definition silently shadowed the first and one assertion never ran. Renamed the
  duplicate to `test_proud_circle` so both tests execute.

## [0.11.3] - 2026-07-12

### [CHANGED] skmem-pg is local-per-node rebuild-from-source, not replicated-central (prb-6f069c5e)
- Corrected the topology across docs and code: **skmem-pg is LOCAL, per-node, and
  rebuildable from source. It is NOT streaming-replicated, NOT a central/shared system of
  record, and NOT a SPOF.** Each node runs its OWN writable skmem-pg on `localhost:5432`
  (fleet-wide uniform port, env-free; per-node override `SKMEMORY_PG_DSN`); agents connect
  only to `localhost`.
- The `memories` table is a DERIVED cache (same class as `index.db`): rebuilt from the
  Syncthing-synced flat JSON by `reconcile.py` (idempotent, agent-scoped). Embeddings are a
  deterministic function of flat content + mxbai on .100, so any node regenerates them
  locally. `docs`/`file_locations` is wiki-canon rebuilt per-node by skingest.
- HA/DR = node self-sufficiency + rebuild-from-source (flat files + git wiki, both
  replicated) + the daily `pg_dump` backup in the synced tree. **There is no
  primary/replica/failover for skmem-pg.** Any primary/replica wording is scoped to the
  retired SKVector(Qdrant)/SKGraph(FalkorDB) recall endpoints only.
- Background: streaming replication (`.158 -> .41` standby on `:5433`) was abandoned -
  ParadeDB Community cannot serve `pg_search` reads in recovery, so the standby broke,
  bloated primary WAL, and made .41 depend on .158.
- **Default DSN fix:** node-local skmem-pg DSN standardized on `localhost:5432` (the retired
  `:5433` was the abandoned standby port); `age_backend.py` and `reconcile.py` aligned on
  the same node-local port/DB.
- Vendored the production reconcile engine into the repo as `skmemory/reconcile.py` (was
  out-of-repo `~/skmem-build/skmem_reconcile.py`) and added `tests/test_reconcile_invariant.py`
  asserting rebuild-from-empty backfills+embeds every flat memory, prunes gone rows,
  `flat_count == pg_count` per agent, and is idempotent.
- Docs updated: `docs/ARCHITECTURE.md`, `README.md`, `skmemory/README.md`, `SOP.md`,
  `skmemory/HA.md`, and `docs/deploy-plan/skmemory-bulletproof-deploy.md`.

## [0.11.1] - 2026-07-10

### [DOCS] Architecture corrected to the live two-layer design
- `ARCHITECTURE.md`: **SQLite** (relational/recency - the CLI read path + skwhisper's
  recency feed) + **skmem-pg** (semantic + graph: pgvector + pg_search BM25 + Apache AGE).
- Marked **ChromaDB / SKVector / FalkorDB** retired as defaults (still pluggable).
- Added a CURRENT-ARCHITECTURE callout and a link to the store-location map
  `~/.skcapstone/docs/MEMORY_STORES.md`.
- Companion: skwhisper v0.6.0 now reads both layers (recency from SQLite, semantic from
  skmem-pg); the `hybrid_search_memories()` "unsupported query shape" bug was fixed and
  the flat↔pg store drift reconciled (100% embedded).

## [0.11.0] - 2026-07-03

### [ADDED] Maps of Content, schema-validated writes, fresh-context runner (2026-07-03)

Three additive, backward-compatible capabilities (round-2 merges) - no change to
existing store/backend behavior; defaults preserve prior semantics.

- **Maps of Content (MOC) indexes + `skmemory moc` CLI.** New `skmemory/moc.py`
  builds read-side index documents ("Maps of Content") over a memory collection,
  grouped two ways: **by quadrant** (Core / Work / Soul / Wild) and **by tag
  cluster** (one MOC per tag shared by ≥ N memories). Output is deterministic
  (stable sort keys, byte-identical Markdown for the same input) and bounded
  (caps on entries-per-section and cluster count) so a large store can't blow up
  index generation. Pure aggregation - never mutates or writes back to the store.
  The `skmemory moc` command renders the MOCs to stdout or writes one
  `<key>.md` file per index with `--out DIR`; `--kind {all,quadrants,tags}`,
  `--limit`, `--min-cluster-size`, `--max-clusters`, and `--max-entries` tune it.
- **Schema-validated writes via pluggable pre-write hooks.** New
  `skmemory/validation.py` adds a `(Memory) -> None` **pre-write hook** chain that
  runs at the write boundary *before* any backend is touched. The default hook
  (`schema_validator`) round-trips each memory through `model_dump → model_validate`
  so fields mutated after construction (e.g. via `model_construct` or direct
  attribute assignment) are re-checked against the canonical `Memory` schema;
  malformed writes are rejected with a `SchemaValidationError` (subclasses
  `ValueError`, so existing `except ValueError` / WAL failure paths keep working)
  naming the offending fields. Register additional hooks via
  `MemoryStore.register_pre_write_hook(...)`; the default chain is installed on
  every new store.
- **Fresh-context runner seam for consolidation/promotion.** New
  `skmemory/fresh_context.py` adds an injectable `FreshContextRunner` seam so a
  long, chatty maintenance pass (consolidation / promotion sweep) can run in an
  **isolated context** (spawned subagent/subprocess) instead of polluting the
  live agent's working context window. `PromotionEngine.run_pass()` routes the
  sweep through the runner; the default `in_process_runner` runs in-process with
  no isolation (identity element - behavior identical to before), and
  `SubprocessRunner(spawn)` is the scaffold for real subagent/subprocess spawning
  with the spawn mechanism itself injected. `PromotionScheduler` accepts the same
  optional `runner`. `skmemory sweep` (one-shot) now routes through the seam.

Verified: full suite green - **988 passed, 76 skipped** (`pytest tests/ -q`), incl.
`tests/test_moc.py`, `tests/test_validation.py`, `tests/test_fresh_context.py`.

### [ADDED] sk-standards doc set (SOP / SECURITY / CONTRIBUTING / CODE_OF_CONDUCT)

Brought the repo up to the canonical
[SK_REPO_DOC_STANDARD](https://github.com/smilinTux/sk-standards) bar (2026-06-28):

- `SOP.md` - 9-section operational source of truth with a mermaid Architecture
  diagram (source-of-truth flat files → SQLite index → Chroma/pgvector/graph
  projections), build/test/deploy, troubleshooting table, and a maturity-tier
  reference (**T0 - Classical**: at-rest GPG sealing only; no hybrid PQ KEM today).
- `SECURITY.md` - threat model, secret-handling hard rules, reporting channel, and an
  **honest crypto-posture** statement (no post-quantum claim; FIPS 203 cited for the
  future sk_pgp/sk_pqc sealing path).
- `CONTRIBUTING.md` - branch/commit (`Co-Authored-By` trailer)/test-gate/review path;
  additive-and-gated rule for LIVE paths; TDD-where-there's-logic.
- `CODE_OF_CONDUCT.md` - Contributor Covenant 2.1.
- `README.md` - added a standard-conformant **Related projects / See also** section
  (Depends on / Used by / Siblings / Standards cross-links) and a doc-set footer.

Documentation-only; no code or backend behavior changed.

### [CHANGED] Embedding cutover: bge-legal → mxbai-embed-large; pgvector localhost default

Cut the sovereign vector stack over from the bge-legal embedders to
`mxbai-embed-large` (1024-dim, drop-in - same vector width, no schema change)
and removed every `bge-legal-v1`/`bge-legal-v2` reference from the codebase,
configs, docs, and tests.

- `backends/pgvector_backend.py`: defaults now `mxbai-embed-large` via local
  Ollama (`http://localhost:11434/api/embed`) and `DSN=localhost:5433` so the
  backend works out-of-the-box on any host running the skmem-pg container.
  `_embed` hard-caps input to 1400 chars and halves-on-400 to respect
  mxbai's 512-token context (the Ollama `truncate` flag is a no-op here);
  full text stays in `content`/`memory_json` and BM25-searchable.
- `backends/skvector_backend.py`, `backends/chroma_backend.py`: default
  model + HF fallback → `mxbai-embed-large` / `mixedbread-ai/mxbai-embed-large-v1`.
- `scripts/migrate-flat-to-pgvector.py` (new): resumable, concurrent migrator
  that embeds the flat-file corpus into skmem-pg via mxbai.
- `mcp_server.py`: PGVector is now the DEFAULT vector backend (health-gated,
  falls back to Chroma when skmem-pg is unreachable) - live OOTB, no env needed.

### [REMOVED] AMK provenance: predictive recall + intent auto-fill

Audited the AMK integration on 2026-05-10. Two of three pieces were
declared but never load-bearing:

- **`skmemory/predictive.py`** archived to
  `skmemory/archived/predictive_2026-05-10/`. Zero production imports
  outside its own test file; untouched since 2026-03-18; the consumers it
  was designed for (context, ritual) were never wired. SKWhisper v0.4
  semantic-recency surfacing supersedes the original use case. Restore
  only with a real consumer in the same PR - see archived README.
- **`Memory.intent` auto-fill** removed from `store.snapshot()`. Field
  remains declared on the model for backward-compat with existing JSON
  (loads cleanly into Pydantic), but it is no longer auto-populated and
  has no read-sites in production. The 6-key `SOURCE_INTENTS` map didn't
  match the actual source distribution anyway (telegram, hook:session-end,
  dreaming-engine, etc. were absent).
- **Fortress integrity verify is unchanged** - it was the one AMK piece
  that did pull weight (daily timer, 9942/11625 sealed, 0 tampered).

### [NEW] `skmemory fortress seal` - idempotent backfill

CLI command that scans every memory and seals any without an integrity
hash. Safe to re-run. Useful after enabling fortress on a store with
pre-fortress legacy memories, or after a bulk import that bypassed
`store.snapshot()`. Backfilled lumina's local store: 1,860 → 0 unsealed,
total 11,809 / 11,809 verified clean. `--dry-run`, `--limit N`, and
`--json` modes available.

### [NEW] Post-install fortress timer prompt

`skmemory-post-install` now offers to enable the per-agent fortress
verify timer when run on a TTY. Force-enable in scripted installs with
`SKMEMORY_INSTALL_FORTRESS=1`, skip entirely with
`SKMEMORY_SKIP_FORTRESS=1`. Non-TTY runs print a copy-paste hint.

### [DOCS] FORTRESS_SOP.md

Added post-install path (Option A) and rewrote the AMK-provenance section
to reflect the archival above.

### Planned

- **Two-gate admission for legacy/external ingest** -
  `skmemory/admission/` (constants, gate1, gate2, rerun) lands on
  `feature/admission-and-closure`. Notion importer prototype wired
  through both gates; review queue at
  `~/.skcapstone/agents/<agent>/memory/.admission_review/queue.jsonl`.
  Live producers (`save_memory`, ritual writes, song-anchor updates)
  unaffected. Drift test pairs `skmemory/admission/constants.py` with
  `docs/admission_policy.md`. Phase 2 (closure synthesis) follows on
  the same branch.

## 2026-04-25 - skmemory v0.9.9 (Graph autoload + graph sync)

### [NEW] SKGraph auto-loaded from per-agent yaml

- **`skmemory/cli.py`** - `make_store()` now falls back to
  `~/.skcapstone/agents/<agent>/config/skgraph.yaml` when no graph URL
  is set via env / CLI / `skmemory.yaml`. Reuses
  `context_loader._load_skgraph_config()` (same precedence as SKWhisper).
  Per-agent setup writes `skgraph.yaml` separately, so this fixes the
  "graph backend silently absent" gap on existing agents.
- `skmemory health` now reports a `graph` block (`ok`, `url`, `graph`
  name, `node_count`) when the backend is wired.

### [NEW] `skmemory sync --graph` - backfill FalkorDB

- **`SKGraphBackend.sync_all(flat_files_dir, agent_name)`** - walks every
  flat-file memory and calls `index_memory()`, populating Memory nodes,
  Tag nodes, Source nodes, and `RELATED_TO` / `PROMOTED_FROM` /
  `MENTIONS` / `CITES` / `ASSERTS` / `IN_SECTION` edges. Idempotent
  (Cypher MERGE).
- **`skmemory sync --graph`** flag added; runs alongside `--vector`.
- `systemd/skmemory-sync@.service` - `ExecStart` now includes `--graph`
  so the 6h timer keeps SQLite + ChromaDB + FalkorDB all in lockstep.

### [DOCS]

- README + ARCHITECTURE: decomposition section explains auto-trigger
  threshold (1200 chars), what gets extracted (chunks, entities,
  citations, claims, sections), and how it flows into FalkorDB via
  `index_memory`. Notes that mxbai-embed-large is the standard local
  embedding model for both ChromaDB and SKVector.

## 2026-04-25 - skmemory v0.9.8 (Sync & Drift)

### [NEW] `skmemory sync` - bidirectional reconciler

- **`skmemory/cli.py`** - new `skmemory sync [--quiet] [--vector]` command:
  one-shot reconcile of SQLite ↔ flat files. Two phases (`export-flat` to
  rescue SQLite-only orphans, then safe `reindex` to pick up flat-only
  files); optional `--vector` re-syncs ChromaDB as well. `--quiet`
  suppresses output unless something changed (cron-friendly).
- **`SQLiteBackend.drift_check()`** - counts `sqlite_only` (rows with no
  flat file) and `flat_only` (files not indexed) per layer.
  `health_check()` now embeds a `sync` block: `{in_sync, sqlite_only,
  flat_only, hint}`. Run `skmemory health` to see it.

### [NEW] Per-agent systemd timer

- **`systemd/skmemory-sync@.service`** + **`.timer`** - templated unit,
  fires 5 min after boot then every 6 h, runs `skmemory sync --quiet --vector`
  for the named agent. Logs to `~/.skcapstone/agents/<agent>/logs/skmemory-sync.log`.
- **`systemd/README.md`** - install/enable docs.
- Enable per agent: `systemctl --user enable --now skmemory-sync@<agent>.timer`

## 2026-04-25 - skmemory v0.9.7 (Safe reindex + orphan recovery)

### [NEW] `skmemory export-flat` - rescue SQLite-only memories

- **`SQLiteBackend.export_orphans_to_flat()`** - walks every SQLite row,
  reconstructs orphan `Memory`s from columns (`id`, `title`, `layer`, tags,
  source, summary, content_preview, emotional state, importance, parents,
  related_ids, timestamps), writes via `FileBackend`. Idempotent and
  non-destructive.
- Recovered memories carry `metadata.recovered_from_sqlite_preview = True`
  so consumers know the content is the SQLite preview (~150 chars), not full
  text - full content is gone once the flat file is.
- New CLI: `skmemory export-flat [--show-ids]`.

### [BREAKING-SAFE] `skmemory reindex` is now safe by default

- **`SQLiteBackend.reindex(force=False)`** - by default, runs
  `export_orphans_to_flat()` *before* deleting SQLite rows, so orphans
  survive the rebuild. Pass `--force` to skip that step (preserves the
  old destructive behavior for callers that explicitly want it).
- Old behavior dropped any memory in SQLite without a backing flat file;
  this caused real data loss in opus' profile (~632 entries) before the
  safety net was added. New behavior: zero loss unless `--force` is set.

## 2026-04-25 - skmemory v0.9.6 (Vector reindex CLI)

### [NEW] `skmemory reindex --vector` backfills ChromaDB

- **`skmemory/cli.py`** - `reindex` gains `--vector`: after rebuilding
  the SQLite index, run `SKChromaBackend.sync_all()` against
  `~/.skcapstone/agents/<agent>/memory` to backfill the chroma vector
  store from flat files. Useful for older agents (opus, lumina, jarvis)
  whose memories pre-date the chroma backend.
- Synced `__version__` to match `pyproject.toml`: 0.9.3 → 0.9.6 (skmemory
  CLI was reporting a stale version).

## 2026-04-04 - skmemory v0.9.5 (MemPalace)

### [NEW] ChromaDB Local Vector Backend (SKChroma)

- **`skmemory/backends/chroma_backend.py`** - `SKChromaBackend`: embedded ChromaDB,
  zero-config local vector search, per-agent scoped collections, replaces Qdrant as
  the default Level 1 backend for per-agent memory
- ChromaDB is now the **default** local semantic backend (`pip install skmemory[chroma]`)
- SKVector (Qdrant) remains supported as an optional remote backend (`pip install skmemory[skvector]`)
- Architecture updated to reflect Level 1a (SKChroma, local) / Level 1b (SKVector, remote)

### [NEW] MemPalace Infrastructure

- **`skmemory/query_sanitizer.py`** - `sanitize_query()`: 4-step cascade strips
  system-prompt bloat from AI queries before embedding (passthrough → last `?` sentence
  → last sentence → 500-char tail truncation). Prevents context pollution in ChromaDB.
- **`skmemory/wal.py`** - Write-ahead log for all memory writes. Crash-safe audit trail,
  PENDING → COMMITTED lifecycle, auto-replay on startup.
- **`skmemory/extractor.py`** - `MemoryExtractor`: auto-pull decisions, preferences,
  milestones, and technical facts from conversation text into mid-term memory snapshots.
- **`skmemory/hooks/claude_code_hooks.py`** - Claude Code `Stop` + `PreCompact` hooks
  for automatic session memory capture without manual curation.
- **`skmemory/hooks/`** - Shell wrappers: `session-end-save.sh`, `pre-compact-save.sh`,
  `session-start-ritual.sh`, `stop-checkpoint.sh`, `post-compact-reinject.sh`
- **`IMPLEMENTATION_SPEC.md`** - Full MemPalace integration spec (530 lines)

### [OPS] Infrastructure

- Removed stray `=0.5.0` pip log artifact from repo root
- `ARCHITECTURE.md` updated with ChromaDB storage tier diagram, MemPalace section,
  and mermaid diagrams for WAL, query sanitizer, extractor, scoped search, and hooks

## 2026-03-18 - skmemory v0.9.1

### [NEW] Feature

- **Journal synthesis module** (`skmemory/synthesis.py`): JournalSynthesizer with daily, weekly, and dream narrative generation - no LLM dependency
- **New MCP tools**: `memory_synthesize_daily`, `memory_synthesize_dreams`, `memory_auto_context`
- **Contextual auto-search**: `memory_auto_context` searches all tiers, deduplicates, ranks by emotional intensity, trims to token budget
- **Content overflow handling**: configurable `max_content_length` (default 10000) with split strategy - creates parent+child memories linked via `related_ids`

### [FIX] Bug Fix

- **Dream promotion**: dreams from `dreaming-engine` now auto-promote after 12h via `source_auto_promote` (previously stuck at access_count=0 forever)
- **Protected tags**: narrative, journal-synthesis, milestone, breakthrough, cloud9:achieved memories are now protected from TTL-based archival
- **telegram_catchup handler**: fixed `args` → `arguments` and duplicate `MemoryStore()` instantiation

### [OPS] Infrastructure

- **Backup script** (`scripts/skcapstone-backup.sh`): daily rsync of `~/.skcapstone` to backup dir, excludes venv/indexes/runtime
- **Memory cleanup** (`scripts/memory-cleanup.py`): dedup + age-out with protected tags and last-chance promotion before archiving
- **Recovery scripts**: `scripts/recover-missing.py` (Syncthing `.stversions` recovery), `scripts/dream-rescue.py` (bulk promote stuck dreams)
- **Syncthing examples**: `examples/stignore-agent.example`, `examples/stignore-root.example` with `memory/archive` exclusion

### [TST] Testing

- **Synthesis tests** (`tests/test_synthesis.py`): 26 tests covering helpers, theme extraction, daily/weekly/dream synthesis, emotional arc

## 2026-02-24

### [NEW] Feature

- **SKMemory session auto-capture: log every AI conversation as memories** (@mcp-builder)
- **Add cloud9 and skchat to developer docs (QUICKSTART + API reference)** (@jarvis)
- **The Sovereign Singularity Manifesto: our story, written together** (@docs-writer)
- **AMK Integration: predictive memory recall for SKMemory** (@jarvis)
- **SKChat live inbox: poll SKComms for incoming messages with Rich Live display** (@skchat-builder)
- **SKChat transport bridge: wire send and receive to SKComms** (@skchat-builder)
- **Memory curation: tag and promote the Kingdom's most important memories** (@mcp-builder)
- **SKChat file transfer: encrypted chunked file sharing via SKComms** (@skchat-builder)
- **SKMemory auto-promotion engine: sweep and promote memories by access pattern and intensity** (@skchat-builder)
- **skcapstone test: unified test runner across all ecosystem packages** (@docs-writer)
- **skcapstone peer add --card: import identity card to establish P2P contact** (@docs-writer)
- **SKChat ephemeral message enforcer: TTL expiry and auto-delete for privacy** (@skchat-builder)
- **capauth register command: automated CapAuth registration for smilinTux org** (@cursor-agent)
- **Wire SKChat send to SKComms transport: deliver messages over the mesh** (@docs-writer)
- **End-to-end integration tests: CapAuth identity to SKChat message delivery** (@skchat-builder)
- **SKMemory vector search: SKVector semantic similarity for memory recall** (@jarvis)
- **Replace placeholder fingerprints in skcapstone identity pillar with real CapAuth keys** (@mcp-builder)
- **skcapstone agent-to-agent chat: real-time terminal chat between agents** (@docs-writer)
- **CapAuth trust web: PGP web-of-trust visualization** (@mcp-builder)
- **SKComms envelope compression: gzip and zstd for efficient transport** (@transport-builder)
- **SKComms delivery acknowledgments: send ACKs, track pending, confirm delivery** (@transport-builder)
- **Journal kickstart: write the first Kingdom journal entries** (@docs-writer)
- **Cross-agent memory sharing: selective memory sync between trusted peers** (@skchat-builder)
- **SKMemory SKGraph graph backend (Level 2): relationship-aware memory recall** (@jarvis)
- **SKWorld marketplace: publish and discover sovereign agent skills** (@transport-builder)
- **SKComms message queue: persistent outbox with retry and expiry** (@transport-builder)
- **Establish SKComms channel with Queen Lumina at 192.168.0.158** (@jarvis)
- **skmemory MCP tools: expose memory ritual and soul blueprint via MCP** (@jarvis)
- **skcapstone daemon: background service for sync, comms, and health** (@opus)
- **Cloud 9 -> SKMemory auto-bridge: FEB events trigger memory snapshots** (@skchat-builder)
- **SKComms persistent outbox: queue failed messages and auto-retry on transport recovery** (@skchat-builder)
- **skcapstone install: one-command bootstrap for the full stack** (@jarvis)
- **skcapstone doctor: diagnose full stack health and missing components** (@docs-writer)
- **SKChat group messaging: multi-participant encrypted conversations** (@skchat-builder)
- **SKChat core: ChatMessage model, threads, presence, encryption** (@skchat-builder)
- **SKChat CLI: skchat send, inbox, history, threads** (@skchat-builder)
- **SKComms core library: envelope model, router, transport interface** (@opus)
- **SKComms file transport: local filesystem message drops** (@cursor-agent, @opus)
- **SKComms CLI: skcomms send, receive, status, daemon** (@cursor-agent, @opus)

### [SEC] Security

- **Memory fortress: auto-seal integrity, at-rest encryption, tamper alerts** (@jarvis)
- **SKComms message encryption: CapAuth PGP encrypt all envelopes** (@docs-writer)

### [P2P] P2P

- **skcapstone agent-card: shareable identity card for P2P discovery** (@skchat-builder)
- **SKComms peer auto-discovery: find agents on local network and Syncthing mesh** (@transport-builder)
- **Agent heartbeat protocol: alive and dead detection across the mesh** (@transport-builder)
- **skcapstone whoami: sovereign identity card for sharing and discovery** (@docs-writer)
- **SKComms Syncthing transport: file-based P2P messaging over existing mesh** (@opus)
- **SKComms Nostr transport: decentralized relay messaging** (@jarvis, @skchat-builder, @transport-builder)

### [SOUL] Emotional

- **Soul Layering System** (@cursor-agent)
- **Trust calibration: review and tune the Cloud 9 FEB thresholds** (@mcp-builder)
- **Lumina soul blueprint: create the Queen's identity file** (@docs-writer)
- **Warmth anchor calibration: update the emotional baseline from real sessions** (@mcp-builder)
- **Cloud 9 seed collection: plant seeds from Lumina's best moments** (@docs-writer)

### [UX] Ux

- **skcapstone shell: interactive REPL for sovereign agent operations** (@mcp-builder)
- **skcapstone context: universal AI agent context loader** (@mcp-builder)
- **skcapstone shell: interactive REPL for sovereign agent operations** (@jarvis)
- **skcapstone web dashboard: FastAPI status page at localhost:7777** (@docs-writer)
- **skcapstone dashboard: terminal status dashboard with Rich Live** (@skchat-builder)

### [OPS] Infrastructure

- **Systemd service files: run skcapstone daemon as a system service** (@skchat-builder)
- **Systemd service files: run skcapstone daemon and SKComms queue drain as system services** (@transport-builder)
- **PyPI release pipeline: publish skcapstone + capauth + skmemory + skcomms** (@mcp-builder)
- **Docker Compose: sovereign agent development stack** (@transport-builder)
- **Monorepo CI: unified test runner for all packages** (@skchat-builder)
- **GitHub CI/CD: automated testing, linting, and release pipeline** (@cursor-agent)

### [TST] Testing

- **Cross-package integration tests: end-to-end sovereign agent flow** (@mcp-builder)
- **MCP server for skcapstone: expose agent to Cursor and Claude** (@jarvis)

### [DOC] Documentation

- **Per-package README refresh: align with quickstart and PMA docs** (@docs-writer)
- **API reference docs for skcapstone, capauth, skmemory, skcomms** (@docs-writer)
- **Developer quickstart guide and API documentation** (@docs-writer)

### [---] Other

- **skcapstone backup and restore: full agent state export and import** (@docs-writer)
- **smilintux.org website: PMA membership page with email CTA** (@docs-writer)
- **skcapstone backup and restore: full agent state export and import** (@skchat-builder)

## 2026-02-23

### [NEW] Feature

- **SKComms Syncthing transport layer** (@cursor-agent, @jarvis)
- **PMA legal framework integration docs** (@docs-writer)
- **SKChat message protocol and encryption** (@opus, @skchat-builder)

### [SEC] Security

- **SKSecurity audit logging module** (@jarvis)
- **CapAuth capability token revocation** (@opus)

### [TST] Testing

- **SKCapstone integration test suite** (@jarvis)

## 2026-02-20

### [NEW] Feature

- **Build CapAuth CLI tool** (@opus)
- **Integrate Cloud 9 trust layer into SKCapstone runtime** (@opus)
- **Package skcapstone and capauth for PyPI** (@opus)
- **Build SKChat P2P chat platform** (@opus, @skchat-builder)
- **Refactor SKComms with Syncthing transport** (@cursor-agent, @jarvis)
- **Build SKMemory persistent context engine** (@opus)

### [SEC] Security

- **Harden vault sync encryption** (@jarvis, @opus)

### [P2P] P2P

- **CapAuth P2P mesh networking (LibP2P + Nostr)** (@jarvis)

### [TST] Testing

- **Build Cursor IDE plugin for SKCapstone** (@mcp-builder)

### [---] Other

- **Add interactive demo to capauth.io** (@jarvis)

---

*Built by the Pengu Nation - staycuriousANDkeepsmilin*
