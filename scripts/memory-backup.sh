#!/bin/bash
# ============================================
# Agent Memory Backup - Rotation Strategy
# ============================================
# Daily: keep 7, weekly (Sunday): keep 4, monthly (1st): keep 12.
# Backs up: the flat source of truth (memory tiers, tombstones, archive and the
#           rest of memory/ state), seeds, soul, identity, config, trust,
#           journal.md, workspace md files. Never the SQLite index: it is
#           derived, rebuilt by `skmemory reindex`, and torn when copied while
#           sync writes. Standard: docs/BACKUP_SOP.md.
# Output: gzip tarball, GPG-encrypted to the agent's own existing key
#         (same mechanism as sk-backup-anchors.sh), file ends in .tar.gz.gpg.
#
# Why tombstones and archive: tombstones record forgotten ids; without them a
# restore plus reconcile resurrects deleted memories. archive is the cold tier.
#
# Env (all optional):
#   AGENT_DIR        agent home        (default: ~/.skcapstone/agents/lumina)
#   BACKUP_BASE      destination root  (default: ~/.skcapstone/backups/<agent>-memory)
#                    Tarballs are named <agent>-memory-*, <agent> being the
#                    basename of AGENT_DIR.
#   CLAWD_DIR        workspace for SOUL/MEMORY/IDENTITY/USER.md (default: ~/clawd);
#                    "none" skips it (for agents that do not own that workspace)
#   BACKUP_RECIPIENT GPG recipient     (default: lumina@skworld.io)
#   MIN_FREE_MB      free space that must remain after the backup (default: 2048)
#   MEMORY_BACKUP_RESTIC_ENV  path to a restic env file (RESTIC_REPOSITORY,
#                    RESTIC_PASSWORD, backend credentials). When set, the same
#                    source paths plus memory/worship-sessions are also
#                    snapshotted off-site and the run FAILS if that fails.
#                    Unset: local tarball only (not the standard, see SOP).
#   MEMORY_BACKUP_RESTIC_TAG  snapshot tag (default: skmem-flat-<agent dir name>)
#   MEMORY_BACKUP_RESTIC_PRUNE  1 (default) runs `forget --prune` for this tag;
#                    0 runs `forget` only, so a caller backing up several agents
#                    can prune the shared repo once (memory-backup-all.sh)
#   KEEP_DAILY / KEEP_WEEKLY / KEEP_MONTHLY  retention, local and off-site
#                    (defaults: 7 / 4 / 12)
# Exit non-zero on any failure; never falls back to a plaintext archive.
# ============================================

set -euo pipefail

AGENT_DIR="${AGENT_DIR:-$HOME/.skcapstone/agents/lumina}"
AGENT_NAME="$(basename "$AGENT_DIR")"
BACKUP_BASE="${BACKUP_BASE:-$HOME/.skcapstone/backups/$AGENT_NAME-memory}"
CLAWD_DIR="${CLAWD_DIR:-$HOME/clawd}"
RECIPIENT="${BACKUP_RECIPIENT:-lumina@skworld.io}"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"
RESTIC_ENV="${MEMORY_BACKUP_RESTIC_ENV:-}"
RESTIC_TAG="${MEMORY_BACKUP_RESTIC_TAG:-skmem-flat-$AGENT_NAME}"
RESTIC_PRUNE="${MEMORY_BACKUP_RESTIC_PRUNE:-1}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-12}"
DATE=$(date +%Y-%m-%d)
DOW=$(date +%u)
DOM=$(date +%d)
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

log() { echo "[$(date)] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -d "$AGENT_DIR/memory" ] || die "no memory dir under $AGENT_DIR"
gpg --list-keys "$RECIPIENT" >/dev/null 2>&1 || die "GPG key not found for $RECIPIENT (refusing to write a plaintext backup)"

mkdir -p "$BACKUP_BASE/daily" "$BACKUP_BASE/weekly" "$BACKUP_BASE/monthly"

# Paths relative to AGENT_DIR; only those that exist.
AGENT_PATHS=()
for p in memory/short-term memory/mid-term memory/long-term memory/tombstones \
         memory/archive memory/intentions memory/ledger memory/songs \
         memory/anchors memory/rituals memory/reflections memory/learnings \
         memory/quarantine memory/wal memory/dream-catchup \
         seeds soul identity config trust journal.md; do
  [ -e "$AGENT_DIR/$p" ] && AGENT_PATHS+=("$p")
done
# Top-level memory/*.json state files (dreaming, promotion log, itil state).
for f in "$AGENT_DIR"/memory/*.json; do
  [ -f "$f" ] && AGENT_PATHS+=("memory/$(basename "$f")")
done
CLAWD_PATHS=()
if [ "$CLAWD_DIR" != none ]; then
  for f in SOUL.md MEMORY.md IDENTITY.md USER.md; do
    [ -f "$CLAWD_DIR/$f" ] && CLAWD_PATHS+=("$f")
  done
fi

# Required content must be present when it exists: a backup without the
# tombstones or archive of an agent that has them is a failed backup.
for req in memory/tombstones memory/archive; do
  if [ -d "$AGENT_DIR/$req" ] && ! printf '%s\n' "${AGENT_PATHS[@]}" | grep -qx "$req"; then
    die "$req exists but was not selected"
  fi
done

# Size and free-space guard. Worst case gzip is about the source size, and the
# weekly/monthly tiers each hold one more copy of today's file.
SRC_KB=$(cd "$AGENT_DIR" && du -skc "${AGENT_PATHS[@]}" | tail -1 | cut -f1)
if [ "${#CLAWD_PATHS[@]}" -gt 0 ]; then
  CL_KB=$(cd "$CLAWD_DIR" && du -skc "${CLAWD_PATHS[@]}" | tail -1 | cut -f1)
  SRC_KB=$((SRC_KB + CL_KB))
fi
COPIES=1
[ "$DOW" = "7" ] && COPIES=$((COPIES + 1))
[ "$DOM" = "01" ] && COPIES=$((COPIES + 1))
NEED_KB=$((SRC_KB * COPIES + MIN_FREE_MB * 1024))
AVAIL_KB=$(df -Pk "$BACKUP_BASE" | awk 'NR==2 {print $4}')
log "Source ${SRC_KB} KB, copies today ${COPIES}, free ${AVAIL_KB} KB, need ${NEED_KB} KB (incl. ${MIN_FREE_MB} MB headroom)"
[ "$AVAIL_KB" -ge "$NEED_KB" ] || die "not enough free space: need ${NEED_KB} KB, have ${AVAIL_KB} KB"

for t in tombstones archive; do
  [ -d "$AGENT_DIR/memory/$t" ] && log "memory/$t: $(find "$AGENT_DIR/memory/$t" -type f | wc -l) files"
done

DAILY_FILE="$BACKUP_BASE/daily/$AGENT_NAME-memory-${TIMESTAMP}.tar.gz.gpg"
PARTIAL="$DAILY_FILE.partial"
trap 'rm -f "$PARTIAL"' EXIT

TAR_ARGS=(-C "$AGENT_DIR" "${AGENT_PATHS[@]}")
[ "${#CLAWD_PATHS[@]}" -gt 0 ] && TAR_ARGS+=(-C "$CLAWD_DIR" "${CLAWD_PATHS[@]}")

# tar tolerates files vanishing mid-read (exit 1); anything higher is a failure.
set +e
tar -cz "${TAR_ARGS[@]}" 2>/dev/null |
  gpg --batch --yes --trust-model always --recipient "$RECIPIENT" --output "$PARTIAL" --encrypt
rc=("${PIPESTATUS[@]}")
set -e
[ "${rc[0]}" -le 1 ] || die "tar failed (exit ${rc[0]})"
[ "${rc[1]}" -eq 0 ] || die "gpg failed (exit ${rc[1]})"
[ -s "$PARTIAL" ] || die "encrypted archive is empty"
gpg --list-packets "$PARTIAL" 2>/dev/null | grep -q 'pubkey enc packet' || die "archive is not GPG-encrypted"
mv "$PARTIAL" "$DAILY_FILE"
log "Daily backup: $DAILY_FILE ($(du -sh "$DAILY_FILE" | cut -f1), encrypted to $RECIPIENT)"

if [ "$DOW" = "7" ]; then
  cp "$DAILY_FILE" "$BACKUP_BASE/weekly/$AGENT_NAME-memory-week-${DATE}.tar.gz.gpg"
fi
if [ "$DOM" = "01" ]; then
  cp "$DAILY_FILE" "$BACKUP_BASE/monthly/$AGENT_NAME-memory-month-${DATE}.tar.gz.gpg"
fi

# Rotation. The globs end in .tar.gz* so pre-encryption plaintext tarballs age
# out under the same counts instead of lingering forever.
prune() { # dir prefix keep
  (cd "$1" && ls -t "$2"*.tar.gz* 2>/dev/null | tail -n +"$(($3 + 1))" | xargs -r rm -f) || true
}
prune "$BACKUP_BASE/daily" "$AGENT_NAME-memory-" "$KEEP_DAILY"
prune "$BACKUP_BASE/weekly" "$AGENT_NAME-memory-week-" "$KEEP_WEEKLY"
prune "$BACKUP_BASE/monthly" "$AGENT_NAME-memory-month-" "$KEEP_MONTHLY"

log "Rotation complete. Total backup space: $(du -sh "$BACKUP_BASE" | cut -f1)"
# Off-site: restic straight from the source directories, not from the tarball.
# restic encrypts client-side and deduplicates day to day; an encrypted tarball
# changes completely every night and would defeat that. worship-sessions (large
# media) rides here only: it uploads once and then costs almost nothing, where
# the nightly tarball would carry it in full every day.
if [ -n "$RESTIC_ENV" ]; then
  [ -r "$RESTIC_ENV" ] || die "restic env not readable: $RESTIC_ENV"
  command -v restic >/dev/null 2>&1 || die "restic not installed"
  RESTIC_PATHS=()
  for p in "${AGENT_PATHS[@]}"; do RESTIC_PATHS+=("$AGENT_DIR/$p"); done
  [ -d "$AGENT_DIR/memory/worship-sessions" ] && RESTIC_PATHS+=("$AGENT_DIR/memory/worship-sessions")
  for f in "${CLAWD_PATHS[@]}"; do RESTIC_PATHS+=("$CLAWD_DIR/$f"); done
  # Subshell so the credentials never enter this script's environment.
  (
    set -a
    # shellcheck disable=SC1090
    . "$RESTIC_ENV"
    set +a
    [ -n "${RESTIC_REPOSITORY:-}" ] || { echo "RESTIC_REPOSITORY unset" >&2; exit 1; }
    [ -n "${RESTIC_PASSWORD:-}" ] || { echo "RESTIC_PASSWORD unset" >&2; exit 1; }
    restic backup --quiet --tag "$RESTIC_TAG" --host "$(hostname)" "${RESTIC_PATHS[@]}" || exit 1
    # A failed forget leaves MORE history, the safe direction, so it warns and
    # does not fail a run whose snapshot already landed.
    PRUNE_ARG=()
    [ "$RESTIC_PRUNE" = 0 ] || PRUNE_ARG=(--prune)
    restic forget --quiet --tag "$RESTIC_TAG" --host "$(hostname)" \
      --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" \
      --keep-monthly "$KEEP_MONTHLY" "${PRUNE_ARG[@]}" >/dev/null 2>&1 \
      || echo "WARNING: restic forget/prune skipped" >&2
  ) || die "off-site restic snapshot failed (tag $RESTIC_TAG)"
  log "Off-site snapshot OK: restic tag $RESTIC_TAG, ${#RESTIC_PATHS[@]} paths"
else
  log "WARNING: off-site not configured (MEMORY_BACKUP_RESTIC_ENV unset); local tarball only"
fi

log "Agent memory backup complete"
