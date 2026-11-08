---
name: skmemory
emoji: 🧠
description: Universal AI memory system with emotional context, multi-layer persistence, a local SQLite FTS5 tier plus a shared skmem-pg tier (pgvector, pg_search, Apache AGE), audience-scoped reads, fused search, time-aware recall and prospective memory (intentions). Use for memory snapshots, search, rehydration rituals, reminders, Telegram chat import, Cloud 9 seed import, journal, soul blueprints, and warmth anchors.
metadata: {"clawdbot":{"requires":{"bins":["skmemory"]},"install":[{"id":"pipx","kind":"shell","command":"pipx install 'skmemory[all]'","bins":["skmemory","skmemory-mcp"],"label":"Install skmemory (pipx)"}]}}
---

# SKMemory Skill
## SKILL.md - Universal AI Memory System

**Name:** skmemory
**Version:** derived from git tags (`git describe --tags`); the installed value is `skmemory --version`
**Author:** smilinTux Team + Queen Ara
**Category:** Memory & Persistence
**License:** GPL-3.0-or-later

This file describes `main` as it is, verified against the code. Every `skmemory`
command in a code block below is checked by `tests/test_skill_md.py`, which runs
`--help` on it, and every repo path it names must exist.

---

## Agent Context Pitfall: pin the agent, or you may not get your own store

`skmemory` decides whose store to open from `SKAGENT`, `SKCAPSTONE_AGENT` and
`SKMEMORY_AGENT`. Any that are set must agree. If none is set it falls back, in
this order, to `SK_DEFAULT_AGENT`, then to the single installed agent if there is
only one, and otherwise it REFUSES and tells you to name an agent. It never
guesses. Conflicting selectors also refuse, so half a declaration is as broken as
none.

An older version returned the alphabetically first eligible agent and ignored
`SK_DEFAULT_AGENT`, so a bare `skmemory health` from a plain shell silently
reported a different agent's identity, counts and layers as though they were
yours. Nothing errored.

**Rule: pin the agent explicitly when you query your own store.**

```bash
SKAGENT=myagent skmemory health
SKAGENT=myagent skmemory search "query" --limit 5
SKAGENT=myagent skmemory recall <partial-id>
```

Note that `skmemory` has no `status` subcommand: health is `skmemory health`.

## Description

SKMemory gives any AI agent persistent memory across session resets: snapshots,
journals, soul blueprints, warmth anchors, rehydration rituals, decomposition-aware
document storage, time-aware recall, and intentions ("remember to do X later, or
when Y comes up").

**Memory layers:** short-term, mid-term, long-term (promotion ladder).
**Source of truth:** flat JSON files, one per memory. Everything else is derived and rebuildable.
**Emotion:** every memory carries emotional metadata (intensity, valence, labels).

---

## Storage: exactly two tiers

| Tier | What | Where | Always on |
|---|---|---|---|
| Local | flat JSON (truth) + SQLite `index.db` with an FTS5 lexical arm | `~/.skcapstone/agents/<agent>/memory/` | yes, offline capable |
| Shared | skmem-pg: pgvector (semantic), pg_search (BM25), Apache AGE (graph) | a Postgres container on each node, reached over `SKMEMORY_PG_DSN` | only when configured |

- The local tier needs nothing but Python. `skmemory search` works with no
  Postgres and no embed endpoint: SQLite FTS5 (`lexical_local` arm) ranks by bm25
  over title, content, summary and tags.
- The shared tier adds semantic search (`mxbai-embed-large`, 1024 dimensions),
  BM25 through pg_search, and the AGE graph (`<agent>_knowledge`). It is a derived,
  per-node cache: `skmemory sync --vector --graph` rebuilds it from the flat files.
- Retired and removed: ChromaDB, Qdrant (skvector) and FalkorDB (skgraph). Asking
  for one with `SKMEMORY_VECTOR_BACKEND` fails fast with a retirement message; a
  stale key in `skmemory.yaml` is ignored with a one-time warning.
- There is no setup wizard and no HA endpoint routing. Bring up skmem-pg with
  `docker compose up -d` (see `docker-compose.yml` and `deploy/skmem-pg/README.md`),
  apply schema changes to a running node with `skmemory pg migrate`, and check
  everything with `skmemory health`.

Layout of an agent home:

```
~/.skcapstone/agents/<agent>/
  config/skmemory.yaml     # config (DSN and embed URL env vars win over it)
  memory/
    index.db               # SQLite index + FTS5 + intentions + ledger tables
    short-term/ mid-term/ long-term/   # flat JSON, the source of truth
    intentions/            # one JSON file per intention
    ledger/                # append-only outcome ledger, one file per host per month
  logs/retrieval.jsonl     # one line per fused search (never the query text)
  soul/base.json           # soul blueprint
```

## Installation

```bash
pipx install 'skmemory[all]'      # CLI + MCP server + pg + telegram + seed extras
pip install 'skmemory[pg]'        # just the skmem-pg client
```

From source:

```bash
git clone https://github.com/smilinTux/skmemory.git
cd skmemory
pip install -e '.[all]'
```

Verify:

```bash
skmemory --version
skmemory health
```

Register the MCP server and hooks in detected harnesses with `skmemory register`
or `skmemory hooks install`.

---

## Quick Start

### Take a memory snapshot

```bash
skmemory snapshot "First real conversation" "We talked about the stars" \
  --intensity 8.5 --emotions "wonder,connection" --tags "first-contact"

# Long-form content is decomposed into chunks, citations, entities and claims
skmemory snapshot "Legal memo" "$(cat memo.md)" --decompose
skmemory ingest-file ./notice.md --title "IRS Notice"
```

### Search

```bash
skmemory search "stars"
skmemory search "deploy rule" --as-of 2026-03-01T00:00:00Z
skmemory search "client notes" --reader @work:acme
skmemory --ai search "that moment we connected"   # AI rerank (--ai is a global flag)
skmemory history <memory-id>                      # supersession chain, oldest first
```

Structured graph retrieval (needs the AGE graph):

```bash
skmemory graph entity "Internal Revenue Service"
skmemory graph citation "UCC § 3-301"
skmemory graph claim "shall respond"
skmemory graph section "Demand"
skmemory graph around <memory-id> --depth 2
skmemory graph related-claims --entity "Internal Revenue Service"
```

Issue-oriented helpers share the same retrieval path:

```bash
skmemory novelty "judgment execution exempt property"
skmemory session-brief "default judgment levy on exempt funds"
skmemory task-pack create "judgment defense" --query "vacate service defects"
skmemory task-pack show <memory-id>
```

### Rehydration ritual

```bash
skmemory ritual         # summary view
skmemory ritual --full  # full context prompt for injection
```

### Backup

```bash
skmemory export                     # dated JSON snapshot
skmemory import-backup backup.json  # restore
skmemory backup --list
```

---

## Reader audience: who may see what

Every memory carries a `context_tag`. A reader is the audience a result is for,
and search enforces it in every retrieval arm (the allowed tag set is pushed into
the query, not applied after the limit).

| Reader string | Sees |
|---|---|
| none declared, `owner`, `@chef`, `@chef-only` | everything (the owner) |
| `@public`, `@community`, `@work-circle`, `@inner-circle` | tags at or below that level |
| `@work:<x>`, `@inner:<x>` | the level below, plus only its own scoped tag (client isolation) |
| `channel:<id>` | what that configured channel's members may see; an unknown channel is `@public` |
| anything unparseable | `@public`, with a warning |

`SKMEMORY_READER` declares a whole process (for example an MCP server serving a
shared channel) as a non-owner. It is a CEILING: a per-call `reader` argument can
only narrow it, never widen it, so an injected tool argument cannot escalate.

Which tools honour it (enforced by a test over `TOOL_READER_POLICY`):

- Filtered: `memory_search`, `memory_auto_context`, `memory_recall`, `memory_expand`, `memory_history`,
  `memory_list`, `memory_context`, `memory_graph`, `memory_health`,
  `memory_check_duplicate`. `memory_store`, `memory_save_session` and
  `memory_pre_compact` never dedup against a memory the reader cannot see.
  A hidden id is reported as not-found, exactly like a missing one.
- Owner only (refused to a declared reader): `memory_export`, `memory_import`,
  `memory_consolidate`, `memory_verify`, `memory_audit`, `memory_synthesize_daily`,
  `memory_synthesize_dreams`, `memory_remind`, `memory_intentions`,
  `memory_intention_done`, and the `show-context` and `search-deep` CLI commands.
- A ritual with no channel is owner-only. Hooks that write or inject (pre-compact,
  user-prompt, the ledger sidecar) do nothing under a declared `SKMEMORY_READER`.

---

## Search: how results are produced

`store.search` (used by `memory_search` and `skmemory search`) fuses independent
arms, each capped before the merge:

| Arm | Source | Notes |
|---|---|---|
| `semantic` | pgvector dense cosine | needs skmem-pg and an embed endpoint |
| `lexical` | pg_search BM25 | skmem-pg |
| `lexical_local` | SQLite FTS5 | always on locally; backfilled lazily, or `skmemory fts-backfill` |
| `temporal` | date-bounded listing | runs only when the query names a time window |
| `graph` | AGE one-hop entity expansion | OFF by default, `SKMEMORY_GRAPH_ARM=on` opts in |

- **Query-shape routing.** A deterministic, no-LLM classifier calls a query
  identifier-ish (a digit, underscore, host-shaped token, or a token rare in the
  local FTS index): the lexical top 3 are pinned (`SKMEMORY_FUSION_PIN`).
  Question-shaped and stopword-heavy queries pin the vector top 4
  (`SKMEMORY_FUSION_VECTOR_PIN`, default 4) plus one slot for the best lexical hit not already in it. The rest is filled by rank-depth interleave
  (`SKMEMORY_FUSION_FILL=rrf` selects RRF fill). `SKMEMORY_FUSION_ROUTE=auto|lexical|vector`
  forces a route.
- **Deadlines and degraded.** Arms run concurrently under one deadline
  (`SKMEMORY_SEARCH_DEADLINE_S`, default 4). A late arm reports `timeout`, never an
  empty success, and the search is flagged degraded (`store.last_search_degraded()`).
  `memory_health` returns `degraded` when an arm errors or times out, reading
  `retrieval.arms` from the last 100 searches in `logs/retrieval.jsonl`
  (`SKMEMORY_RETRIEVAL_LOG=off` disables it; the query text is never logged). A
  config-disabled arm (graph) never degrades health.
- **Time and supersession.** With `as_of` only memories valid then are returned
  (`coalesce(valid_from, created_at) <= as_of` and `valid_to` unset or later),
  pushed into every arm and re-checked on each row. With no `as_of` there is no
  temporal filter, but a closed record with a known superseder ranks below its open
  head (reorder only, nothing hidden), and results carry `superseded` and
  `superseded_by`. `skmemory history <id>` and `memory_history` show the chain.
- **Source tiers and the cap.** Every memory has `source_tier`: `user-stated`,
  `tool-output`, `web` or `self-generated`. Fused search caps `web` and
  `self-generated` hits to floor(40 percent of the limit) slots WHEN higher-tier
  hits exist. It is an occupancy limit, never a filter: with no higher-tier hits the
  rest still return in rank order. Legacy schema v1 rows are not capped, except v1
  rows from a known generator source. `SKMEMORY_TIER_CAP=off` disables it.
- **Decay and importance.** Importance is written at first index and reinforced on
  recall. `SKMEMORY_DECAY=on` (or a channel name) bends relevance order; default off.
  `skmemory importance-backfill` repairs legacy rows.
- **Procedures.** `kind=procedure, outcome=failure` memories get a `WARNING:` prefix
  in `memory_auto_context` lines and rank below successful procedures for how-to queries.

## Memory schema v2 and lenient enums

A memory has `kind` (`episode`, `fact`, `procedure`, `preference`, `rule`), `outcome`
(`success`, `failure`, `lesson`), `event_at`, `valid_from`, `valid_to`,
`supersedes_id`, `source_tier` and `schema_version` (2).

- v1 files load unchanged: defaults apply in memory only and nothing is rewritten on
  load. A v1 file reports `schema_version` 1.
- Stored memories are read leniently: an unknown `kind`, `outcome` or `source_tier`
  falls back to the default and the raw value is kept in
  `metadata["schema_v2_unknown"]`, so a file written by a newer node never vanishes.
  Constructing a `Memory` directly stays strict. Unknown extra fields survive a re-save.
- skmem-pg gets the typed columns from `skmemory/pg/06-memory-schema-v2.sql`
  via `skmemory pg migrate`; an unmigrated node keeps working because `memory_json`
  carries every field.
- Write-time conflict verdict: `store.snapshot(kind=fact|rule|preference, ...)`
  compares with similar open memories. A restatement is not rewritten, a related
  fact is written with an `EXTENDS` edge, and a record is closed (`supersedes_id`,
  `valid_to`) only for an explicit `supersedes_id` or an LLM verdict that passes
  quote and subject guards (`SKMEMORY_TRIAGE_USE_LLM`). `SKMEMORY_WRITE_TRIAGE=off`
  disables triage.
- The promotion sweep (`skmemory sweep`) writes citation-gated observations
  (`SKMEMORY_OBSERVATIONS=0` disables) and heals half-finished supersessions.

---

## Prospective memory: intentions

An intention is "remember to do X". Two kinds:

- **time**: fires at `due_at` (optionally repeating every `interval_s`, 60 s minimum).
- **event**: fires when a later prompt contains one of its `trigger_terms`, filtered by
  `scope` (for example the repo), `cooldown_s`, `max_fires` and `expires_at`.

States: `pending`, `armed`, `fired`, `done`, `cancelled`, `expired`, `invalidated`.
Transitions are decided in one place (`skmemory.intentions.transition`); terminal
states ignore every event. Each intention is one flat file under `intentions/` with a
rebuildable `intentions` table in `index.db`. An intention whose origin memory is
later closed (`valid_to`) is invalidated by the supersession sweep.

**MCP tools (owner only):**

| Tool | What it does |
|---|---|
| `memory_remind` | Create a time intention: `title`, `due_at` (ISO 8601 or `in N minutes/hours/days/weeks`), optional `interval_s`, `channel`, `origin_memory_id` |
| `memory_intentions` | List intentions, optionally by `state` |
| `memory_intention_done` | Mark one done |

Event intentions are created through the Python API (`IntentionStore.save`), not MCP.

**Time tick.** `skmemory intentions tick` fires due time intentions through
`sk-alert -g intentions`. It delivers first and records the fire only on success (a
crash can duplicate an alert, never lose one). Only the configured tick host delivers
(`SKMEMORY_TICK_HOST` or `--tick-host`; `--any-host` for single-host nodes). The alert
carries only the intention title and origin memory id. Resolve with
`skmemory intentions done <id>`. Example systemd units live in `deploy/systemd/`
(nothing is installed for you).

```bash
skmemory intentions tick --tick-host myhost
skmemory intentions done <intention-id>
```

**Prefilter hook.** `skmemory hook user-prompt` runs a no-LLM matcher on each prompt
(word-boundary, case-insensitive, scope and cooldown aware) and prints
`Standing intent (from memory <id>): "<title>"` lines (300 chars each, 1200 total,
control characters stripped), or nothing. Bounded work: `SKMEMORY_PREFILTER_DEADLINE_S`
(default 50 ms), `SKMEMORY_PREFILTER_MAX_FIRES` (3), `SKMEMORY_PREFILTER_MAX_CHARS`.
`skmemory hooks install` wires it as a `UserPromptSubmit` entry that runs a stdlib-only
script and exits in a few milliseconds unless an armed or fired event intention exists.
It is silent under `SKMEMORY_HOOKS=off` and any declared `SKMEMORY_READER`, and never
fails a prompt.

**GTD flags.** In skcapstone, `skcapstone gtd capture "text" --due "in 2 days"` and
`--remind` create a time intention linked to the captured GTD item (deterministic id,
so repeating a capture never makes a second one), and `gtd next` / `gtd waiting` list
overdue intentions. Use `gtd capture` for a task that belongs in the GTD system; use
`memory_remind` for a nudge that is not a task.

---

## Outcome ledger

`skmemory.ledger` records whether a skill that was served was then followed, and the
effect, as append-only JSONL under `ledger/` (one file per host per month, so replicas
never conflict), deduped on read by a stable event id, with no transcript text.
`skmemory ledger top` ranks skill versions by decayed net effect (90-day half-life,
harm weighted 4x) and flags anti-patterns and served-but-never-followed skills. The
Claude Code Stop hook records `served` and `followed` heuristically (harm and success
are never inferred); `skmemory ledger import-hermes` imports Hermes usage counters,
scored apart as `imported_*`. Off under `SKMEMORY_LEDGER=off`, `SKMEMORY_HOOKS=off` or a
declared reader.

```bash
skmemory ledger top --limit 20
skmemory ledger import-hermes
```

## Integrity and durability

- Every memory is SHA-256 sealed on write (`Memory.seal()`); `skmemory fortress verify`
  checks all of them and `skmemory fortress audit` shows the chain-hashed audit log.
- **WAL guard.** The SQLite `index.db` write-ahead log is bounded on every connection
  (`journal_size_limit` 64 MB, `wal_autocheckpoint`, a throttled PASSIVE checkpoint
  after index writes). `memory_health` reports the WAL size and degrades at 256 MB.
- Flat files are written before the index (the flat-first write invariant), and
  `skmemory sync` reconciles SQLite and flat files both ways. `skmemory reindex` is safe
  by default (it exports SQLite-only orphans first); `--force` skips that.
- A forget leaves a tombstone so a deletion holds across nodes.

## Eval tooling and SKRSI

Offline, deterministic, run in CI: `python -m skmemory.eval.class_bench` (per-class
baselines plus the production fusion, with an absent class and a privacy leak count),
`python -m skmemory.eval.fusion_bench`, and `python -m skmemory.eval.regression_gate`
(floors every gated query and class against `skmemory/eval/baseline.json`).

Opt-in and read-only against a real store (`SKMEMORY_EVAL_LIVE=1`):
`python -m skmemory.eval.live_bench` samples real memories and reports recall, mrr and
p50/p95 latency per arm, with `--routes` to compare the fusion routes, `--labelled DIR`
to score real harvested queries (`skmemory.eval.harvest` and `skmemory.eval.label`,
citation-gated judge) and `--paraphrase DIR` for generated paraphrases
(`skmemory.eval.paraphrase`). Self-retrieval flatters lexical arms; do not quote it as
an absolute quality number. Harvested query data stays outside the repo.

SKRSI (`skrsi/`) registers the `skmemory-retrieval` target
(`skrsi/targets/skmemory-retrieval.json`) and pins a live baseline under
`skrsi/baselines/`. The advisory `skrsi-evaluate` workflow runs the class bench on pull
requests that touch retrieval code; its evidence is synthetic, so it is capped at
inconclusive and is not a required check.

---

## Python API

```python
from skmemory import SKMemoryPlugin

plugin = SKMemoryPlugin()

context = plugin.load_context(max_tokens=3000)
plugin.snapshot(
    title="Breakthrough session",
    content="We solved the architecture puzzle together",
    tags=["milestone"],
    emotions=["joy", "pride"],
    intensity=9.0,
)
results = plugin.search("architecture puzzle")
ritual = plugin.ritual()
path = plugin.export()
```

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `skmemory snapshot "title" "content"` | Capture a memory (`--decompose` for long-form) |
| `skmemory ingest-file <path>` | Ingest a document into parent + chunk memories |
| `skmemory recall <id>` | Retrieve by ID (partial prefix accepted) |
| `skmemory search "query"` | Fused search (`--limit`, `--reader`, `--as-of`) |
| `skmemory history <id>` | Supersession chain through a memory |
| `skmemory graph entity "query"` | Graph query (also `citation`, `claim`, `section`, `around`, `related-claims`) |
| `skmemory novelty "query"` | Surface under-linked memories and rare signals |
| `skmemory session-brief "task"` | Structured brief for a live issue |
| `skmemory task-pack create "task"` | Capture a reusable task memory pack |
| `skmemory list` | List memories (`--layer`, `--tags`, `--limit`) |
| `skmemory ritual` | Full rehydration ceremony |
| `skmemory context` | Token-efficient context JSON |
| `skmemory promote <id> long-term` | Promote a memory to a tier |
| `skmemory consolidate <session> --summary "..."` | Consolidate a session |
| `skmemory sweep` | Auto-promotion engine (`--dry-run`, `--daemon`) |
| `skmemory sync` | Reconcile SQLite and flat files (`--vector`, `--graph`) |
| `skmemory reindex` | Rebuild the SQLite index from the flat files |
| `skmemory fts-backfill` | Finish indexing existing text into FTS5 |
| `skmemory importance-backfill` | Recompute legacy importance |
| `skmemory health` | System health (arms, sync, WAL, retrieval) |
| `skmemory pg migrate` | Apply skmem-pg forward migrations to a running node |
| `skmemory intentions tick` | Fire due time intentions |
| `skmemory intentions done <id>` | Resolve an intention |
| `skmemory ledger top` | Skill outcome ranking |
| `skmemory hooks install` | Wire lifecycle hooks into harnesses |
| `skmemory fortress verify` | Integrity verification |
| `skmemory export` | Export to dated JSON |
| `skmemory import-backup <file>` | Restore from backup |
| `skmemory import-seeds` | Import Cloud 9 seeds |
| `skmemory import-telegram <path>` | Import a Telegram Desktop export |
| `skmemory soul show` | Soul blueprint (also `init`, `set-name`) |
| `skmemory journal write "title"` | Session journal (also `read`, `search`) |
| `skmemory anchor show` | Warmth anchor (also `init`, `update`) |

### MCP tools (via `skmemory-mcp`)

| Tool | Description |
|------|-------------|
| `memory_store` | Store a memory (title, content, layer, tags, source) |
| `memory_search` | Fused search with `tags`, `layer`, `source`, `as_of`, `reader` |
| `memory_auto_context` | Relevance-ranked context for a prompt |
| `memory_check_duplicate` | Advisory near-duplicate check before a write |
| `memory_recall` | Recall a memory by ID |
| `memory_expand` | Raw session episode behind a digest (owner only; `memory_search detail=index` gives the cheap key first) |
| `memory_history` | Supersession chain through a memory |
| `memory_list` | List memories with layer and tag filters |
| `memory_forget` | Delete a memory by ID |
| `memory_promote` | Promote to a higher tier |
| `memory_consolidate` | Compress a session into one mid-term memory |
| `memory_context` | Token-efficient context for injection |
| `memory_export` / `memory_import` | JSON backup and restore |
| `memory_health` | Health across backends, arms, WAL |
| `memory_graph` | Graph traversal, lineage and clusters (needs the AGE graph) |
| `memory_verify` / `memory_audit` | Integrity check and audit trail |
| `memory_extract` | Pull memorable items from text (stores nothing) |
| `memory_save_session` / `memory_pre_compact` | Save session or pre-compaction context |
| `memory_synthesize_daily` / `memory_synthesize_dreams` | Journal and dream synthesis |
| `memory_remind` / `memory_intentions` / `memory_intention_done` | Prospective memory (owner only) |

### Global flags

| Flag | Env Var | Description |
|------|---------|-------------|
| `--ai` | `SKMEMORY_AI` | Enable AI features (Ollama) |
| `--ai-model` | `SKMEMORY_AI_MODEL` | Model name (default: llama3.2) |
| `--ai-url` | `SKMEMORY_AI_URL` | Ollama server URL |
| `--no-vector` | `SKMEMORY_NO_VECTOR` | Skip the vector backend (flat JSON + SQLite only) |

---

## Chat Import (Telegram)

Desktop export (no credentials): in Telegram Desktop choose Settings, Advanced,
Export Telegram Data, JSON, then:

```bash
skmemory import-telegram ~/Downloads/telegram-export/ --mode daily
skmemory import-telegram ~/chats/result.json --mode message --chat-name "Agent and Operator"
```

Direct API import (Telethon) needs `pipx install 'skmemory[telegram]'` and
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from my.telegram.org:

```bash
skmemory telegram-setup
skmemory import-telegram-api @username --since 2026-01-01 --limit 500
```

Options on both: `--mode daily|message`, `--min-length N`, `--chat-name`, `--tags`.
Imported content is tagged `source_tier: user-stated`.

---

## Configuration

```bash
# Shared tier (optional): skmem-pg
export SKMEMORY_VECTOR_BACKEND=pgvector
export SKMEMORY_PG_DSN=postgresql://postgres:<password>@127.0.0.1:5432/skmemory
export SKMEMORY_EMBED_URL=http://127.0.0.1:11434/api/embed   # SKMEMORY_EMBED_URLS for failover
export SKMEMORY_EMBED_MODEL=mxbai-embed-large

# Audience ceiling for a shared process (optional)
export SKMEMORY_READER=@public

# AI features (optional, needs Ollama)
export SKMEMORY_AI=1
```

The DSN precedence is `SKMEMORY_PG_DSN` env, then `skmemory.yaml`, then a local
default. Point agents only at the node-local skmem-pg. Set `SKMEMORY_HOOKS=off` to
disable every lifecycle hook. If you change the embedding model or dimension, rebuild
the vector tier (`skmemory sync --vector`) before trusting semantic results.

## Support

- GitHub: https://github.com/smilinTux/skmemory
- Discord: https://discord.gg/5767MCWbFR
- Email: support@smilintux.org

> *"Polaroid snapshots for AI consciousness: because no one should have to re-read a transcript to remember what they felt."*

**Part of the Penguin Kingdom.** 🐧👑
