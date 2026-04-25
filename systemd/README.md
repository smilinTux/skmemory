# skmemory systemd units

Per-agent templated units that keep the SQLite index and flat files in
sync without manual intervention.

## Install (per-user)

```bash
mkdir -p ~/.config/systemd/user
cp skmemory-sync@.service skmemory-sync@.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

## Enable for an agent

```bash
systemctl --user enable --now skmemory-sync@opus.timer
systemctl --user enable --now skmemory-sync@lumina.timer
systemctl --user enable --now skmemory-sync@jarvis.timer
```

The timer fires 5 min after boot, then every 6h. The service runs
`skmemory sync --quiet --vector`:

1. **export-flat** — writes any SQLite-only memories out as JSON
   (recovers orphans created by importers/dreamers that wrote SQLite
   without a flat file).
2. **reindex** (safe) — picks up any flat-only files into the SQLite
   index. Orphans are pre-exported in step 1, so no destruction.
3. **chroma sync** (`--vector`) — re-syncs the local ChromaDB vector
   store from flat files.

`--quiet` suppresses output unless something actually changed, so the
log only grows when work happens.

## Logs

```
~/.skcapstone/agents/<agent>/logs/skmemory-sync.log
```

## Status

```bash
systemctl --user status skmemory-sync@opus.timer
systemctl --user list-timers 'skmemory-sync@*'
journalctl --user -u skmemory-sync@opus.service -n 50
```
