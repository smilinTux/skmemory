#!/usr/bin/env bash
# skcapstone-backup.sh: nightly point-in-time backup of ~/.skcapstone from
# noroc2027 (.158) to the .41 laptop, off this node's own disk. Syncthing
# spreads deletes to every node instantly, so this keeps dated snapshots
# instead (unchanged files are hardlinked to the previous day via
# --link-dest, so each extra day costs only what changed).
#
# Version control note: this script lived only at
# ~/.skcapstone/agents/lumina/scripts/ on .158 -- it had evolved past (and
# this repo copy had drifted stale relative to) the live, more advanced
# hardlink-snapshot version. Brought back in sync here, then extended with
# GFS tiers.
#
# Card: skmemory sqlite-only-orphans, 2026-10-16. The daily tier alone kept
# only 7 days; this off-node snapshot is the laptop's one genuinely
# off-disk copy of .158's whole ~/.skcapstone tree (including flat memory
# files), and 7 days could never have reached back to April, when that
# incident's corruption actually started. Adds weekly (Sunday) and monthly
# (1st) promotion tiers on top of the existing daily tier, each with its
# own independent retention, mirroring deploy/ops/skmem-pg-backup.sh's GFS
# design.
#
# Install:
#   chmod +x scripts/skcapstone-backup.sh
#   cp scripts/skcapstone-backup.sh ~/.skcapstone/agents/lumina/scripts/
#   crontab -e  # add: 30 3 * * * ~/.skcapstone/agents/lumina/scripts/skcapstone-backup.sh
#
# Env (all optional):
#   SKCAPSTONE_BACKUP_HOST        gate: only runs on this host (default: noroc2027)
#   SKCAPSTONE_BACKUP_REMOTE      ssh target (default: laptop)
#   SKCAPSTONE_BACKUP_RDIR        remote base dir (default: backups/skcapstone-noroc2027)
#   SKCAPSTONE_BACKUP_DAILY_KEEP  daily snapshots to keep (default: 7)
#   SKCAPSTONE_BACKUP_WEEKLY_KEEP   weekly snapshots to keep (default: 8)
#   SKCAPSTONE_BACKUP_MONTHLY_KEEP  monthly snapshots to keep (default: 12)
#   SKCAPSTONE_BACKUP_DOW_OVERRIDE  override day-of-week (1=Mon..7=Sun), for tests
#   SKCAPSTONE_BACKUP_DOM_OVERRIDE  override day-of-month (01..31), for tests
#   SKCAPSTONE_BACKUP_TODAY_OVERRIDE  override "today"'s date string, for tests

set -uo pipefail

# --- Pure decision logic (no ssh, no filesystem -- directly unit-testable) --

gfs_tiers_for_today() {
    # Given day-of-week (1=Mon..7=Sun) and day-of-month (01..31), print zero
    # or more "tier_name retain_count" lines for today's promotions.
    #
    # Args:
    #   $1 dow: day of week, 1-7 (7 = Sunday).
    #   $2 dom: day of month, 01-31.
    #   $3 weekly_keep: weekly tier retention count.
    #   $4 monthly_keep: monthly tier retention count.
    local dow="$1" dom="$2" weekly_keep="$3" monthly_keep="$4"
    [ "$dow" = "7" ] && echo "weekly $weekly_keep"
    [ "$dom" = "01" ] && echo "monthly $monthly_keep"
    return 0
}

# --- Side-effecting main, guarded so sourcing this file for its functions
# (e.g. from a test) never ships, deletes, or ssh's anything. ---

main() {
    local backup_host="${SKCAPSTONE_BACKUP_HOST:-noroc2027}"
    # shellcheck disable=SC2155
    local this_host="$(hostname)"
    [ "$this_host" = "$backup_host" ] || return 0 # this file syncs to every node

    local src="$HOME/.skcapstone/"
    local remote="${SKCAPSTONE_BACKUP_REMOTE:-laptop}"
    local rdir="${SKCAPSTONE_BACKUP_RDIR:-backups/skcapstone-noroc2027}"
    local daily_keep="${SKCAPSTONE_BACKUP_DAILY_KEEP:-7}"
    local weekly_keep="${SKCAPSTONE_BACKUP_WEEKLY_KEEP:-8}"
    local monthly_keep="${SKCAPSTONE_BACKUP_MONTHLY_KEEP:-12}"
    local today="${SKCAPSTONE_BACKUP_TODAY_OVERRIDE:-$(date +%Y-%m-%d)}"
    local dow="${SKCAPSTONE_BACKUP_DOW_OVERRIDE:-$(date +%u)}"
    local dom="${SKCAPSTONE_BACKUP_DOM_OVERRIDE:-$(date +%d)}"
    local log="$HOME/.skcapstone/agents/lumina/logs/skcapstone-backup.log"

    log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$log"; }

    log "Starting skcapstone backup -> $remote:$rdir/$today"
    local prev
    prev="$(ssh -o BatchMode=yes -o ConnectTimeout=20 "$remote" "ls -1d $rdir/20* 2>/dev/null | tail -1")"
    local link=()
    [ -n "$prev" ] && [ "$prev" != "$rdir/$today" ] && link=(--link-dest="../$(basename "$prev")")

    rsync -a --delete "${link[@]}" \
        --exclude="venv/" --exclude="__pycache__/" --exclude=".stversions/" \
        --exclude=".stfolder" --exclude="*.db-wal" --exclude="*.db-shm" \
        --exclude="index.db" --exclude="daemon.pid" --exclude="*.lock" \
        --exclude="*.tmp" --exclude="*.sync-conflict*" --exclude="node_modules/" \
        -e "ssh -o BatchMode=yes -o ConnectTimeout=20" \
        "$src" "$remote:$rdir/$today/" 2>&1 | tail -5 >>"$log"
    local result=${PIPESTATUS[0]}
    # rsync 24 = "some files vanished during transfer": normal on a live,
    # rotating tree (backup tiers prune while we copy). Not a failure.
    if [ "$result" -eq 24 ]; then
        log "rsync: some source files vanished during transfer (exit 24), treated as success"
        result=0
    fi

    if [ "$result" -ne 0 ]; then
        log "Backup FAILED (rsync exit $result)"
        return "$result"
    fi

    ssh -o BatchMode=yes "$remote" "cd $rdir && ls -1d 20* | head -n -$daily_keep | xargs -r rm -rf"
    log "Backup complete ($today, daily keep=$daily_keep)"

    # GFS promotion: hardlink today's snapshot into weekly/monthly trees on
    # the right day, each with its own independent retention.
    while read -r tier retain; do
        [ -z "$tier" ] && continue
        ssh -o BatchMode=yes "$remote" "
            mkdir -p '$rdir/$tier'
            if [ ! -e '$rdir/$tier/$today' ]; then
                cp -al '$rdir/$today' '$rdir/$tier/$today' 2>/dev/null \
                    || cp -a '$rdir/$today' '$rdir/$tier/$today'
            fi
            cd '$rdir/$tier' && ls -1d 20* 2>/dev/null | head -n -$retain | xargs -r rm -rf
        "
        log "Promoted $today to $tier (retain $retain)"
    done < <(gfs_tiers_for_today "$dow" "$dom" "$weekly_keep" "$monthly_keep")

    return 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    exit $?
fi
