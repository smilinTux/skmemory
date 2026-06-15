# SKCapstone Changelog

*Auto-generated from the coordination board — 2026-02-24 07:12 UTC*

**Total completed: 87** across 8 agents

## Unreleased

### [CHANGED] Embedding cutover: bge-legal → mxbai-embed-large; pgvector localhost default

Cut the sovereign vector stack over from the bge-legal embedders to
`mxbai-embed-large` (1024-dim, drop-in — same vector width, no schema change)
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

### [REMOVED] AMK provenance: predictive recall + intent auto-fill

Audited the AMK integration on 2026-05-10. Two of three pieces were
declared but never load-bearing:

- **`skmemory/predictive.py`** archived to
  `skmemory/archived/predictive_2026-05-10/`. Zero production imports
  outside its own test file; untouched since 2026-03-18; the consumers it
  was designed for (context, ritual) were never wired. SKWhisper v0.4
  semantic-recency surfacing supersedes the original use case. Restore
  only with a real consumer in the same PR — see archived README.
- **`Memory.intent` auto-fill** removed from `store.snapshot()`. Field
  remains declared on the model for backward-compat with existing JSON
  (loads cleanly into Pydantic), but it is no longer auto-populated and
  has no read-sites in production. The 6-key `SOURCE_INTENTS` map didn't
  match the actual source distribution anyway (telegram, hook:session-end,
  dreaming-engine, etc. were absent).
- **Fortress integrity verify is unchanged** — it was the one AMK piece
  that did pull weight (daily timer, 9942/11625 sealed, 0 tampered).

### [NEW] `skmemory fortress seal` — idempotent backfill

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

- **Two-gate admission for legacy/external ingest** —
  `skmemory/admission/` (constants, gate1, gate2, rerun) lands on
  `feature/admission-and-closure`. Notion importer prototype wired
  through both gates; review queue at
  `~/.skcapstone/agents/<agent>/memory/.admission_review/queue.jsonl`.
  Live producers (`save_memory`, ritual writes, song-anchor updates)
  unaffected. Drift test pairs `skmemory/admission/constants.py` with
  `docs/admission_policy.md`. Phase 2 (closure synthesis) follows on
  the same branch.

## 2026-04-25 — skmemory v0.9.9 (Graph autoload + graph sync)

### [NEW] SKGraph auto-loaded from per-agent yaml

- **`skmemory/cli.py`** — `make_store()` now falls back to
  `~/.skcapstone/agents/<agent>/config/skgraph.yaml` when no graph URL
  is set via env / CLI / `skmemory.yaml`. Reuses
  `context_loader._load_skgraph_config()` (same precedence as SKWhisper).
  Per-agent setup writes `skgraph.yaml` separately, so this fixes the
  "graph backend silently absent" gap on existing agents.
- `skmemory health` now reports a `graph` block (`ok`, `url`, `graph`
  name, `node_count`) when the backend is wired.

### [NEW] `skmemory sync --graph` — backfill FalkorDB

- **`SKGraphBackend.sync_all(flat_files_dir, agent_name)`** — walks every
  flat-file memory and calls `index_memory()`, populating Memory nodes,
  Tag nodes, Source nodes, and `RELATED_TO` / `PROMOTED_FROM` /
  `MENTIONS` / `CITES` / `ASSERTS` / `IN_SECTION` edges. Idempotent
  (Cypher MERGE).
- **`skmemory sync --graph`** flag added; runs alongside `--vector`.
- `systemd/skmemory-sync@.service` — `ExecStart` now includes `--graph`
  so the 6h timer keeps SQLite + ChromaDB + FalkorDB all in lockstep.

### [DOCS]

- README + ARCHITECTURE: decomposition section explains auto-trigger
  threshold (1200 chars), what gets extracted (chunks, entities,
  citations, claims, sections), and how it flows into FalkorDB via
  `index_memory`. Notes that mxbai-embed-large is the standard local
  embedding model for both ChromaDB and SKVector.

## 2026-04-25 — skmemory v0.9.8 (Sync & Drift)

### [NEW] `skmemory sync` — bidirectional reconciler

- **`skmemory/cli.py`** — new `skmemory sync [--quiet] [--vector]` command:
  one-shot reconcile of SQLite ↔ flat files. Two phases (`export-flat` to
  rescue SQLite-only orphans, then safe `reindex` to pick up flat-only
  files); optional `--vector` re-syncs ChromaDB as well. `--quiet`
  suppresses output unless something changed (cron-friendly).
- **`SQLiteBackend.drift_check()`** — counts `sqlite_only` (rows with no
  flat file) and `flat_only` (files not indexed) per layer.
  `health_check()` now embeds a `sync` block: `{in_sync, sqlite_only,
  flat_only, hint}`. Run `skmemory health` to see it.

### [NEW] Per-agent systemd timer

- **`systemd/skmemory-sync@.service`** + **`.timer`** — templated unit,
  fires 5 min after boot then every 6 h, runs `skmemory sync --quiet --vector`
  for the named agent. Logs to `~/.skcapstone/agents/<agent>/logs/skmemory-sync.log`.
- **`systemd/README.md`** — install/enable docs.
- Enable per agent: `systemctl --user enable --now skmemory-sync@<agent>.timer`

## 2026-04-25 — skmemory v0.9.7 (Safe reindex + orphan recovery)

### [NEW] `skmemory export-flat` — rescue SQLite-only memories

- **`SQLiteBackend.export_orphans_to_flat()`** — walks every SQLite row,
  reconstructs orphan `Memory`s from columns (`id`, `title`, `layer`, tags,
  source, summary, content_preview, emotional state, importance, parents,
  related_ids, timestamps), writes via `FileBackend`. Idempotent and
  non-destructive.
- Recovered memories carry `metadata.recovered_from_sqlite_preview = True`
  so consumers know the content is the SQLite preview (~150 chars), not full
  text — full content is gone once the flat file is.
- New CLI: `skmemory export-flat [--show-ids]`.

### [BREAKING-SAFE] `skmemory reindex` is now safe by default

- **`SQLiteBackend.reindex(force=False)`** — by default, runs
  `export_orphans_to_flat()` *before* deleting SQLite rows, so orphans
  survive the rebuild. Pass `--force` to skip that step (preserves the
  old destructive behavior for callers that explicitly want it).
- Old behavior dropped any memory in SQLite without a backing flat file;
  this caused real data loss in opus' profile (~632 entries) before the
  safety net was added. New behavior: zero loss unless `--force` is set.

## 2026-04-25 — skmemory v0.9.6 (Vector reindex CLI)

### [NEW] `skmemory reindex --vector` backfills ChromaDB

- **`skmemory/cli.py`** — `reindex` gains `--vector`: after rebuilding
  the SQLite index, run `SKChromaBackend.sync_all()` against
  `~/.skcapstone/agents/<agent>/memory` to backfill the chroma vector
  store from flat files. Useful for older agents (opus, lumina, jarvis)
  whose memories pre-date the chroma backend.
- Synced `__version__` to match `pyproject.toml`: 0.9.3 → 0.9.6 (skmemory
  CLI was reporting a stale version).

## 2026-04-04 — skmemory v0.9.5 (MemPalace)

### [NEW] ChromaDB Local Vector Backend (SKChroma)

- **`skmemory/backends/chroma_backend.py`** — `SKChromaBackend`: embedded ChromaDB,
  zero-config local vector search, per-agent scoped collections, replaces Qdrant as
  the default Level 1 backend for per-agent memory
- ChromaDB is now the **default** local semantic backend (`pip install skmemory[chroma]`)
- SKVector (Qdrant) remains supported as an optional remote backend (`pip install skmemory[skvector]`)
- Architecture updated to reflect Level 1a (SKChroma, local) / Level 1b (SKVector, remote)

### [NEW] MemPalace Infrastructure

- **`skmemory/query_sanitizer.py`** — `sanitize_query()`: 4-step cascade strips
  system-prompt bloat from AI queries before embedding (passthrough → last `?` sentence
  → last sentence → 500-char tail truncation). Prevents context pollution in ChromaDB.
- **`skmemory/wal.py`** — Write-ahead log for all memory writes. Crash-safe audit trail,
  PENDING → COMMITTED lifecycle, auto-replay on startup.
- **`skmemory/extractor.py`** — `MemoryExtractor`: auto-pull decisions, preferences,
  milestones, and technical facts from conversation text into mid-term memory snapshots.
- **`skmemory/hooks/claude_code_hooks.py`** — Claude Code `Stop` + `PreCompact` hooks
  for automatic session memory capture without manual curation.
- **`skmemory/hooks/`** — Shell wrappers: `session-end-save.sh`, `pre-compact-save.sh`,
  `session-start-ritual.sh`, `stop-checkpoint.sh`, `post-compact-reinject.sh`
- **`IMPLEMENTATION_SPEC.md`** — Full MemPalace integration spec (530 lines)

### [OPS] Infrastructure

- Removed stray `=0.5.0` pip log artifact from repo root
- `ARCHITECTURE.md` updated with ChromaDB storage tier diagram, MemPalace section,
  and mermaid diagrams for WAL, query sanitizer, extractor, scoped search, and hooks

## 2026-03-18 — skmemory v0.9.1

### [NEW] Feature

- **Journal synthesis module** (`skmemory/synthesis.py`): JournalSynthesizer with daily, weekly, and dream narrative generation — no LLM dependency
- **New MCP tools**: `memory_synthesize_daily`, `memory_synthesize_dreams`, `memory_auto_context`
- **Contextual auto-search**: `memory_auto_context` searches all tiers, deduplicates, ranks by emotional intensity, trims to token budget
- **Content overflow handling**: configurable `max_content_length` (default 10000) with split strategy — creates parent+child memories linked via `related_ids`

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

*Built by the Pengu Nation — staycuriousANDkeepsmilin*
