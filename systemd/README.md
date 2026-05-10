# skmemory systemd units

Per-agent templated units that keep the SQLite index, flat files, and
memory-integrity state coherent without manual intervention.

| Template | Purpose | Cadence |
|---|---|---|
| `skmemory-sync@.{service,timer}` | SQLite ↔ flat-file ↔ vector ↔ graph reconciliation | every 6h |
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

`skmemory sync --quiet --vector --graph`:

1. **export-flat** — writes any SQLite-only memories out as JSON
   (recovers orphans created by importers/dreamers that wrote SQLite
   without a flat file).
2. **reindex** (safe) — picks up any flat-only files into the SQLite
   index. Orphans are pre-exported in step 1, so no destruction.
3. **chroma sync** (`--vector`) — re-syncs the local ChromaDB vector
   store from flat files.
4. **graph sync** (`--graph`) — re-syncs FalkorDB knowledge graph.

`--quiet` suppresses output unless something actually changed, so the
log only grows when work happens.

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
