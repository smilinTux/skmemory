# skmemory systemd units

Per-agent templated units that keep the SQLite index, flat files, and
memory-integrity state coherent without manual intervention.

| Template | Purpose | Cadence |
|---|---|---|
| `skmemory-sync@.{service,timer}` | SQLite ↔ flat-file + skmem-pg (pgvector) + AGE graph reconciliation | every 6h |
| `skmemory-fortress-verify@.{service,timer}` | SHA-256 integrity verify of all memories; alert on tamper | daily 03:00 |

## Install (one-shot, recommended)

The repo ships an installer that handles copy + enable + optional alert
hook wiring with an interactive prompt:

```bash
cd ~/clawd/skcapstone-repos/skmemory
scripts/install-systemd.sh
```

Or non-interactively:

```bash
scripts/install-systemd.sh --agents lumina,opus,jarvis --sync --fortress --telegram-hook
scripts/install-systemd.sh --agents lumina --no-fortress     # sync only
scripts/install-systemd.sh --uninstall --agents lumina       # remove all timers
```

## Install (manual)

```bash
mkdir -p ~/.config/systemd/user
cp skmemory-sync@.{service,timer}              ~/.config/systemd/user/
cp skmemory-fortress-verify@.{service,timer}   ~/.config/systemd/user/
systemctl --user daemon-reload
```

## Enable per agent

```bash
# Sync (every 6h)
systemctl --user enable --now skmemory-sync@lumina.timer

# Fortress verify (daily 03:00 with ±5min jitter)
systemctl --user enable --now skmemory-fortress-verify@lumina.timer
```

## sync timer details

`skmemory sync --quiet --vector --graph` targets the **live stack**:
the node-local **skmem-pg** container (pgvector) and its **Apache AGE**
knowledge graph. The retired ChromaDB and FalkorDB targets were removed
(card 162a19eb).

1. **export-flat** — writes any SQLite-only memories out as JSON
   (recovers orphans created by importers/dreamers that wrote SQLite
   without a flat file).
2. **reindex** (safe) — picks up any flat-only files into the SQLite
   index. Orphans are pre-exported in step 1, so no destruction.
3. **pgvector reconcile** (`--vector`) — delegates to the vendored
   production engine (`python -m skmemory.reconcile`): backfills flat
   memories missing from skmem-pg, applies the **guarded** orphan prune
   (cold-boot / mid-Syncthing-sync safe — refuses to wipe a derived
   index from an empty/partial flat source), and embeds any null-vector
   rows via mxbai on `.100`.
4. **AGE graph sync** (`--graph`) — backfills the
   `<agent>_knowledge` property graph in skmem-pg (memory nodes +
   Tag/Source/RELATED_TO/SUPERSEDES/MENTIONS/CITES/ASSERTS/IN_SECTION
   edges) from flat files. Idempotent (MERGE semantics).

Both phases talk only to the **node-local** container
(`localhost:5432` / `docker exec skmem-pg`), so the timer must run on
every node, for every agent whose flat files that node serves. DSN
override per node is `SKMEMORY_PG_DSN` (default the local container);
embed endpoint/model come from the agent's `skmemory.yaml`
(`embed_url` / `embed_model`) or the reconcile env defaults.

`--quiet` suppresses output unless something actually changed, so the
log only grows when work happens.

> **Deploy note (not auto-applied):** the tracked unit already invokes
> `skmemory sync --quiet --vector --graph`, so no `ExecStart` edit is
> needed. To roll this retarget onto a node, refresh the installed unit
> copies and reload:
>
> ```bash
> cd ~/clawd/skcapstone-repos/skmemory
> scripts/install-systemd.sh --agents lumina,opus,jarvis --sync --no-fortress
> systemctl --user restart skmemory-sync@lumina.timer   # or wait for next tick
> ```
>
> Requires the node-local `skmem-pg` container running and reachable
> (`docker ps | grep skmem-pg`).

## fortress-verify timer details

Runs `scripts/fortress-verify.sh`, which:

1. Calls `skmemory fortress verify --json` for the agent named by `%i`.
2. Writes a stamped JSON result to
   `~/.skcapstone/agents/<agent>/fortress/last-verify.json`.
3. Fires an alert hook on `TAMPER` (exit 2) or `FAIL` (exit 3) — silent
   on `OK`.

Alert hook resolution (in order):
- `$SKMEMORY_FORTRESS_ALERT_CMD` (override in the service unit)
- `~/.skenv/bin/skmemory-fortress-alert` (auto-detected, executable)

A sample Telegram hook ships at `scripts/fortress-alert-telegram.sh` and
is symlinked into place when you run the installer with
`--telegram-hook`. Full procedures + tamper-response playbook in
[`../docs/FORTRESS_SOP.md`](../docs/FORTRESS_SOP.md).

## Logs

```
~/.skcapstone/agents/<agent>/logs/skmemory-sync.log
~/.skcapstone/agents/<agent>/logs/fortress-verify.log
~/.skcapstone/agents/<agent>/fortress/last-verify.{json,txt}
```

## Status

```bash
systemctl --user status skmemory-sync@lumina.timer
systemctl --user status skmemory-fortress-verify@lumina.timer
systemctl --user list-timers 'skmemory-*'
journalctl --user -u skmemory-fortress-verify@lumina.service -n 50
```
