# skmemory production ops scripts

The scripts that keep a skmemory node alive. Previously these lived only on
`.158` outside any git repo (`~/skmem-build/`, `~/.skcapstone/scripts/`,
`~/.hermes/scripts/`), so losing that host destroyed the only copy. They are
vendored here (coord `ce559215`) so any node is rebuildable from source.

All host-specifics (agent, DSN, container, backup dir, embed URL, alert lib) are
env-parametrized. The defaults reproduce the original `.158` behavior, so each
script runs unchanged there.

| Script | What it does | Schedule |
| --- | --- | --- |
| `skmem_reconcile.py` (in `deploy/skmem-pg/`, module `skmemory/reconcile.py`) | Idempotent flat↔pg reconcile: backfills missing memories (embed + upsert), prunes pg rows whose flat file is gone, re-embeds NULL-vector rows. Rebuilds the `memories` derived cache from the Syncthing-synced flat JSON source of truth. | daily |
| `skmem-pg-backup.sh` | Daily `pg_dump -Fc` of skmem-pg to the agent's `backups/` dir; retains the newest N (default 14) AND ships the dump OFF-box (fail-loud). Fast-recovery path complementing the from-source rebuild. | daily |
| `skmem-health.sh` | Deterministic health probe over the whole stack (flat writes, SQLite index + functional query, skmem-pg reachability + functional vector/hybrid retrieval, backups, skwhisper). Prints a `[PASS]/[WARN]/[FAIL]` digest, archives a dated report, and fires sk-alert on WARN/FAIL. | daily |

## `skmem_reconcile.py`

Lives at `deploy/skmem-pg/skmem_reconcile.py` (standalone cron copy) and as the
in-package engine `skmemory/reconcile.py` (importable + unit-tested). The
backfill / prune / re-embed-NULL decision logic is covered by
`tests/test_reconcile_invariant.py` (rebuild-from-empty, prune-on-flat-delete,
re-embed, idempotency), agent-scoped to a throwaway PID-keyed agent so it cannot
touch real memories; it self-skips when the local pg / docker path / embed
endpoint is unreachable.

Env contract:

| Var | Default | Meaning |
| --- | --- | --- |
| `SKAGENT` / argv[1] | `lumina` | agent whose flat files to reconcile (single-agent mode) |
| `--all` / `--agents a,b,c` | - | in-package engine only: reconcile every provisioned agent, or an explicit list (see below) |
| `EMBED_URL` | `http://192.168.0.100:11434/api/embed` | mxbai embed endpoint |
| `EMBED_MODEL` | `mxbai-embed-large` | embed model |

Talks to the node-LOCAL container via `docker exec skmem-pg psql` (no host
param), so it can only ever act on the box it runs on. Run:

```sh
python deploy/skmem-pg/skmem_reconcile.py [AGENT]
# or, from the installed package:
python -m skmemory.reconcile [AGENT]
```

### Multi-agent reconcile (all agents, one run)

The standalone cron copy reconciles a single agent. The in-package engine adds
an "all agents" mode so a node self-heals EVERY agent whose flat files it serves
(not just `lumina`) - opus, jarvis, ava, and the swarm specialists all write via
MCP and would otherwise drift in pg with no repair:

```sh
python -m skmemory.reconcile --all            # every agent with a memory dir
python -m skmemory.reconcile --agents opus,jarvis,ava   # explicit list
```

- Agents are discovered by scanning the agent base dir (`~/.skcapstone/agents/`,
  or `SKMEMORY_HOME` / `SKCAPSTONE_HOME`) for homes that have a `memory/` dir;
  `*-template` scaffolds are excluded.
- Failure isolation: a failing agent (e.g. embed timeout) does NOT abort the
  others; each agent's result is captured in a per-agent summary and the process
  exits non-zero if ANY agent failed.
- Node self-sufficiency (prb-6f069c5e): schedule this on every node, for every
  agent whose flat files that node serves. `--all` replaces N per-agent systemd
  template instances with one timer per node; respecting the .158-primary policy,
  .158 runs `--all` for the full fleet, other nodes run `--all` for whatever
  agents they hold. Programmatic entry point: `skmemory.reconcile.reconcile_all`.

```sh
# cron, single node, all agents:
15 4 * * *  python -m skmemory.reconcile --all >> ~/.skcapstone/logs/skmem-reconcile-all.log 2>&1
```

## `skmem-pg-backup.sh`

```sh
deploy/ops/skmem-pg-backup.sh
```

| Var | Default | Meaning |
| --- | --- | --- |
| `SKAGENT` | `lumina` | agent whose `backups/` dir receives the dump |
| `SKMEM_BACKUP_DIR` | `$HOME/.skcapstone/agents/$SKAGENT/backups` | full destination dir override |
| `SKMEM_PG_CONTAINER` | `skmem-pg` | docker container name |
| `SKMEM_PG_USER` | `postgres` | postgres role |
| `SKMEM_PG_DB` | `skmemory` | database name |
| `SKMEM_BACKUP_RETAIN` | `14` | daily dumps to keep |
| `SKMEM_BACKUP_OFFBOX` | (unset) | space/comma-separated OFF-box targets: local dir(s) (e.g. the Syncthing tree) and/or remote `user@host:/path` (rsync/ssh). A dump that never leaves the box is not DR. |
| `SKMEM_BACKUP_OFFBOX_STRICT` | `1` when OFFBOX set | `1` = an off-box failure is fatal (exit non-zero); `0` = downgrade to a warning (only for a peerless node). |

Output: `<container>-<db>-<UTC-timestamp>.dump` (pg custom format, full schema +
data + functions, and it DOES carry the AGE `ag_graph` registry). When
`SKMEM_BACKUP_OFFBOX` is set the dump is also shipped to each target and the run
FAILS LOUD if any ship fails. When it is unset the run still succeeds but prints a
loud warning that DR shipping is not configured.

Restore with `scripts/skmem-pg-restore.sh <dump>` (builds/uses the vendored image,
restores into a FRESH ephemeral container, and VERIFIES hybrid_search + AGE graph;
refuses to target the live container/port). skmem-pg is a rebuildable derived
cache, so a lost dump is not data loss; the dump just makes recovery seconds
instead of a full re-embed, and is the only fast/complete path for the AGE graph.
See `docs/deploy-plan/skmemory-bulletproof-deploy.md` section 6 for the full
cold-machine ceremony + flat-files-only fallback, and
`docs/deploy-plan/restore-drill-log.md` for drill records.

## `skmem-health.sh`

```sh
deploy/ops/skmem-health.sh          # prints digest to stdout + archives a report
```

Emits one `[PASS]/[WARN]/[FAIL] <check> — <detail>` line per check and a final
`SUMMARY` line (worst verdict). Writes a dated report to
`$AGENT_DIR/logs/skmem-health/<date>-skmem-health.md` (+ a `latest.md` symlink)
and persists `pg_rows`/`worst` to `skmem-health-state.json` for run-over-run
deltas. On WARN/FAIL it calls `sk_alert` (deduped, 6h TTL) if the alert lib is
present; the alert is optional and its absence never fails the run. Every check
is deterministic — no LLM decides "healthy".

| Var | Default | Meaning |
| --- | --- | --- |
| `SKAGENT` | `lumina` | agent to probe |
| `SKMEMORY_HEALTH_DSN` | `postgresql://postgres:skmemory@localhost:5432/skmemory` | pg DSN for the probe (node-local dev default, matches the repo tests) |
| `SKMEM_EMBED_URL` | `http://192.168.0.100:11434/api/embed` | embed endpoint for the functional pg retrieval probe |
| `SKMEM_EMBED_MODEL` | `mxbai-embed-large` | embed model |
| `SKMEM_BACKUP_ROOT` | `$HOME/.skcapstone/backups` | GFS + `<agent>-memory` backup lineage root |
| `SKALERT_LIB` | `$HOME/.hermes/scripts/lib/skalert.sh` | optional sk-alert helper to source |

## Secrets

No secret values are committed. The one credential-shaped default,
`postgresql://postgres:skmemory@localhost:5432/skmemory`, is the node-LOCAL
skmem-pg dev password already used throughout the repo (see
`tests/test_reconcile_invariant.py`, `skmemory/backends/*`). It is not a
production secret; override it per node with `SKMEMORY_HEALTH_DSN`.

## Scheduling

Install as daily cron entries or systemd timers on each node, per agent. Example
crontab:

```cron
15 3 * * *  SKAGENT=lumina /path/to/repo/deploy/ops/skmem-pg-backup.sh   >> ~/.skcapstone/agents/lumina/logs/skmem-pg-backup.log 2>&1
30 3 * * *  SKAGENT=lumina python /path/to/repo/deploy/skmem-pg/skmem_reconcile.py lumina >> ~/.skcapstone/agents/lumina/logs/skmem-reconcile.log 2>&1
45 7 * * *  SKAGENT=lumina /path/to/repo/deploy/ops/skmem-health.sh      # stdout usually wrapped by Hermes into the synthesis prompt
```

## Related

- `docs/deploy-plan/skmemory-bulletproof-deploy.md` — the bulletproof deploy plan
- `deploy/skmem-pg/README.md` — skmem-pg image build (schema, extensions)
- `~/.skcapstone/docs/MEMORY_STORES.md` — store map
