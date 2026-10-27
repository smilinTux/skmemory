#!/usr/bin/env bash
# skmemory-sync-audit.sh: periodic health check of every agent's
# skmemory-sync@<agent>.timer coverage and drift.
#
# Version control note: this script lived unversioned at
# ~/.skcapstone/scripts/skmemory-sync-audit.sh on .158 only (cron-invoked
# weekly; never present on any other node's crontab). It is now versioned
# here; each host's cron-invoked copy should be a symlink into a checkout
# of this repo. See docs/postmortems/2026-10-16-sqlite-only-orphans.md.
#
# Root cause fixed here (card: skmemory sqlite-only-orphans, 2026-10-16):
# the previous version discovered agents to audit by listing ACTIVE
# `skmemory-sync@*.timer` units, and when that list was empty it printed
# "Nothing to audit" and exited 0 with NO alert. From 2026-04-26 through
# 2026-10-11 -- every single week for six months -- that is exactly what
# happened on .158, because the lumina/opus/jarvis timers were never
# actually enabled there (shipped in-repo since v0.9.8, never installed).
# Zero monitored targets was silently treated as "all clear" instead of
# the alarming condition it actually is.
#
# This version instead discovers every agent that HAS a memory home on
# this host (an actual thing to protect), and treats "home exists, timer
# not enabled" as a loud, alerted problem -- not a skip. It also replaces
# the old raw Telegram-API curl with `sk-alert`, so this audit goes through
# the estate's one alerting primitive instead of its own side channel.

set -uo pipefail

LOG_DIR="${LOG_DIR:-$HOME/.skcapstone/logs}"
mkdir -p "$LOG_DIR"
DATE=$(date +%Y-%m-%d)
AUDIT_LOG="$LOG_DIR/skmemory-sync-audit-$DATE.log"
exec > >(tee -a "$AUDIT_LOG") 2>&1
echo "=== skmemory-sync audit $(date +'%Y-%m-%d %H:%M %Z') ==="

SK_ALERT="${SK_ALERT:-$HOME/.skenv/bin/sk-alert}"
SKMEMORY="${SKMEMORY:-$HOME/.skenv/bin/skmemory}"
AGENTS_DIR="${AGENTS_DIR:-$HOME/.skcapstone/agents}"

send_alert() {
    # send_alert LEVEL MESSAGE -- goes through sk-alert when available,
    # else prints loudly to stderr so a human watching cron output still
    # sees it (never silently drop an alert just because sk-alert is
    # missing on this host).
    local level="$1"
    shift
    if [[ -x "$SK_ALERT" ]]; then
        "$SK_ALERT" -l "$level" -k "skmemory-sync-audit-$DATE" "$@"
    else
        echo "WARN: sk-alert not found at $SK_ALERT -- printing alert instead:" >&2
        printf '[%s] %s\n' "$level" "$*" >&2
    fi
}

if [[ ! -x "$SKMEMORY" ]]; then
    echo "FATAL: skmemory CLI not found at $SKMEMORY" >&2
    send_alert crit "skmemory-sync-audit: skmemory CLI not found at $SKMEMORY -- audit cannot run on $(hostname)"
    exit 2
fi

# Discover every agent with a memory home on THIS host -- not just agents
# with an active timer. An agent home with no timer at all is exactly the
# gap this fix closes.
mapfile -t AGENTS_WITH_HOMES < <(
    find "$AGENTS_DIR" -maxdepth 2 -type d -name memory 2>/dev/null \
        | sed -E "s#^${AGENTS_DIR}/([^/]+)/memory\$#\\1#"
)

if [[ ${#AGENTS_WITH_HOMES[@]} -eq 0 ]]; then
    echo "No agent memory homes found under $AGENTS_DIR. Nothing to audit."
    exit 0
fi

report=""
problems=0
zero_timer_agents=()

for agent in "${AGENTS_WITH_HOMES[@]}"; do
    timer="skmemory-sync@${agent}.timer"
    timer_enabled="$(systemctl --user is-enabled "$timer" 2>/dev/null || true)"
    [[ -z "$timer_enabled" ]] && timer_enabled="disabled"

    if [[ "$timer_enabled" != "enabled" ]]; then
        zero_timer_agents+=("$agent")
        line="- $agent -- NO enabled skmemory-sync timer (state=$timer_enabled) -> ZERO SYNC COVERAGE"
        echo "$line"
        report="${report}${line}"$'\n'
        problems=$((problems + 1))
        continue
    fi

    health_json="$(SKAGENT="$agent" "$SKMEMORY" health 2>/dev/null || echo '{}')"
    status="$(echo "$health_json" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read() or '{}')
print(d.get('status', d.get('primary', {}).get('ok', '?')))
" 2>/dev/null || echo '?')"
    sqlite_only="$(echo "$health_json" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read() or '{}')
print(d.get('primary', {}).get('sync', {}).get('sqlite_only', '?'))
" 2>/dev/null || echo '?')"
    flat_only="$(echo "$health_json" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read() or '{}')
print(d.get('primary', {}).get('sync', {}).get('flat_only', '?'))
" 2>/dev/null || echo '?')"
    in_sync="$(echo "$health_json" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read() or '{}')
print(d.get('primary', {}).get('sync', {}).get('in_sync', '?'))
" 2>/dev/null || echo '?')"

    log_file="$HOME/.skcapstone/agents/$agent/logs/skmemory-sync.log"
    err_count=0
    if [[ -f "$log_file" ]]; then
        err_count="$(awk '
            /[Cc]hroma|ChromaDB/ { next }
            /Error|ERROR|failed|Traceback/ { print }
        ' "$log_file" | wc -l)"
    fi

    flag=""
    if [[ "$status" == "degraded" || "$status" == "red" ]]; then
        flag="DRIFT STATUS=$status"
        problems=$((problems + 1))
    elif [[ "$in_sync" != "True" && "$in_sync" != "true" ]]; then
        flag="DRIFT (sqlite_only=$sqlite_only flat_only=$flat_only)"
        problems=$((problems + 1))
    fi
    if [[ "$err_count" -gt 0 ]]; then
        flag="${flag}${flag:+ + }${err_count} log err(s)"
        problems=$((problems + 1))
    fi
    [[ -z "$flag" ]] && flag="clean"

    line="- $agent -- status=$status sync=$in_sync sqlite_only=$sqlite_only flat_only=$flat_only log_errors_7d=$err_count -> $flag"
    echo "$line"
    report="${report}${line}"$'\n'
done

host="$(hostname)"
header="skmemory-sync audit ($host, $DATE)"

if [[ "$problems" -eq 0 ]]; then
    summary="$header

All ${#AGENTS_WITH_HOMES[@]} agent(s) with a memory home have an enabled sync timer and clean drift. No log errors in last 7d.

$report"
else
    summary="$header

$problems issue(s) found across ${#AGENTS_WITH_HOMES[@]} agent(s) with a memory home.

$report
Fix a drift/log issue: systemctl --user start skmemory-sync@<agent>.service (the managed unit: serialized with its timer, logged, stub-safe). Do not run export-flat or an ad-hoc sync on an old skmemory: they can write truncated previews into the flat source of truth.
Fix a missing timer: systemctl --user enable --now skmemory-sync@<agent>.timer (or re-run scripts/install.sh)"
fi

if [[ "${AUDIT_QUIET:-0}" == "1" && "$problems" -eq 0 ]]; then
    echo "AUDIT_QUIET=1 and clean -- skipping alert."
    exit 0
fi

level="info"
[[ "$problems" -gt 0 ]] && level="warn"
[[ ${#zero_timer_agents[@]} -gt 0 ]] && level="crit"

send_alert "$level" "$summary"
echo "Sent audit summary (level=$level, problems=$problems, zero_timer_agents=${#zero_timer_agents[@]})."
