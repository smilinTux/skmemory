#!/usr/bin/env bash
# Test: skmemory-sync-audit.sh must ALERT (not silently exit clean) when an
# agent has a memory home but no enabled skmemory-sync timer -- the exact
# gap that let 23,525 orphans go undetected for six months (card: skmemory
# sqlite-only-orphans, 2026-10-16). It must also still report cleanly when
# every agent's timer is enabled and healthy.
#
# Strategy: run the REAL script with HOME redirected to a throwaway dir and
# stub `systemctl`, `skmemory`, `sk-alert`, `hostname` on the resolved PATH,
# so no real systemd/CLI state is touched. We assert on what the script
# passed to the stubbed sk-alert.

set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/skmemory-sync-audit.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin"
PATH_STUB="$TMP/bin"

cat >"$PATH_STUB/hostname" <<'STUB'
#!/usr/bin/env bash
echo "test-host"
STUB
chmod +x "$PATH_STUB/hostname"

cat >"$PATH_STUB/sk-alert" <<STUB
#!/usr/bin/env bash
printf '%s\0' "\$@" >>"$TMP/alert-calls.log"
printf '\n---CALL---\n' >>"$TMP/alert-calls.log"
exit 0
STUB
chmod +x "$PATH_STUB/sk-alert"

run_case() {
    # run_case CASE_NAME ENABLED_AGENTS... -- ENABLED_AGENTS is the list of
    # agents whose timer `systemctl --user is-enabled` should report "enabled"
    # for. Agents listed in $ALL_AGENTS but not here report "disabled".
    local case_name="$1"
    shift
    local enabled_csv
    enabled_csv="$(printf '%s,' "$@")"

    rm -f "$TMP/alert-calls.log"
    local home="$TMP/home-$case_name"
    rm -rf "$home"
    mkdir -p "$home/.skcapstone/agents/lumina/memory" "$home/.skcapstone/agents/opus/memory"

    cat >"$PATH_STUB/systemctl" <<STUB
#!/usr/bin/env bash
# fake: systemctl --user is-enabled skmemory-sync@<agent>.timer
if [[ "\$1" == "--user" && "\$2" == "is-enabled" ]]; then
    unit="\$3"
    agent="\${unit#skmemory-sync@}"
    agent="\${agent%.timer}"
    case ",$enabled_csv," in
        *",\$agent,"*) echo "enabled"; exit 0 ;;
        *) echo "disabled"; exit 1 ;;
    esac
fi
exit 0
STUB
    chmod +x "$PATH_STUB/systemctl"

    cat >"$PATH_STUB/skmemory" <<'STUB'
#!/usr/bin/env bash
echo '{"status": "ok", "primary": {"ok": true, "sync": {"in_sync": true, "sqlite_only": 0, "flat_only": 0}}}'
STUB
    chmod +x "$PATH_STUB/skmemory"

    AUDIT_QUIET=0 LOG_DIR="$home/.skcapstone/logs" AGENTS_DIR="$home/.skcapstone/agents" \
        HOME="$home" PATH="$PATH_STUB:/usr/bin:/bin" SK_ALERT="$PATH_STUB/sk-alert" \
        SKMEMORY="$PATH_STUB/skmemory" bash "$SCRIPT" >"$home.out" 2>&1
}

fail=0

# Case 1: NEITHER agent has an enabled timer -- the exact 6-month gap.
run_case "zero-coverage"
if [[ ! -f "$TMP/alert-calls.log" ]]; then
    echo "FAIL: zero-coverage case sent NO alert at all (this is the original bug)"; fail=1
else
    echo "PASS: zero-coverage case sent an alert"
fi
if ! grep -qa '\-l' "$TMP/alert-calls.log" || ! grep -qa 'crit' "$TMP/alert-calls.log"; then
    echo "FAIL: zero-coverage case did not use crit level. Got:"; cat "$TMP/alert-calls.log"; fail=1
else
    echo "PASS: zero-coverage case alerted at crit level"
fi
if ! grep -qa 'ZERO SYNC COVERAGE' "$TMP/alert-calls.log"; then
    echo "FAIL: alert body missing 'ZERO SYNC COVERAGE' explanation"; fail=1
else
    echo "PASS: alert body names the zero-coverage condition"
fi

# Case 2: both agents have enabled, in-sync timers -- clean run.
run_case "all-healthy" lumina opus
if [[ ! -f "$TMP/alert-calls.log" ]]; then
    echo "FAIL: all-healthy case sent no status alert at all"; fail=1
elif grep -qa 'crit' "$TMP/alert-calls.log"; then
    echo "FAIL: all-healthy case alerted at crit level"; cat "$TMP/alert-calls.log"; fail=1
else
    echo "PASS: all-healthy case did not alert at crit level"
fi

# Case 3: AUDIT_QUIET=1 and all healthy -- must suppress the alert entirely.
rm -f "$TMP/alert-calls.log"
home="$TMP/home-quiet-healthy"
rm -rf "$home"
mkdir -p "$home/.skcapstone/agents/lumina/memory"
cat >"$PATH_STUB/systemctl" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "--user" && "$2" == "is-enabled" ]] && { echo "enabled"; exit 0; }
exit 0
STUB
chmod +x "$PATH_STUB/systemctl"
AUDIT_QUIET=1 LOG_DIR="$home/.skcapstone/logs" AGENTS_DIR="$home/.skcapstone/agents" \
    HOME="$home" PATH="$PATH_STUB:/usr/bin:/bin" SK_ALERT="$PATH_STUB/sk-alert" \
    SKMEMORY="$PATH_STUB/skmemory" bash "$SCRIPT" >/dev/null 2>&1
if [[ -f "$TMP/alert-calls.log" ]]; then
    echo "FAIL: AUDIT_QUIET=1 clean run still sent an alert"; fail=1
else
    echo "PASS: AUDIT_QUIET=1 clean run suppressed the alert"
fi

# Case 4: ZERO agent homes at all on this host -- genuinely nothing to
# audit, must exit 0 without crashing (not the same as zero coverage above,
# where homes exist but their timers don't).
home="$TMP/home-empty"
rm -rf "$home"
mkdir -p "$home/.skcapstone/agents"
rm -f "$TMP/alert-calls.log"
LOG_DIR="$home/.skcapstone/logs" AGENTS_DIR="$home/.skcapstone/agents" HOME="$home" \
    PATH="$PATH_STUB:/usr/bin:/bin" SK_ALERT="$PATH_STUB/sk-alert" \
    SKMEMORY="$PATH_STUB/skmemory" bash "$SCRIPT" >/dev/null 2>&1
rc=$?
if [[ "$rc" -ne 0 ]]; then
    echo "FAIL: empty-agents-dir case exited $rc (expected 0)"; fail=1
else
    echo "PASS: empty-agents-dir case exits cleanly"
fi

exit $fail
