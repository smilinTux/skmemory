#!/usr/bin/env bash
# skmem-pg-backup.sh — daily pg_dump of skmem-pg (the vector store). Keeps N days.
#
# skmem-pg holds pgvector + BM25 + AGE. It is a node-LOCAL derived cache and is
# rebuildable from the flat JSON source of truth via skmem_reconcile.py, but a
# custom-format dump gives a fast recovery path (seconds vs a full re-embed).
#
# Vendored into the repo (coord ce559215) so the production ops path is versioned
# and a node can be rebuilt from source. Previously only lived at
# ~/.skcapstone/scripts/skmem-pg-backup.sh on .158.
#
# Host-specifics are env-parametrized; defaults preserve the original .158
# behavior so it runs unchanged there.
#
# Env contract (all optional):
#   SKAGENT          agent whose backups dir receives the dump   (default: lumina)
#   SKMEM_BACKUP_DIR override the full destination directory     (default:
#                    $HOME/.skcapstone/agents/$SKAGENT/backups)
#   SKMEM_PG_CONTAINER  docker container name                    (default: skmem-pg)
#   SKMEM_PG_USER       postgres role                            (default: postgres)
#   SKMEM_PG_DB         database name                            (default: skmemory)
#   SKMEM_BACKUP_RETAIN number of daily dumps to keep            (default: 14)
#
# Schedule: daily via cron/systemd-timer (see deploy/ops/README.md).
set -euo pipefail

AGENT="${SKAGENT:-lumina}"
DST="${SKMEM_BACKUP_DIR:-$HOME/.skcapstone/agents/$AGENT/backups}"
CONTAINER="${SKMEM_PG_CONTAINER:-skmem-pg}"
PGUSER="${SKMEM_PG_USER:-postgres}"
PGDB="${SKMEM_PG_DB:-skmemory}"
RETAIN="${SKMEM_BACKUP_RETAIN:-14}"

mkdir -p "$DST"
TS=$(date -u +%Y%m%d-%H%M%S)
OUT="$DST/${CONTAINER}-${PGDB}-${TS}.dump"

docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" -Fc > "$OUT"
echo "$(date -u +%FT%TZ) — dumped $(du -h "$OUT" | cut -f1) -> $OUT"

# retain the newest $RETAIN daily dumps; drop the rest
ls -1t "$DST/${CONTAINER}-${PGDB}"-*.dump 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f
