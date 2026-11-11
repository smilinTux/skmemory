#!/bin/bash
# ============================================
# Back up every agent on this node with memory-backup.sh
# ============================================
# One nightly job for the whole node, so an agent added later is covered
# without a new cron line. Each agent gets its own local GFS tarballs
# (BACKUP_BASE_ROOT/<agent>-memory) and its own restic tag
# (skmem-flat-<agent>); the shared restic repo is pruned once at the end
# instead of once per agent. Standard: docs/BACKUP_SOP.md.
#
# An agent is backed up when its home is a real directory (not a symlink)
# holding at least one .json in memory/{short,mid,long}-term, tombstones or
# archive. Empty scaffolding dirs are skipped and logged.
#
# Env (all optional; everything memory-backup.sh reads passes through):
#   AGENTS_ROOT       agent homes       (default: ~/.skcapstone/agents)
#   BACKUP_BASE_ROOT  local destination (default: ~/.skcapstone/backups)
#   MEMORY_BACKUP_SKIP  space-separated agent names to leave out
#   MEMORY_BACKUP_WORKSPACE_AGENT  the one agent whose backup also carries the
#                     CLAWD_DIR workspace md files (default: lumina)
# Exit non-zero when any agent failed; the others still run.
# ============================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ONE="$HERE/memory-backup.sh"
AGENTS_ROOT="${AGENTS_ROOT:-$HOME/.skcapstone/agents}"
BACKUP_BASE_ROOT="${BACKUP_BASE_ROOT:-$HOME/.skcapstone/backups}"
SKIP=" ${MEMORY_BACKUP_SKIP:-} "
WORKSPACE_AGENT="${MEMORY_BACKUP_WORKSPACE_AGENT:-lumina}"
RESTIC_ENV="${MEMORY_BACKUP_RESTIC_ENV:-}"

log() { echo "[$(date)] $*"; }

[ -x "$ONE" ] || [ -r "$ONE" ] || { log "ERROR: $ONE missing" >&2; exit 1; }
[ -d "$AGENTS_ROOT" ] || { log "ERROR: no agents root $AGENTS_ROOT" >&2; exit 1; }

has_memory() {
  local t
  for t in short-term mid-term long-term tombstones archive; do
    [ -d "$1/memory/$t" ] || continue
    [ -n "$(find "$1/memory/$t" -maxdepth 1 -name '*.json' -type f -print -quit)" ] && return 0
  done
  return 1
}

ok=() failed=() skipped=()
for dir in "$AGENTS_ROOT"/*/; do
  dir="${dir%/}"
  name="$(basename "$dir")"
  if [ -L "$dir" ] || [[ "$SKIP" == *" $name "* ]] || ! has_memory "$dir"; then
    skipped+=("$name")
    continue
  fi
  ws=none
  [ "$name" = "$WORKSPACE_AGENT" ] && ws="${CLAWD_DIR:-$HOME/clawd}"
  log "== $name"
  if AGENT_DIR="$dir" BACKUP_BASE="$BACKUP_BASE_ROOT/$name-memory" CLAWD_DIR="$ws" \
     MEMORY_BACKUP_RESTIC_TAG="skmem-flat-$name" MEMORY_BACKUP_RESTIC_PRUNE=0 \
     bash "$ONE"; then
    ok+=("$name")
  else
    failed+=("$name")
    log "ERROR: backup failed for $name" >&2
  fi
done

# One prune for the shared repo. A failed prune keeps more data, the safe
# direction, so it warns without failing the run.
if [ -n "$RESTIC_ENV" ] && [ "${#ok[@]}" -gt 0 ]; then
  (
    set -a
    # shellcheck disable=SC1090
    . "$RESTIC_ENV"
    set +a
    restic prune --quiet >/dev/null 2>&1
  ) && log "restic prune OK" || log "WARNING: restic prune skipped" >&2
fi

log "Backed up ${#ok[@]}: ${ok[*]:-}"
log "Skipped ${#skipped[@]} (empty, symlink or MEMORY_BACKUP_SKIP): ${skipped[*]:-}"
if [ "${#ok[@]}" -eq 0 ] && [ "${#failed[@]}" -eq 0 ]; then
  log "ERROR: no agent with memory under $AGENTS_ROOT; nothing was backed up" >&2
  exit 1
fi
if [ "${#failed[@]}" -gt 0 ]; then
  log "FAILED ${#failed[@]}: ${failed[*]}" >&2
  exit 1
fi
log "All agent memory backups complete"
