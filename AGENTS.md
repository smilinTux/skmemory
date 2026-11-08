# AGENTS.md (skmemory)

Universal AI memory for the SK stack: per-agent memories with emotion, song anchors and audience
barriers, served over one MCP server and a CLI. Overview: [README.md](./README.md). Operations:
[SOP.md](./SOP.md). This file follows
[AGENT_INSTRUCTIONS_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/AGENT_INSTRUCTIONS_STANDARD.md).
There is no `CLAUDE.md`: Claude Code reads this file, and a `CLAUDE.md` next to it would hide it.

## Source of truth and invariants

- **Flat JSON files are the truth**, under `~/.skcapstone/agents/<agent>/memory/{short,mid,long}-term/`
  (synced by Syncthing). Everything else is derived and rebuildable.
- **Exactly two tiers.** Local: SQLite `index.db` with FTS5, always on, offline. Shared, per node:
  `skmem-pg` on `127.0.0.1:5432` (pgvector, ParadeDB BM25, Apache AGE), no replication; its
  `memories` table is a cache rebuilt by `deploy/skmem-pg/skmem_reconcile.py`. ChromaDB, Qdrant and
  FalkorDB are retired: `backend_select.py` fails fast on them; never reintroduce them.
- **No implicit agent.** `SKAGENT` / `SKCAPSTONE_AGENT` / `SKMEMORY_AGENT` (any set must agree), then
  `SK_DEFAULT_AGENT`, then the sole installed agent, else `None`. `get_active_agent()`
  (`skmemory/agents.py`) refuses rather than guesses. Never treat `None` as "use the template".
- **Audience barriers are code, not labels.** A declared reader (`SKMEMORY_READER`) is a ceiling a
  per-call `reader` can only narrow; the filter runs inside every search arm; a hidden id reads as
  not found; owner-only tools are refused to declared readers (`TOOL_READER_POLICY` in
  `mcp_server.py`). Sacred, private and personal-history memories never reach hook injection
  (`session_context.py`, `hooks/query_slice.py`). The word "sacred" only classifies; it is not the
  barrier. Any change here keeps every barrier fail-closed.
- **A missing or dead search arm is reported as missing**, never as zero results or success.
- **Embeddings are `mxbai-embed-large`, 1024 dimensions everywhere.** Every `vector(...)` column is
  `vector(1024)`; a mismatch breaks cross-collection recall.
- **No secret in the repo, image, argv or logs.** Name skvault entries (`SKMEM_PG_PASSWORD`,
  `SKBRAIN_PG_PROJECTOR_PW`, `SKBRAIN_PG_READER_PW`); `docker compose` refuses to start without
  `SKMEM_PG_PASSWORD`.

## Commands

CI (`.github/workflows/ci.yml`) has three jobs, `test`, `lint` and `build`; all must be green.

```bash
~/.skenv/bin/python -m pytest tests/ -q                 # CI: py3.11 and py3.12, with coverage
~/.skenv/bin/ruff format --check skmemory/ tests/        # CI lint, step 1
~/.skenv/bin/ruff check skmemory/ tests/                 # CI lint, step 2
python -m build && twine check dist/*                    # CI build
```

Lint and format BOTH `skmemory/` and `tests/`; the older README snippet covers only `skmemory/`.
Ruff config is in `pyproject.toml` (line length 99, rules `E,F,W,I,UP,B,SIM`, `E501` ignored).
`docs-check.yml` runs the sk-standards docs gate. Retrieval PRs also get the advisory
`skrsi-evaluate.yml` comment, and `regression-gate.yml` runs
`python -m skmemory.eval.regression_gate` against the committed baseline.

## Start here

1. `skmemory/store.py`: `MemoryStore`, the write path and fused search.
2. `skmemory/backends/`: `file_backend.py` (the truth), `sqlite_backend.py`, `pgvector_backend.py`, `age_backend.py`.
3. `skmemory/mcp_server.py`: every MCP tool and its reader policy.
4. `skmemory/audience.py`: audience levels and reader resolution.
5. `deploy/skmem-pg/README.md`: schema, migrations and node operations.

## Rules

- **Writing style:** no em dashes (U+2014) and no en dashes (U+2013) anywhere: code, comments,
  docstrings, docs, commits, PR bodies. Use commas, parentheses, colons or a new sentence. Hyphens
  are fine, including ranges (`5-10`).
- **Branch and commit:** never commit to `main`. Branch, commit as soon as a fast check passes and
  before the full suite, then open a PR. Add a `changelog.d/<slug>.md` fragment for any code change.
- **Attribution:** a `Co-Authored-By` trailer names the agent that actually did the work, or is left
  out. Never one you cannot evidence.
- **Migrations:** a new migration is `deploy/skmem-pg/NN-name.sql` plus one line in
  `skmemory/pg/migrations.txt`, the single list read by both the compose init wrapper and
  `skmemory pg migrate`. Never add `02-enable-bm25-age.sql` or `03-cutover-mxbai.sql` to it: both
  are baked into `schema.sql`, and `03` is not idempotent. CI never applies migrations.
- **Lint gotchas:** `B904` (`raise ... from exc` inside `except`), `UP045` (`X | None`, not
  `Optional[X]`), `E731` (`def`, not `lambda =`).
- **Install:** SK packages go into `~/.skenv/` (CLIs in `~/.skenv/bin/`).

## Privacy

This repo is public. Never put host names, LAN or tailnet IPs, estate names, personal names or data,
credentials, or any real memory, journal, FEB, seed or song content in code, docs, tests, fixtures,
commits or the site. Fixtures are invented. Enforced for the shared `docs` table by
`tests/test_docs_public_privacy.py` and for readers by `tests/test_audience.py`,
`tests/test_search_reader.py`, `tests/test_reader_read_tools.py`, `tests/test_query_slice.py`;
doc text is on review.

## Hazards

- **Structural vs live tests:** `tests/test_compose_initdb_wiring.py`, `tests/test_pg_migrate.py` and
  `tests/test_ops_namespace_migration.py` check the out-of-the-box wiring with no DB or docker. Live
  DB validation is the operator verify step (`skmemory health`, `skmemory operator observe`,
  `skmemory/pg/verify-ops.sql`), deliberately not automated. Green CI does not mean a node works.
- **`vector_ok: null` means the probe did not run**, not that vectors are fine; a `-template`
  base path means a scaffold store. See sk-standards `SKMEMORY_NODE_CONFIG_STANDARD`.
- **MCP entries must not set `SKMEMORY_PG_DSN`**; a stale DSN in an MCP `env` block silently drops
  search to keyword-only.
- **Song anchors:** skmemory reads anchor files from a separate FFT ingest step. CLAP audio
  embeddings are not implemented (`song_search.py`); do not claim them.
- **Concurrent sessions:** other agents often work this repo at the same time. Branch from a fresh
  `origin/main`, keep PRs small, and never leave edits uncommitted across turns.

## See also

- [skcapstone](https://github.com/smilinTux/skcapstone): agent framework and the main skmemory consumer.
- [cloud9](https://github.com/smilinTux/cloud9): FEB and emotional continuity protocol.
- [skrsi](https://github.com/smilinTux/skrsi): evaluation of retrieval changes (`skrsi/targets/`).
- [sk-standards](https://github.com/smilinTux/sk-standards): `SK_REPO_DOC_STANDARD`, `SKMEMORY_NODE_CONFIG_STANDARD`, `AGENT_INSTRUCTIONS_STANDARD`.
- [skmemory.skworld.io](https://skmemory.skworld.io/): the public site.
