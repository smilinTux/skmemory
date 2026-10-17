#!/usr/bin/env bash
# Test: scripts/skcapstone-backup.sh's gfs_tiers_for_today() decision logic.
#
# Card: skmemory sqlite-only-orphans, 2026-10-16. The laptop's off-node
# snapshot of .158's whole ~/.skcapstone tree kept only 7 daily
# generations -- not nearly enough to reach back to April, when that
# incident's corruption actually started. This adds weekly/monthly
# promotion tiers.
#
# Strategy: source the script. Its BASH_SOURCE guard means sourcing it
# only defines functions -- no ssh, no rsync, no hostname check runs --
# so the pure decision function can be tested directly, with no stubs.

set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/skcapstone-backup.sh"
# shellcheck disable=SC1090
source "$SCRIPT"

fail=0

assert_tiers() {
    local desc="$1" dow="$2" dom="$3" expected="$4"
    local got
    got="$(gfs_tiers_for_today "$dow" "$dom" 8 12)"
    if [[ "$got" != "$expected" ]]; then
        echo "FAIL: $desc -- expected [$expected], got [$got]"
        fail=1
    else
        echo "PASS: $desc"
    fi
}

assert_tiers "ordinary Tuesday, mid-month: no promotions" 2 15 ""
assert_tiers "Sunday, mid-month: weekly only" 7 15 "weekly 8"
assert_tiers "Thursday the 1st: monthly only" 4 01 "monthly 12"
assert_tiers "Sunday the 1st: both tiers" 7 01 $'weekly 8\nmonthly 12'
assert_tiers "Saturday, not the 1st: nothing" 6 02 ""

# Retain counts must flow through, not just the tier names.
got="$(gfs_tiers_for_today 7 01 3 5)"
if [[ "$got" != $'weekly 3\nmonthly 5' ]]; then
    echo "FAIL: custom retain counts not passed through -- got [$got]"
    fail=1
else
    echo "PASS: custom retain counts flow through"
fi

# Sourcing the script must not have executed main() -- no ssh/rsync/log
# calls, no hostname check. If it did, this process would have tried to
# reach a real "laptop" host and this test would hang or error loudly.
if declare -F main >/dev/null && ! declare -F gfs_tiers_for_today >/dev/null; then
    echo "FAIL: gfs_tiers_for_today not defined after sourcing"
    fail=1
else
    echo "PASS: sourcing defined functions without executing main()"
fi

exit $fail
