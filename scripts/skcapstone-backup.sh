#!/usr/bin/env bash
# skcapstone-backup.sh — Daily rsync of ~/.skcapstone to ~/clawd/skcapstone-backup
# File deltas only (no tgz), ~/clawd is already backed up externally.
#
# Excludes: venv, __pycache__, .stversions, index.db (rebuildable),
#           runtime locks/PIDs, and Syncthing internals.
#
# Install:
#   chmod +x scripts/skcapstone-backup.sh
#   cp scripts/skcapstone-backup.sh ~/.skcapstone/agents/lumina/scripts/
#   crontab -e  # add: 30 3 * * * ~/.skcapstone/agents/lumina/scripts/skcapstone-backup.sh

set -euo pipefail

SRC="${SKCAPSTONE_HOME:-$HOME/.skcapstone}/"
DST="${SKCAPSTONE_BACKUP_DIR:-$HOME/clawd/skcapstone-backup}/"
AGENT="${SKAGENT:-lumina}"
LOG="${SRC}agents/${AGENT}/logs/skcapstone-backup.log"

mkdir -p "$(dirname "$LOG")" "$DST"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — Starting skcapstone backup" >> "$LOG"

rsync -a --delete \
  --exclude="venv/" \
  --exclude="__pycache__/" \
  --exclude=".stversions/" \
  --exclude=".stfolder" \
  --exclude="*.db-wal" \
  --exclude="*.db-shm" \
  --exclude="index.db" \
  --exclude="daemon.pid" \
  --exclude="*.lock" \
  --exclude="*.tmp" \
  --exclude="*.sync-conflict*" \
  --exclude="node_modules/" \
  "$SRC" "$DST" 2>&1 | tail -5 >> "$LOG"

RESULT=$?
if [ $RESULT -eq 0 ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — Backup complete" >> "$LOG"
else
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — Backup FAILED (exit $RESULT)" >> "$LOG"
fi
