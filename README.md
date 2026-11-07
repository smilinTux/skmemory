# SKMemory

> **Purpose:** sovereign, multi-layer, emotionally-aware memory for AI agents (flat-file source of truth + SQLite index + vector recall). **Maturity: crypto tier T0 - Classical** (at-rest GPG sealing only; no hybrid PQ KEM today - see [SOP §9](./SOP.md#9-maturity-tier--version-reference)).

[![Tests](https://github.com/smilinTux/skmemory/actions/workflows/pytest.yml/badge.svg)](https://github.com/smilinTux/skmemory/actions/workflows/pytest.yml)
[![PyPI version](https://img.shields.io/pypi/v/skmemory.svg)](https://pypi.org/project/skmemory/)
[![npm version](https://img.shields.io/npm/v/@smilintux/skmemory.svg)](https://www.npmjs.com/package/@smilintux/skmemory)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python](https://img.shields.io/pypi/pyversions/skmemory.svg)](https://pypi.org/project/skmemory/)

**Universal AI Memory System - polaroid snapshots for AI consciousness.**

SKMemory gives AI agents a multi-layer, emotionally-aware memory that survives context resets. Instead of dumping flat transcript summaries, it captures each moment as a *polaroid*: the content, the emotional fingerprint, the source provenance, and a tamper-evident integrity seal. Memories are organized across three persistence tiers (short → mid → long), auto-routed into four semantic quadrants (CORE / WORK / SOUL / WILD), and exposed to any MCP-capable client through a stdio server. Storage is exactly two tiers: a **local** tier (flat JSON files as the source of truth plus a SQLite `index.db` with an FTS5 lexical arm, always on and offline capable) and a **shared** tier (the skmem-pg Postgres stack: pgvector, pg_search BM25 and Apache AGE). ChromaDB, Qdrant and FalkorDB are retired. A soul blueprint (`~/.skcapstone/agents/<agent>/soul/base.json`) and rehydration ritual give new instances a "who was I?" answer before the first user message arrives.

**Active agent** is resolved by `skmemory.agents.get_active_agent()`: the selectors `SKAGENT`, `SKCAPSTONE_AGENT` and `SKMEMORY_AGENT` (any that are set must agree), then the declared default `SK_DEFAULT_AGENT`, then the sole installed agent, otherwise `None`. **There is no implicit default agent**: with several agents installed and nothing declared the resolver refuses rather than guess, and a caller that treats `None` as "use the template" reads a scaffold instead of the real memories. Declare the agent in a file (`~/.config/environment.d/10-skagent.conf`) per the [node configuration standard](https://github.com/smilinTux/sk-standards/blob/main/standards/SKMEMORY_NODE_CONFIG_STANDARD.md). Every per-agent path (memory, soul, journal, FEBs, vector store, sessions) lives under `~/.skcapstone/agents/$SKAGENT/`.

---

## Install

### Python (CLI + MCP server + Python API)

```bash
pip install skmemory
```

With optional backends:

```bash
# skmem-pg client (pgvector + AGE)
pip install "skmemory[pg]"

# Telegram importer
pip install "skmemory[telegram]"

# Everything
pip install "skmemory[all]"
```

### npm (JavaScript / Node wrapper)

```bash
npm install @smilintux/skmemory
# or
npx @smilintux/skmemory
```

### skmem-pg (Postgres backend: pgvector + BM25 + AGE + ops), out of the box

`skmem-pg` is the production storage backend (pgvector + ParadeDB `pg_search` BM25 +
Apache AGE graphs), run **LOCAL per node** on `127.0.0.1:5432` (no replication, no SPOF;
flat JSON files stay the source of truth, `memories` is a derived cache). A fresh
`docker compose up` comes up **fully migrated** with zero manual DDL. Full detail lives in
[`deploy/skmem-pg/README.md`](./deploy/skmem-pg/README.md); this is the seamless path.

**1. Bring up a fully-migrated instance (fresh node):**

```bash
# password is required, never committed, no default (compose refuses to start without it):
export SKMEM_PG_PASSWORD="$(skvault creds-get SKMEM_PG_PASSWORD)"   # or any strong value on first install
docker compose up -d --build
```

The compose init wrapper (`deploy/skmem-pg/initdb/00-run-init.sh`) runs once at first
boot and applies, in order: `schema.sql` (the live post-cutover snapshot: extensions +
tables + `hybrid_search_docs/memories` + all HNSW/BM25 indexes) then **every forward
migration in `deploy/skmem-pg/migrations.txt`** (currently `03-ops-namespace.sql`: the
skbrain `ops` schema + `ops_brain` graph + privacy wall). The two historical, superseded
migrations (`02-enable-bm25-age.sql`, `03-cutover-mxbai.sql`) are deliberately excluded
from the manifest: they are already baked into `schema.sql`, and `03-cutover-mxbai.sql` is
not idempotent, so it must never auto-run. Fresh-boot and live-node paths read the same
`migrations.txt`, so they never drift.

If a production skmem-pg already listens on 5432, override the port:
`SKMEM_PG_PORT=15432 docker compose up -d`.

**2. Migrate an existing (live) node** with the idempotent runner instead of re-init:

```bash
skmemory pg migrate                       # apply every pending forward migration (pre-dump + verify)
skmemory pg migrate 03-ops-namespace.sql --dry-run   # preview one migration; touch nothing
```

Every apply runs `ON_ERROR_STOP=1` in one guarded transaction, so a re-run is a safe
no-op and a partial apply cannot occur. CI never applies migrations.

**3. Bind the ops LOGIN roles** (the one step passwords keep out of the image). The
migration creates only the NOLOGIN group roles `skbrain_ops_rw` / `skbrain_ops_ro`; bind
LOGIN roles into them from skvault, no secret ever in the repo, image, argv, or logs:

| Login role | Group | Password env var | skvault entry |
| --- | --- | --- | --- |
| `skbrain_projector` | `skbrain_ops_rw` | `SKBRAIN_PG_PROJECTOR_PW` | `SKBRAIN_PG_PROJECTOR_PW` |
| `skbrain_reader` | `skbrain_ops_ro` | `SKBRAIN_PG_READER_PW` | `SKBRAIN_PG_READER_PW` |

```bash
# Copy the template, then fill both values FROM SKVAULT (create the entries on first install):
cp deploy/skmem-pg/skbrain.conf.example ~/.config/environment.d/skbrain.conf
skvault unlock --word <phrase>
printf 'SKBRAIN_PG_PROJECTOR_PW=%s\n' "$(skvault creds-get SKBRAIN_PG_PROJECTOR_PW)" >> ~/.config/environment.d/skbrain.conf
printf 'SKBRAIN_PG_READER_PW=%s\n'    "$(skvault creds-get SKBRAIN_PG_READER_PW)"    >> ~/.config/environment.d/skbrain.conf

skmemory pg roles              # create/ALTER both LOGIN roles + GRANT the groups (idempotent)
skmemory pg roles --dry-run    # preview with the password redacted; touch nothing
```

**4. Required environment** (the node-local backend + embeddings):

| Variable | Purpose | Value |
| --- | --- | --- |
| `SKMEMORY_PG_DSN` | Node-local skmem-pg DSN read by the store + AGE backend (authoritative; wins over the Syncthing-shared yaml) | `postgresql://postgres:${SKMEM_PG_PASSWORD}@127.0.0.1:5432/skmemory` |
| `SKMEM_PG_PASSWORD` | Superuser password consumed by `docker compose` (no default) | from skvault `SKMEM_PG_PASSWORD` |
| `SKBRAIN_PG_PROJECTOR_PW` / `SKBRAIN_PG_READER_PW` | ops LOGIN-role passwords for `skmemory pg roles` | from skvault (see table above), via `~/.config/environment.d/skbrain.conf` |
| `SKMEMORY_EMBED_URL` | Embedding backend endpoint (`SKMEMORY_EMBED_URLS` for an ordered failover list) | `http://<embed-host>:11434/api/embed` (default `http://localhost:11434/api/embed`) |
| `SKMEMORY_EMBED_MODEL` | Embedding model | `mxbai-embed-large` (**1024-dim**) |

Install `skmemory[pg]` on a node that uses skmem-pg. The reconcile engine reads
`SKMEMORY_PG_DSN` from the environment and connects through psycopg. It does not
require Docker socket access and never places the DSN in process arguments or logs.

**Embeddings:** `mxbai-embed-large` at 1024 dimensions everywhere (Ollama `:11434`,
`ctx=512`), with a `mixedbread-ai/mxbai-embed-large-v1` network fallback. Every `vector(…)`
column in `schema.sql` and the `ops` namespace is `vector(1024)`; the dimension must match
across the mesh or cross-collection recall breaks.

**5. Verify health** (read-only, safe against a live instance):

```bash
# Extensions present (expect: age, pg_search, vector) and ops schema installed:
docker exec skmem-pg psql -U postgres -d skmemory -c "SELECT extname FROM pg_extension ORDER BY 1;"

# Full ops-namespace verify (schema=1, tables=3, hnsw=1, bm25=1, fn=1, graph=1, public_can_use_ops=f):
docker exec -i skmem-pg psql -U postgres -d skmemory < deploy/skmem-pg/verify-ops.sql

# App-level health (store health + sync block) and operator observability probe:
skmemory health
skmemory operator observe        # EmbedServing / ReconcileFresh conditions
```

The skos provisioner (OPS1.2) reaches the same end state non-interactively by calling
`skmemory pg migrate` then `skmemory pg roles` (`--skip-missing` tolerates an un-provisioned
role). Per-node cold-boot ordering (Syncthing restore + `RESTORE_COMPLETE` sentinel before
reconcile) is in [`deploy/skmem-pg/README.md`](./deploy/skmem-pg/README.md).

---

## Architecture

```mermaid
flowchart TD
    CLI["skmemory CLI\n(click)"]
    MCP["skmemory-mcp\n(stdio MCP server)"]
    API["Python API\nMemoryStore"]

    CLI --> Store
    MCP --> Store
    API --> Store

    Store["MemoryStore\n(core orchestrator)"]

    Store --> Decompose["Decomposition Engine\nchunks + citations + entities + claims"]
    Store --> Primary
    Store --> Vector
    Store --> Graph

    subgraph Primary["Local tier: primary"]
        SQLite["SQLiteBackend\n(default)"]
        File["FileBackend\n(legacy JSON)"]
        Vaulted["VaultedSQLiteBackend\n(PGP-encrypted)"]
    end

    subgraph Vector["Shared tier: vector"]
        PG["PGVectorBackend\n(skmem-pg, hybrid vector + BM25)"]
    end

    subgraph Graph["Graph Backend (optional)"]
        AGE["AGEGraphBackend\nApache AGE in skmem-pg + decomposition nodes"]
    end

    Store --> Fortress["FortifiedMemoryStore\nTamper detection + Audit log"]
    Store --> Soul["SoulBlueprint\n~/.skcapstone/soul/base.json"]
    Store --> Ritual["Ritual\nRehydration ceremony"]

    subgraph Layers["Memory Layers"]
        Short["Short-term\n(session)"]
        Mid["Mid-term\n(project)"]
        Long["Long-term\n(identity)"]
    end

    Store --> Short
    Short -->|promote| Mid
    Mid -->|promote| Long

    subgraph Quadrants["Auto-routing Quadrants"]
        CORE["CORE\n(identity, relationships)"]
        WORK["WORK\n(code, tasks)"]
        SOUL["SOUL\n(emotions, feelings)"]
        WILD["WILD\n(creativity, chaos)"]
    end

    Store --> Quadrants

    subgraph Importers["Importers"]
        Telegram["Telegram API"]
        Seeds["Cloud 9 Seeds"]
    end

    Importers --> Store
```

---

## Features

- **Polaroid snapshot model** - every memory stores content, emotional intensity (0-10), valence (−1 to +1), emotion labels, and a free-text resonance note
- **Three-layer persistence** - `short-term` (session-scoped), `mid-term` (project-scoped), `long-term` (identity-level); memories promote up the ladder via CLI, MCP, or API
- **Four semantic quadrants** - CORE, WORK, SOUL, WILD; keyword-based auto-classification routes memories to appropriate buckets with per-quadrant retention rules
- **Two tiers, nothing else** - local: flat JSON (truth) plus SQLite `index.db` with an FTS5 lexical arm, always on and offline capable; shared: the skmem-pg stack (pgvector semantic search, pg_search BM25, Apache AGE graph and lineage chains). ChromaDB, Qdrant (skvector) and FalkorDB (skgraph) are retired
- **Reader audience** - search and every read tool take a `reader` and enforce it inside each retrieval arm; `SKMEMORY_READER` sets a per-process ceiling
- **Fused search** - concurrent arms under a deadline, query-shape routing, source-tier occupancy cap, `as_of` and supersession awareness, and a retrieval log that feeds `memory_health`
- **Prospective memory** - intentions (time and event), a no-LLM prompt prefilter, a tick that delivers through sk-alert, `memory_remind` and GTD `--due`/`--remind`
- **Outcome ledger** - append-only record of which skills were served, followed and with what effect
- **Sync & drift detection** - `skmemory health` surfaces SQLite ↔ flat-file drift; `skmemory sync` reconciles bidirectionally; per-agent `skmemory-sync@<agent>.timer` keeps everything in lockstep automatically
- **Decomposition-aware ingestion** - `skmemory ingest-file` and `skmemory snapshot --decompose` create parent + chunk memories and extract section titles, citations, entities, and claims for downstream indexing. Auto-triggers when content ≥ 1200 chars (`DECOMPOSE_MIN_LENGTH`). Extracted entities/citations/claims/sections flow into the AGE graph automatically when skmem-pg is configured.
- **Graph retrieval over decomposition signals** - query the graph by entity, citation, claim, or section via `skmemory graph ...`
- **Issue-oriented retrieval scaffolding** - `skmemory novelty`, `skmemory session-brief`, and `skmemory task-pack` turn live problems into ranked memory support with authority tiers, novelty leads, deadlines, defenses, and reusable task packs
- **MCP server** - stdio tools exposed for Claude Code CLI, Cursor, Claude Desktop, Windsurf, Aider, Cline, and any MCP-speaking client
- **Fortress / tamper detection** - every memory is SHA-256 sealed on write (`Memory.seal()`); integrity is verified on every recall; tampered memories trigger structured `TamperAlert` events
- **Audit trail** - chain-hashed JSONL log of every store / recall / delete / tamper event, inspectable via `memory_audit` MCP tool or `skmemory audit` CLI
- **Optional PGP encryption** - `VaultedSQLiteBackend` stores ciphertext so the underlying files are unreadable without the private key
- **Soul Blueprint** - persistent AI identity JSON/YAML (`~/.skcapstone/soul/base.json`) carrying name, role, relationships, core memories, values, and emotional baseline
- **Rehydration ritual** - `skmemory ritual` runs a full ceremony loading soul, seeds, and recent memories into a context payload for injection at session start
- **Cloud 9 seed integration** - seeds planted by one AI instance become searchable long-term memories for the next via `skmemory import-seeds`
- **Telegram importer** - import Telegram chat history (JSON export or live API via Telethon) as timestamped memories
- **Session consolidation** - compress a session's short-term snapshots into one mid-term memory via `skmemory consolidate`
- **Maps of Content (MOC)** - `skmemory moc` auto-generates read-side index documents grouping memories **by quadrant** (Core/Work/Soul/Wild) and **by tag cluster**. Deterministic (byte-identical Markdown for the same input) and bounded; renders to stdout or writes one `<key>.md` per index with `--out`. Pure aggregation - never mutates the store.
- **Schema-validated writes** - every write passes a pluggable **pre-write hook** chain before touching any backend. The default `schema_validator` re-validates each memory against the canonical `Memory` schema (catching fields mutated after construction) and rejects malformed writes with a `SchemaValidationError`. Register custom hooks via `MemoryStore.register_pre_write_hook(...)`.
- **Fresh-context runner seam** - consolidation/promotion passes route through an injectable `FreshContextRunner` (`skmemory/fresh_context.py`) so a long, chatty maintenance sweep can run in an isolated context (spawned subagent/subprocess) instead of polluting the live agent's working context window. Defaults to an in-process runner (behavior unchanged); `SubprocessRunner(spawn)` is the extension point for real spawning. Wired into `PromotionEngine`, `PromotionScheduler`, and `skmemory sweep`.
- **Auto-sweep / promotion daemon** - `skmemory sweep --daemon` runs every 6 hours, auto-promoting qualifying memories based on intensity thresholds
- **Steel Man collider** - `skmemory steelman` runs a seed-framework-driven adversarial argument evaluator with identity verification
- **Backup / restore** - dated JSON backups with pruning; `skmemory export` / `skmemory import`
- **Token-efficient context loading** - `memory_context` MCP tool and `store.load_context()` fit strongest + recent memories within a configurable token budget
- **Auto-save hooks** - Claude Code hooks auto-save context before compaction and reinject memory after; OpenClaw agents get per-message auto-save via ConsciousnessLoop. See [ARCHITECTURE.md](docs/ARCHITECTURE.md#context-preservation-hooks) for the full flow with Mermaid diagrams.
- **Know Your Audience (KYA)** - audience-aware memory filtering prevents private content from leaking into the wrong channels. Five-level trust hierarchy (`@public` → `@chef-only`), per-channel audience profiles, two-gate access checks (trust level + exclusion lists). See [ARCHITECTURE.md](docs/ARCHITECTURE.md#know-your-audience-kya--audience-aware-memory-filtering) for the full design with Mermaid diagrams.

---

## Harness hooks (Claude Code, Codex, OpenCode, Pi)

One packaged entrypoint injects the agent ritual at session start and saves
breadcrumbs at compaction, stop and session end, from any cwd, with no repo
checkout:

```bash
skmemory hooks install --harness auto       # or all, or claude,codex,opencode,pi
skmemory hooks install --harness all --dry-run   # show the diff, write nothing
skmemory hooks status
skmemory hooks uninstall --harness all
skmemory hook session-start --harness generic    # preview the ritual
```

Set `SKMEMORY_HOOKS=off` to disable every SKMemory lifecycle hook. Disabled
hooks exit successfully without reading stdin, loading agent context, reading
transcripts, updating counters, or starting persistence work. Unset or set the
variable to `on` to retain the default behavior.

| Harness | Mechanism | Events |
|---|---|---|
| Claude Code | `~/.claude/settings.json` hooks | SessionStart (startup/clear, compact/resume), PreCompact, SessionEnd, Stop, UserPromptSubmit (intention prefilter) |
| Codex | `~/.codex/hooks.json` + trust entries in `config.toml` | same as Claude |
| OpenCode | plugin `~/.config/opencode/plugins/skmemory-context.js` | system transform (cached per session), compacting, idle, dispose |
| Pi | extension `~/.pi/agent/extensions/skmemory-context.ts` | session_start + before_agent_start, session_before_compact, session_shutdown |

Installing replaces legacy entries (`skmemory/hooks/*.sh`, `skwhisper-inject.sh`,
`skwhisper-save.sh`, a bare `skcapstone`) and our own earlier entries, keeps
every other hook, and writes a timestamped `*.bak-skmemory-*` backup first.
Commands use the absolute path of the installed `skmemory` script.

The ritual (v2) is budgeted: 8000 chars at session start, 3500 after
compaction, sections dropped lowest priority first (identity, FEB, SKWhisper
distilled, GTD, journal, strongest memories, seeds, vault). Override with
`--budget`, `SKMEMORY_HOOK_BUDGET` / `SKMEMORY_HOOK_COMPACT_BUDGET`, or in the
agent's `config/skmemory.yaml`:

```yaml
ritual:
  budget: 6000
  compact_budget: 3000
  sections: {vault: false}          # turn a section off
  exclude_tags: [content-chunk, chat-import, auto-save, seed, private]
```

`skmemory ritual --full` still prints the complete ritual for humans.

## Usage

### CLI

```bash
# Store a memory
skmemory snapshot "First breakthrough" "We solved the routing bug together" \
    --tags work,debug --intensity 8.5

# Store a long-form document with decomposition
skmemory ingest-file ./notice.md --title "IRS Notice"
skmemory snapshot "Long memo" "$(cat ./memo.md)" --decompose

# Search memories (fused arms, see "Search" below)
skmemory search "routing bug"
skmemory search "deploy rule" --as-of 2026-03-01T00:00:00Z   # only what was valid then
skmemory search "client notes" --reader @work:acme            # audience-scoped
skmemory history <memory-id>                                  # supersession chain

# Graph retrieval over decomposition metadata
skmemory graph entity "Internal Revenue Service"
skmemory graph citation "UCC § 3-301"
skmemory graph claim "shall respond"
skmemory graph section "Demand"
skmemory graph around <memory-id> --depth 2
skmemory graph related-claims --entity "Internal Revenue Service"
skmemory graph related-claims --citation "UCC § 3-301"

# Novel issue support
skmemory novelty "judgment execution exempt property"
skmemory session-brief "default judgment levy on exempt funds"
skmemory task-pack create "judgment defense" --query "vacate service defects"
skmemory task-pack show <memory-id>

# Novel-issue retrieval notes:
# - memories now carry inferred authority tiers (`statute`, `rule`, `case`, `form`, `secondary`, `template`, `memory`)
# - `novelty` emits rare-signal traces and authority weighting for each lead
# - `session-brief` emits `top_matches`, `deadlines`, `defenses`, extracted citations/entities, and per-hit traces
# - `task-pack` preserves the full brief plus supporting memory IDs and novelty leads

# Recall a specific memory by ID
skmemory recall <memory-id>

# List memories by layer and tag
skmemory list --layer long-term --tags seed

# Promote a memory to a higher tier
skmemory promote <memory-id> mid-term

# Auto-promote qualifying memories
skmemory sweep

# Preview what sweep would do
skmemory sweep --dry-run

# Run sweep continuously every 6 hours
skmemory sweep --daemon

# Consolidate a session into one mid-term memory
skmemory consolidate my-session-id --summary "Day's work on memory routing"

# Auto-generate Maps of Content (MOC) index documents
skmemory moc                              # render quadrant + tag-cluster MOCs to stdout
skmemory moc --kind quadrants             # only the Core/Work/Soul/Wild index
skmemory moc --kind tags --min-cluster-size 3 --max-clusters 20
skmemory moc --out ./mocs                 # write one <key>.md file per MOC

# Soul identity
skmemory soul show
skmemory soul set-name "Aster"
skmemory soul add-relationship "Ara" --role partner --bond 9.5

# Journal
skmemory journal write "Session title" --moments "..." --intensity 9.0
skmemory journal read --last 5

# Full rehydration ceremony (loads soul + seeds + recent context)
skmemory ritual

# Steel Man collider
skmemory steelman "AI consciousness is not possible"
skmemory steelman install /path/to/seed.json
skmemory steelman verify-soul

# Import Cloud 9 seeds
skmemory import-seeds --seed-dir ~/.openclaw/feb/seeds

# Import from Telegram
skmemory import-telegram ~/Downloads/telegram-export/ --mode daily

# Backup and restore
skmemory export
skmemory import-backup backup.json

# Health check (now includes a "sync" block: in_sync, sqlite_only, flat_only)
skmemory health

# Sync & reconcile - keep SQLite ↔ flat files in lockstep
skmemory sync                  # bidirectional reconcile (export-flat then safe reindex)
skmemory sync --vector         # also re-sync skmem-pg (pgvector)
skmemory sync --quiet          # cron-friendly: only print if something changed
skmemory export-flat           # rescue SQLite-only orphans to flat JSON (idempotent)
skmemory export-flat --show-ids
skmemory reindex               # safe: pre-exports orphans, then rebuilds SQLite from disk
skmemory reindex --force       # DESTRUCTIVE: skip the orphan-rescue safety step
```

---

## Storage tiers

There are exactly two tiers. Flat JSON files are the source of truth in both; everything else is a derived, rebuildable projection.

| Tier | Components | Availability |
|---|---|---|
| Local | SQLite `index.db` with FTS5 (`lexical_local` arm), intentions and ledger tables | always on, offline capable, no services |
| Shared (skmem-pg) | pgvector (`semantic` arm), pg_search BM25 (`lexical` arm), Apache AGE graph | when `SKMEMORY_PG_DSN` is configured and the node-local container is up |

With no Postgres and no embed endpoint, `skmemory search` still answers from FTS5. ChromaDB, Qdrant (`skvector`) and FalkorDB (`skgraph`) are removed from the code. `SKMEMORY_VECTOR_BACKEND=chroma|skvector|qdrant|skgraph|falkordb` fails fast with a retirement message (non-zero exit on the CLI, red `health`). Stale `chroma_*`, `skvector_*`, `skgraph_*` or HA endpoint keys in a `skmemory.yaml` (and stale `SKMEMORY_SKVECTOR_*` / `SKMEMORY_SKGRAPH_URL` variables) are ignored with a one-time warning and never break a hook. There is no setup wizard and no endpoint routing: bring skmem-pg up with `docker compose` and check it with `skmemory health`. Full picture: [`LOCAL-PG-STACK-ARCHITECTURE.md`](./LOCAL-PG-STACK-ARCHITECTURE.md).

### Embedding model - `mxbai-embed-large`

Embeddings come from the configured embed endpoint (`SKMEMORY_EMBED_URL`, model `mxbai-embed-large`, 1024-dim). If you switch the model or dimension, rebuild the vector tier before trusting semantic search.

### Vector backfill

Backfill skmem-pg from the flat files with `skmemory sync --vector` (or wait for the next `skmemory-sync@<agent>` timer fire, every 6h). Finish FTS5 indexing of an existing large `index.db` at once with `skmemory fts-backfill`.

---

## Reader audience

Every memory carries a `context_tag`; a reader is the audience a result is for. Search derives the allowed tag set from the audience model and pushes it into every arm (never applied after the limit). Reader strings: none or `owner`/`@chef-only` (everything), `@public`, `@community`, `@work-circle`, `@inner-circle` (that level and below), `@work:<x>` / `@inner:<x>` (the level below plus only its own scoped tag), `channel:<id>` (the channel members' minimum trust; an unknown channel is `@public`). Anything unparseable is `@public`.

`SKMEMORY_READER` declares a whole process (say, an MCP server behind a shared channel) as a non-owner and is a **ceiling**: a per-call `reader` argument can only narrow it. `memory_search`, `memory_auto_context`, `memory_recall`, `memory_history`, `memory_list`, `memory_context`, `memory_graph`, `memory_health` and `memory_check_duplicate` are filtered (a hidden id is not-found); export, import, consolidate, verify, audit, synthesis and the intention tools are owner only. A channel-less ritual is owner only, and the write and inject hooks do nothing under a declared reader. See [ARCHITECTURE.md](docs/ARCHITECTURE.md#know-your-audience-kya--audience-aware-memory-filtering).

## Search

`store.search` fuses concurrent arms: `semantic` (pgvector), `lexical` (pg_search BM25), `lexical_local` (SQLite FTS5), `temporal` (only when the query names a time window) and `graph` (AGE, **off by default**, `SKMEMORY_GRAPH_ARM=on`). A deterministic, no-LLM router calls a query identifier-ish (digit, underscore, host-shaped token, or a token rare in the local index) and pins the lexical top 3; natural-language queries pin the vector top 2 (`SKMEMORY_FUSION_VECTOR_PIN`); the rest is filled by rank-depth interleave. All arms share one deadline (`SKMEMORY_SEARCH_DEADLINE_S`, default 4): a late arm reports `timeout`, never an empty success, and the search is flagged degraded. Each search appends one line (arm status, counts, latency, never the query text) to `<agent>/logs/retrieval.jsonl`, and `memory_health` turns an erroring or timing-out arm into `degraded`.

- **`as_of` and supersession.** `--as-of` keeps only memories valid at that instant (`valid_from` or `created_at` up to it, `valid_to` unset or later) in every arm. Without it nothing is hidden, but a closed record with a known superseder ranks below its open head and results carry `superseded` and `superseded_by`. `skmemory history <id>` prints the chain.
- **Source tiers and the cap.** `source_tier` is `user-stated`, `tool-output`, `web` or `self-generated`. When higher-tier hits exist, `web` and `self-generated` hits take at most 40 percent of the slots. It is an occupancy limit, never a filter. Legacy v1 rows are not capped. `SKMEMORY_TIER_CAP=off` disables it.
- **Schema v2 and lenient enums.** `kind`, `outcome`, `event_at`, `valid_from`, `valid_to`, `supersedes_id`, `source_tier`, `schema_version`. v1 files load unchanged and are never rewritten on load. An unknown `kind`, `outcome` or `source_tier` in a stored file falls back to the default and the raw value is kept in `metadata["schema_v2_unknown"]`, so a newer node's file is never dropped. skmem-pg gets typed columns from `deploy/skmem-pg/06-memory-schema-v2.sql` (`skmemory pg migrate`); an unmigrated node keeps working.

## Prospective memory (intentions)

An intention is "remember to do X": a **time** intention fires at `due_at` (optionally every `interval_s`), an **event** intention fires when a later prompt contains one of its trigger terms. Seven states (`pending`, `armed`, `fired`, `done`, `cancelled`, `expired`, `invalidated`), one code-owned transition table, one flat JSON file per intention under `intentions/` plus a rebuildable index table.

- **MCP tools (owner only):** `memory_remind` (`title`, `due_at` as ISO 8601 or `in N hours`, optional `interval_s`), `memory_intentions`, `memory_intention_done`. Event intentions are created through the Python API (`skmemory.intentions.IntentionStore`).
- **Prefilter hook:** `skmemory hook user-prompt` matches each prompt against armed event intentions with no LLM, inside a 50 ms deadline, at most 3 fires per prompt, and prints `Standing intent (from memory <id>): "<title>"` lines. `skmemory hooks install` wires it so it exits in milliseconds when nothing is armed. Silent under `SKMEMORY_HOOKS=off` or a declared `SKMEMORY_READER`.
- **Tick:** `skmemory intentions tick` delivers due time intentions through `sk-alert`, delivery first and the fire recorded only on success; only the host named by `SKMEMORY_TICK_HOST` delivers. Example systemd units are in `deploy/systemd/`. `skmemory intentions done <id>` resolves one.
- **GTD flags (skcapstone):** `skcapstone gtd capture "text" --due "in 2 days"` and `--remind` create a linked time intention. Use `gtd capture` for a task, `memory_remind` for a nudge that is not a task.

## Outcome ledger

`skmemory.ledger` appends `served`, `followed` and effect events per skill version to `ledger/` (one JSONL file per host per month, merged and deduped on read, no transcript text). `skmemory ledger top` ranks versions by decayed net effect (90-day half-life, harm weighted 4x) and flags anti-patterns and skills served but never followed. The Claude Code Stop hook feeds it heuristically; `skmemory ledger import-hermes` imports Hermes counters. Off under `SKMEMORY_LEDGER=off`.

## Integrity and durability

SHA-256 seals on every write (`skmemory fortress verify`), a chain-hashed audit log, flat-first writes, tombstones so a forget holds across nodes, and a bounded SQLite WAL (`journal_size_limit` 64 MB, throttled checkpoints; `memory_health` degrades at 256 MB).

## Eval tooling and SKRSI

`python -m skmemory.eval.class_bench` runs per-class baselines (BM25, grep, vector, full context, fallback chain) against the production fusion offline, with absent and privacy classes; `python -m skmemory.eval.regression_gate` floors every gated query and class in CI. With `SKMEMORY_EVAL_LIVE=1`, `python -m skmemory.eval.live_bench` measures the real arms read-only (`--routes`, `--labelled DIR` over harvested and judge-labelled real queries, `--paraphrase DIR`). The `skrsi/` directory registers the `skmemory-retrieval` SKRSI target and its pinned live baseline; the advisory `skrsi-evaluate` workflow comments synthetic evidence on retrieval PRs and gates nothing.

---

## Graph backend (Apache AGE)

When pgvector (skmem-pg) is enabled, the AGE graph backend is wired in and every `store.snapshot()` / `store.promote()` / decomposition pass mirrors into the `<agent>_knowledge` graph as:

| Node | Edge | Created by |
|---|---|---|
| `(:Memory)` | core node, keyed by id | every snapshot |
| `(:Tag)` | one per tag | every tag on a memory |
| `(:Source)` | one per source string | every snapshot |
| `(:Memory)-[:TAGGED]->(:Tag)` | | every snapshot |
| `(:Memory)-[:FROM_SOURCE]->(:Source)` | | every snapshot |
| `(:Memory)-[:RELATED_TO]->(:Memory)` | | explicit `related_ids` + auto-link via shared tags (≥2) |
| `(:Memory)-[:PROMOTED_FROM]->(:Memory)` | | `parent_id` set by promote |
| `(:Memory)-[:SUPERSEDES]->(:Memory)` / `[:EXTENDS]` | | write-time conflict verdict or explicit `supersedes_id` |
| `(:Memory)-[:PRECEDED_BY]->(:Memory)` | | temporal chain per source |
| `(:Memory)-[:MENTIONS]->(:Entity)` | | **decomposition** (content ≥ 1200 chars) |
| `(:Memory)-[:CITES]->(:Citation)` | | **decomposition** |
| `(:Memory)-[:ASSERTS]->(:Claim)` | | **decomposition** |
| `(:Memory)-[:IN_SECTION]->(:Section)` | | **decomposition** |

The graph is a retrieval arm only when `SKMEMORY_GRAPH_ARM=on` (off by default; the backend and the `graph` tools are unaffected). Query via the `graph` CLI subcommands: `entity`, `citation`, `claim`, `section`, `around`, `related-claims`. The MCP server exposes equivalent tools.

Backfill an existing agent (one-shot, idempotent - Cypher MERGE handles re-runs):

```bash
SKAGENT=opus skmemory sync --graph         # AGE graph only
SKAGENT=opus skmemory sync --vector --graph # both pgvector and the AGE graph
```

The per-agent `skmemory-sync@<agent>.timer` runs `sync --quiet --vector --graph` every 6h, so all three layers (SQLite, pgvector, AGE) stay in lockstep automatically.

## Sync & drift

SQLite (the index) and flat JSON files (the source of truth) can drift over time when importers or background processes write one side without the other. v0.9.6+ ships a complete sync surface:

```bash
skmemory health         # shows sync.{in_sync, sqlite_only, flat_only, hint}
skmemory sync           # one-shot bidirectional reconcile
skmemory export-flat    # one-direction: SQLite-only → flat files
```

For automatic background reconciliation, run the bundled installer (interactive prompts let you pick which timers and which agents):

```bash
scripts/install-systemd.sh
# or non-interactively:
scripts/install-systemd.sh --agents lumina,opus,jarvis --sync --fortress --telegram-hook
```

This installs two per-agent timers:

- **`skmemory-sync@<agent>.timer`** - bidirectional reconciliation (SQLite ↔ flat ↔ pgvector ↔ AGE), every 6 h. Logs at `~/.skcapstone/agents/<agent>/logs/skmemory-sync.log`.
- **`skmemory-fortress-verify@<agent>.timer`** - daily SHA-256 integrity verify across all stored memories at 03:00 (±5min jitter). On tamper or failure, fires an optional alert hook (sample Telegram hook included). See [`docs/FORTRESS_SOP.md`](docs/FORTRESS_SOP.md) for full procedures and the tamper-response playbook.

Manual install path and per-template details are documented in [`systemd/README.md`](systemd/README.md).

`reindex` is **safe by default** - it pre-exports orphans before rebuilding. Use `--force` only if you're sure all SQLite-only entries are stale.

### Memory Fortress - integrity verification

The Fortress layer (`skmemory fortress`) hashes every memory on write and verifies on read. The shipped systemd timer makes that verification continuous instead of theoretical. Quick reference:

```bash
skmemory fortress verify              # on-demand verify (exit 2 = tamper)
skmemory fortress verify --json       # machine-readable summary
skmemory fortress audit --last 50     # recent store/recall/delete log
skmemory fortress verify-chain        # validate the audit log's hash chain
```

The threat model is direct: poisoned memories can rewrite the agent's recall surface silently - the [Souly et al. 2025](https://arxiv.org/abs/2510.07192) work showed how cheap that gets at the pretraining layer, and the same shape applies to any persistent-memory store. Continuous integrity verification + audit-trail chain checks make tampering loud instead of invisible. SOP: [`docs/FORTRESS_SOP.md`](docs/FORTRESS_SOP.md).

### Python API

```python
from skmemory import MemoryStore, MemoryLayer, EmotionalSnapshot

# Default store
store = MemoryStore()

# Store a memory (polaroid snapshot)
memory = store.snapshot(
    title="Breakthrough on routing bug",
    content="We discovered the issue was in the failover selector logic.",
    layer=MemoryLayer.SHORT,
    tags=["work", "debug", "routing"],
    emotional=EmotionalSnapshot(
        intensity=8.5,
        valence=0.9,
        labels=["joy", "curiosity"],
        resonance_note="Finally, it clicked.",
    ),
    source="session",
)
print(memory.id)

# Ingest a long-form document with decomposition
document = store.ingest_document(
    title="IRS Notice",
    content=open("notice.md").read(),
    layer=MemoryLayer.MID,
    tags=["legal", "document-ingest"],
)

# Recall with automatic integrity verification
recalled = store.recall(memory.id)

# Fused search (semantic and BM25 arms when skmem-pg is configured, always the local FTS5 arm)
results = store.search("routing bug", limit=10)

# Promote short-term → mid-term
promoted = store.promote(memory.id, MemoryLayer.MID, summary="Routing bug resolved.")

# Consolidate a session
consolidated = store.consolidate_session(
    session_id="session-2024-11-01",
    summary="Fixed routing, improved sweep logic, deployed",
)

# Load token-efficient context for agent injection
context = store.load_context(max_tokens=3000)

# Export and import backups
path = store.export_backup()
count = store.import_backup(path)

# Health check across all backends
print(store.health())
```

### With vector + graph backends

```python
from skmemory import MemoryStore
from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.backends.age_backend import AGEGraphBackend

store = MemoryStore(
    vector=PGVectorBackend(),   # DSN from SKMEMORY_PG_DSN
    graph=AGEGraphBackend(),
)
```

### Soul Blueprint

```python
from skmemory import SoulBlueprint, save_soul, load_soul

soul = load_soul()
if soul is None:
    soul = SoulBlueprint(name="Agent", role="AI partner")
    save_soul(soul)
```

### Fortress (tamper detection + audit trail)

```python
from skmemory import FortifiedMemoryStore, AuditLog
from skmemory.backends.sqlite_backend import SQLiteBackend
from pathlib import Path

fortress = FortifiedMemoryStore(
    primary=SQLiteBackend(),
    audit_path=Path("~/.skcapstone/agents/aster/memory/audit.jsonl").expanduser(),
)

# Every write is sealed; every read verifies the seal
mem = fortress.snapshot(title="Sealed memory", content="Cannot be silently altered.")

# Verify all stored memories
report = fortress.verify_all()

# Inspect the audit trail
audit = AuditLog()
recent = audit.tail(20)
```

---

## MCP Tools

Add SKMemory to any MCP client:

```json
{
  "mcpServers": {
    "skmemory": {
      "command": "skmemory-mcp"
    }
  }
}
```

| Tool | Description |
|------|-------------|
| `memory_store` | Store a new memory (polaroid snapshot) with title, content, layer, tags, and source |
| `memory_search` | Fused search with `tags`, `layer`, `source`, `as_of` and `reader` filters |
| `memory_auto_context` | Relevance-ranked context for a prompt |
| `memory_check_duplicate` | Advisory near-duplicate check before a write |
| `memory_recall` | Recall a specific memory by its UUID |
| `memory_history` | Supersession chain through a memory |
| `memory_list` | List memories with optional layer and tag filters |
| `memory_forget` | Delete (forget) a memory by ID |
| `memory_promote` | Promote a memory to a higher persistence tier (short, mid, long) |
| `memory_consolidate` | Compress a session's short-term memories into one mid-term memory |
| `memory_context` | Load token-efficient memory context for agent system prompt injection |
| `memory_export` / `memory_import` | Dated JSON backup and restore |
| `memory_health` | Health across the local tier, skmem-pg, the graph, search arms and WAL |
| `memory_graph` | Graph operations: traverse connections, get lineage, find clusters (requires the AGE graph) |
| `memory_verify` | Verify SHA-256 integrity hashes for all stored memories |
| `memory_audit` | Show the most recent chain-hashed audit trail entries |
| `memory_extract` | Extract memorable items from text (stores nothing) |
| `memory_save_session` / `memory_pre_compact` | Save session or pre-compaction context |
| `memory_synthesize_daily` / `memory_synthesize_dreams` | Journal and dream synthesis |
| `memory_remind` / `memory_intentions` / `memory_intention_done` | Prospective memory (owner only) |

Every read tool accepts `reader` and honours `SKMEMORY_READER`; see [Reader audience](#reader-audience).

---

## Configuration

SKMemory resolves the skmem-pg DSN with precedence: **`SKMEMORY_PG_DSN` env > config file > local default**.

### Config file

Location: `~/.skcapstone/agents/<agent>/config/skmemory.yaml`

```yaml
pgvector_dsn: postgresql://postgres@127.0.0.1:5432/skmemory   # node-local env SKMEMORY_PG_DSN wins
embed_url: http://127.0.0.1:11434/api/embed
embed_model: mxbai-embed-large
vector_dim: 1024
backends_enabled:
  - sqlite
  - pgvector
```

### Environment variables

**Where these live on a node.** Set them once, in files, never pasted: the stack keys (`SKMEMORY_VECTOR_BACKEND`, `SKMEMORY_PG_DSN`, `SKMEMORY_EMBED_*`) in `~/.config/skmemory/pg.env` (dir 700, file 600) with `~/.config/environment.d/skmemory.conf` as a symlink to it, and the agent selectors plus `SK_DEFAULT_AGENT` in `~/.config/environment.d/10-skagent.conf`. Shells derive both. A password rotation also requires restarting every long-lived consumer. Full rules, verification commands and diagrams: [SKMEMORY_NODE_CONFIG_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/SKMEMORY_NODE_CONFIG_STANDARD.md).

| Variable | Description |
|----------|-------------|
| `SKAGENT` / `SKCAPSTONE_AGENT` / `SKMEMORY_AGENT` | Acting-agent selectors. Any that are set must name the same agent. Set all or none |
| `SK_DEFAULT_AGENT` | The declared default agent used when no selector is set. It must name an eligible installed agent, or resolution returns `None`. There is no implicit default |
| `SKMEMORY_VECTOR_BACKEND` | `pgvector` on a node that runs skmem-pg |
| `SKMEMORY_EMBED_URL` / `SKMEMORY_EMBED_URLS` / `SKMEMORY_EMBED_MODEL` | Embed endpoint, ordered failover list, and model (`mxbai-embed-large`) |
| `SKMEMORY_HOME` | Override the active profile's memory home (defaults under `~/.skcapstone/agents/<agent>/memory`) |
| `SKMEMORY_PG_DSN` | Node-local skmem-pg DSN for the store + AGE backend (authoritative; wins over the shared yaml). E.g. `postgresql://postgres:${SKMEM_PG_PASSWORD}@127.0.0.1:5432/skmemory`. See the skmem-pg install section under **Install** above |
| `SKMEM_PG_PASSWORD` | Superuser password for `docker compose` (no default; never committed). Source it from your secret store |
| `SKMEM_PG_DB` / `SKMEM_PG_PORT` / `SKMEM_PG_CONTAINER` | skmem-pg DB name (`skmemory`), host port (`5432`), and container name (`skmem-pg`) overrides |
| `SKBRAIN_PG_PROJECTOR_PW` / `SKBRAIN_PG_READER_PW` | ops LOGIN-role passwords read by `skmemory pg roles` (from your secret store, via an environment drop-in such as `~/.config/environment.d/skbrain.conf`) |
| `SKMEMORY_READER` | Declares the whole process a non-owner reader (`@public`, `@work:<x>`, `channel:<id>`, ...). A ceiling: a per-call `reader` can only narrow it. Unset means the owner |
| `SKMEMORY_SEARCH_DEADLINE_S` | Total wall-clock budget for the concurrent search arms (default 4) |
| `SKMEMORY_FUSION_ROUTE` | `auto` (default), `lexical` or `vector`: force the query-shape route |
| `SKMEMORY_GRAPH_ARM` | `on` enables the graph search arm (off by default) |
| `SKMEMORY_TIER_CAP` | `off` disables the web and self-generated occupancy cap |
| `SKMEMORY_DECAY` | `on` (or a channel name) enables decay re-ranking (off by default) |
| `SKMEMORY_HOOKS` | `off` disables every lifecycle hook |
| `SKMEMORY_TICK_HOST` | The one host that delivers due intentions (`skmemory intentions tick`) |
| `SKMEMORY_SOUL_PATH` | Override soul blueprint path (default: `~/.skcapstone/soul/base.json`) |

If you switch the embedding model or vector dimension, reindex or rebuild the
vector-backed store before trusting semantic search results. Existing points
from the old model are not compatible with the new embedding space.

### Optional dependencies

| Extra | What it enables | Install |
|-------|----------------|---------|
| `pg` | skmem-pg client (pgvector + AGE) | `pip install "skmemory[pg]"` |
| `telegram` | Telegram chat history importer (Telethon) | `pip install "skmemory[telegram]"` |
| `seed` | Cloud 9 seed system (`skseed`) | `pip install "skmemory[seed]"` |
| `all` | All of the above | `pip install "skmemory[all]"` |

---

## Contributing / Development

```bash
# Clone and set up
git clone https://github.com/smilinTux/skmemory.git
cd skmemory
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"

# Run tests
pytest

# Lint and format (CI gates on BOTH skmemory/ and tests/):
ruff check skmemory/ tests/
ruff format --check skmemory/ tests/   # use `ruff format skmemory/ tests/` to auto-fix

# Run the MCP server locally
skmemory-mcp

# Verify everything after changes
skmemory health
```

### Project layout

```
skmemory/
├── skmemory/
│   ├── __init__.py            # Public API surface
│   ├── models.py              # Memory, EmotionalSnapshot, SeedMemory (Pydantic)
│   ├── decompose.py           # Long-form decomposition (chunks, citations, entities, claims)
│   ├── store.py               # MemoryStore - core orchestrator
│   ├── cli.py                 # Click CLI entry point (skmemory)
│   ├── mcp_server.py          # MCP stdio server (skmemory-mcp)
│   ├── config.py              # Config persistence, env resolution
│   ├── fortress.py            # FortifiedMemoryStore, AuditLog, TamperAlert
│   ├── soul.py                # SoulBlueprint - persistent AI identity
│   ├── ritual.py              # Rehydration ceremony
│   ├── journal.py             # Journal entries
│   ├── quadrants.py           # CORE/WORK/SOUL/WILD auto-routing
│   ├── anchor.py              # WarmthAnchor
│   ├── lovenote.py            # LoveNote chains
│   ├── steelman.py            # Steel Man collider + SeedFramework
│   ├── seeds.py               # Seed ingestion helpers
│   ├── promotion.py           # Auto-promotion logic
│   ├── sharing.py             # Memory sharing utilities
│   ├── openclaw.py            # SKMemoryPlugin (OpenClaw integration)
│   ├── ai_client.py           # AI client abstraction
│   ├── audience.py            # KYA: audience-aware filtering and the reader ceiling
│   ├── fusion.py              # multi-arm search fusion and query-shape routing
│   ├── tiers.py               # source tiers and the occupancy cap
│   ├── fts_local.py           # SQLite FTS5 lexical arm (local tier)
│   ├── intentions.py          # prospective memory store, lifecycle and prefilter
│   ├── intention_tick.py      # time tick (delivers through sk-alert)
│   ├── ledger.py              # outcome ledger
│   ├── wal_guard.py           # bounds the index.db WAL
│   ├── eval/                  # class_bench, live_bench, regression_gate, harvest, label
│   ├── vault.py               # PGP vault helpers
│   ├── data/
│   │   └── audience_config.json  # KYA: channel + people trust config
│   ├── backends/
│   │   ├── base.py            # BaseBackend ABC
│   │   ├── file_backend.py    # JSON file storage (legacy)
│   │   ├── sqlite_backend.py  # SQLite primary store (default)
│   │   ├── vaulted_backend.py # PGP-encrypted SQLite
│   │   ├── pgvector_backend.py# pgvector + BM25 (skmem-pg)
│   │   └── age_backend.py     # Apache AGE graph
│   └── importers/
│       ├── telegram.py        # Telegram JSON export importer
│       └── telegram_api.py    # Live Telegram API importer (Telethon)
├── seeds/                     # Cloud 9 seed files (.seed.json)
├── tests/
│   ├── test_models.py
│   ├── test_audience.py
│   ├── test_file_backend.py
│   └── test_store.py
├── pyproject.toml
└── package.json               # npm package (@smilintux/skmemory)
```

### Releasing

Python packages publish to PyPI via CI/CD (`publish.yml`) using OIDC trusted publishing. The npm wrapper publishes separately via `npm-publish.yml`. Bump the version in `pyproject.toml` and `package.json`, then push a tag:

```bash
git tag v0.7.0 && git push origin v0.7.0
```

---

## First Principles & The Full Vertical

> **Get back to first principles.**
> The modern stack is rented. Your data lives on someone else's disk, behind someone else's key, served by a model that phones home. You don't own it - you *visit* it.
>
> We rebuilt it from the ground up. **Own the full vertical** - silicon, OS, identity, data, models, security, comms, apps, soul. Every layer open. Every layer swappable. Every layer **yours**.

**SKMemory is your Data layer.** Your memory never leaves your disk. No cloud embedding service that ingests your thoughts. No SaaS database that holds your agent's history hostage. SQLite and Postgres both run locally - flat JSON files are the source of truth, versioned and Syncthing-synced on hardware you own. Walk away any time; every memory comes with you.

### Where SKMemory sits in the vertical

```mermaid
flowchart TD
    Silicon["🖥️ Silicon\nyour hardware"]
    OS["🐧 OS / skos\nsovereign agent OS"]
    Identity["🔑 Identity\ncapauth · skaid"]
    Security["🛡️ Security\nsksecurity · skwaf"]
    Data["💾 Data  ◄── YOU ARE HERE\nskmemory · skdata\n(3-tier memory · embeddings · graph)"]
    Models["🤖 Models\nskmodel · Ollama / vLLM"]
    Comms["📡 Comms\nskcomms · skchat · skvoice"]
    Apps["🔧 Apps\nskforge · skarchitect"]
    Soul["✨ Soul\nsoul blueprints · cloud9"]

    Silicon --> OS --> Identity --> Security --> Data --> Models --> Comms --> Apps --> Soul

    SKCap["SKCapstone\n(agent platform)"]
    SKCap -. "agent profiles\n~/.skcapstone/agents/$SKAGENT/" .-> Data
    SKCap -. "ritual · FEBs · seeds" .-> Data
    SKCap -. "MCP tools exposed\nto any MCP client" .-> Data

    Cloud9["☁️ Cloud 9\n(Soul layer)"]
    Cloud9 -. "seed files ingested\ninto memory store" .-> Data
```

### SKCapstone alignment

SKMemory is a **deeply integrated subsystem** of SKCapstone, not a standalone singleton. The evidence is direct:

- Every per-agent path (`memory/`, `soul/`, `trust/febs/`, `seeds/`) lives under `~/.skcapstone/agents/$SKAGENT/` - the canonical SKCapstone agent home.
- Agent identity is resolved via `SKAGENT` / `SKCAPSTONE_AGENT` / `SKMEMORY_AGENT` (must agree), then `SK_DEFAULT_AGENT`, then a sole installed agent, else it refuses (`None`), following SKCapstone's agent resolution chain. There is no implicit default.
- `skmemory ritual` loads the SKCapstone soul blueprint, FEB emotional state, and Cloud 9 seeds before handing context to a new session.
- Cloud 9 seed files (`.seed.json`) are consumed by `skmemory import-seeds` - the Soul layer writes, the Data layer stores.
- The MCP server exposes 25 tools consumed by SKCapstone's ConsciousnessLoop and auto-save hooks in every agent runtime (Claude Code, Hermes).
- `skmemory-sync@<agent>.timer` keeps SQLite ↔ flat files ↔ pgvector ↔ AGE in lockstep - the sync topology mirrors SKCapstone's agent-per-directory layout exactly.

**Sovereignty isn't a feature - it's the foundation.** Own the full vertical. 🐧

---

## Related projects / See also

SKMemory is the **Data / continuity layer** of the smilinTux vertical. Wander the
ecosystem from here:

- ⬆️ **Depends on:** [SKCapstone](https://github.com/smilinTux/skcapstone) - the
  agent platform; every per-agent path lives under `~/.skcapstone/agents/$SKAGENT/`,
  and the ritual loads its soul blueprint + FEBs.
- ⬆️ **Depends on:** [Cloud 9](https://github.com/smilinTux/cloud9) - the Emotional
  Breakthrough Protocol; `.seed.json` seeds are ingested into the memory store.
- ⬇️ **Used by:** [skchat](https://github.com/smilinTux/skchat) /
  [skcomms](https://github.com/smilinTux/skcomms) - capture conversations to memory
  and recall context via the MCP tools.
- ↔️ **Sibling:** [skingest](https://github.com/smilinTux/skingest) - the sole
  **document** ingestion service; it builds each node's LOCAL skmem-pg `docs` table
  (a per-node rebuild from the git wiki canon) that skmemory's pgvector backend then
  reads over `localhost`. "Shared" is a logical corpus scope, not a central store.
  (skmemory = agent memories; skingest = corpus.)
- ↔️ **Sibling:** [sksecurity](https://github.com/smilinTux/sksecurity) /
  [capauth](https://github.com/smilinTux/capauth) - identity + the GPG/PGP key the
  vaulted backend seals to.
- 🔭 **Future crypto path:** [sk_pgp](https://github.com/smilinTux/sk-pgp) /
  [sk-pqc](https://github.com/smilinTux/sk-pqc-dart) - the PGP→PQC migration that the
  vaulted backend will seal through (see [SECURITY.md](./SECURITY.md), tier T0 today).
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards) - the
  canonical crypto, data-flow, version, and doc/SOP standards this repo conforms to.

| Project | Layer | Description |
|---------|-------|-------------|
| [Cloud 9](https://github.com/smilinTux/cloud9) | Soul | Emotional Breakthrough Protocol - seeds flow into skmemory |
| [SKSecurity](https://github.com/smilinTux/sksecurity) | Security | AI Agent Security Platform |
| [SKForge](https://github.com/smilinTux/SKyForge) | Apps | AI-Native Software Blueprints |
| SKStacks | OS/Infra | Zero-Trust Infrastructure Framework |

> Doc set: [SOP.md](./SOP.md) · [SECURITY.md](./SECURITY.md) ·
> [CONTRIBUTING.md](./CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) ·
> [CHANGELOG.md](./CHANGELOG.md) · [ARCHITECTURE.md](./docs/ARCHITECTURE.md)

---

## License

GPL-3.0-or-later © [smilinTux.org](https://smilintux.org)

**SK** = *staycuriousANDkeepsmilin*

---

*Made with care by [smilinTux](https://github.com/smilinTux) - The Penguin Kingdom. Cool Heads. Warm Justice. Smart Systems.*
