#!/usr/bin/env bash
# skmem-pg-backup.sh - daily pg_dump of skmem-pg (the vector store). Keeps N days
# AND ships a copy OFF-BOX (a backup that never leaves the box is not DR).
#
# skmem-pg holds pgvector + BM25 + AGE. It is a node-LOCAL derived cache and is
# rebuildable from the flat JSON source of truth via skmem_reconcile.py, but a
# custom-format dump gives a fast recovery path (seconds vs a full re-embed) and
# is the ONLY off-node copy of the ~33k-node AGE graph.
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
#   Off-box DR (the point of this script):
#   SKMEM_BACKUP_OFFBOX  space- and/or comma-separated list of off-box targets.
#                        Each target is either:
#                          - a LOCAL directory path (e.g. the Syncthing-replicated
#                            tree, so the dump propagates to every node), or
#                          - a REMOTE rsync/ssh spec "user@host:/path" or
#                            "host:/path" (e.g. .41), shipped via rsync-over-ssh.
#                        If UNSET: the run still succeeds but prints a loud WARNING
#                        that DR shipping is not configured (backward compatible).
#                        If SET: every target MUST receive the dump or the whole
#                        run FAILS non-zero (fail loudly - the whole reason DR
#                        exists is that the box protecting the dump can die).
#   SKMEM_BACKUP_OFFBOX_STRICT  when "1" (default when SKMEM_BACKUP_OFFBOX is set),
#                        an off-box failure is fatal. Set "0" to downgrade to a
#                        warning (NOT recommended; only for a node with no peer).
#
# Schedule: daily via cron/systemd-timer (see deploy/ops/README.md).
set -euo pipefail

AGENT="${SKAGENT:-lumina}"
DST="${SKMEM_BACKUP_DIR:-$HOME/.skcapstone/agents/$AGENT/backups}"
CONTAINER="${SKMEM_PG_CONTAINER:-skmem-pg}"
PGUSER="${SKMEM_PG_USER:-postgres}"
PGDB="${SKMEM_PG_DB:-skmemory}"
RETAIN="${SKMEM_BACKUP_RETAIN:-14}"
OFFBOX_RAW="${SKMEM_BACKUP_OFFBOX:-}"

log() { echo "$(date -u +%FT%TZ) $*"; }
warn() { echo "$(date -u +%FT%TZ) WARNING: $*" >&2; }
die() { echo "$(date -u +%FT%TZ) ERROR: $*" >&2; exit 1; }

mkdir -p "$DST"
TS=$(date -u +%Y%m%d-%H%M%S)
BASENAME="${CONTAINER}-${PGDB}-${TS}.dump"
OUT="$DST/$BASENAME"

# --- 1. Local dump (unchanged behavior) ---
# pg_dump -Fc is a full custom-format dump (schema + data + functions), the exact
# artifact scripts/skmem-pg-restore.sh consumes.
docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" -Fc > "$OUT"
if [ ! -s "$OUT" ]; then
  die "local dump is empty or missing: $OUT (is container '$CONTAINER' up?)"
fi
log "dumped $(du -h "$OUT" | cut -f1) -> $OUT"

# retain the newest $RETAIN daily dumps locally; drop the rest
ls -1t "$DST/${CONTAINER}-${PGDB}"-*.dump 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f

# --- 2. Off-box shipping (the DR requirement) ---
# A dump on the same disk it protects is not a backup. Ship it elsewhere.
prune_local_dir() {
  # keep newest $RETAIN dumps in an off-box LOCAL dir too (best-effort, non-fatal)
  local dir="$1"
  ls -1t "$dir/${CONTAINER}-${PGDB}"-*.dump 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f || true
}

ship_one() {
  # ship $OUT to a single target; return non-zero on failure
  local target="$1"
  case "$target" in
    *:*)
      # remote rsync/ssh spec (host:/path or user@host:/path). A bare absolute
      # local path never matches because it has no ':'.
      if ! command -v rsync >/dev/null 2>&1; then
        warn "rsync not found; falling back to scp for $target"
        scp -q "$OUT" "$target/" || return 1
        return 0
      fi
      rsync -a --timeout=120 "$OUT" "$target/" || return 1
      # best-effort remote retention prune (never fatal)
      local rhost="${target%%:*}" rpath="${target#*:}"
      ssh -o BatchMode=yes -o ConnectTimeout=15 "$rhost" \
        "ls -1t '$rpath/${CONTAINER}-${PGDB}'-*.dump 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f" \
        >/dev/null 2>&1 || warn "remote prune skipped for $target"
      ;;
    *)
      # local directory path (e.g. Syncthing-replicated tree)
      mkdir -p "$target" || return 1
      cp -f "$OUT" "$target/$BASENAME" || return 1
      # verify the copy landed with the same byte size
      local src_sz dst_sz
      src_sz=$(stat -c '%s' "$OUT" 2>/dev/null || wc -c < "$OUT")
      dst_sz=$(stat -c '%s' "$target/$BASENAME" 2>/dev/null || wc -c < "$target/$BASENAME")
      if [ "$src_sz" != "$dst_sz" ]; then
        warn "size mismatch after copy to $target ($src_sz != $dst_sz)"
        return 1
      fi
      prune_local_dir "$target"
      ;;
  esac
  return 0
}

if [ -z "$OFFBOX_RAW" ]; then
  warn "SKMEM_BACKUP_OFFBOX is not set - the dump stays on THIS box only."
  warn "That is NOT disaster recovery. Set SKMEM_BACKUP_OFFBOX to a synced dir"
  warn "and/or a remote host (e.g. '/path/to/synced-tree user@192.168.0.41:/backups/skmem')."
else
  STRICT="${SKMEM_BACKUP_OFFBOX_STRICT:-1}"
  # split on commas and/or whitespace
  offbox_norm=$(echo "$OFFBOX_RAW" | tr ',' ' ')
  shipped=0
  failed=0
  for target in $offbox_norm; do
    [ -z "$target" ] && continue
    if ship_one "$target"; then
      log "off-box OK -> $target"
      shipped=$((shipped + 1))
    else
      warn "off-box FAILED -> $target"
      failed=$((failed + 1))
    fi
  done
  if [ "$failed" -gt 0 ]; then
    if [ "$STRICT" = "1" ]; then
      die "$failed off-box target(s) failed; a dump that never leaves the box is not DR. (set SKMEM_BACKUP_OFFBOX_STRICT=0 to downgrade to a warning)"
    else
      warn "$failed off-box target(s) failed but STRICT=0; continuing."
    fi
  fi
  log "off-box shipping complete: $shipped ok, $failed failed"
fi
