#!/usr/bin/env bash
# Test: skmem-pg-backup.sh must promote daily dumps into weekly (Sunday)
# and monthly (1st) tiers with independent retention, on top of (not
# instead of) the existing daily retention.
#
# Card: skmemory sqlite-only-orphans, 2026-10-16 -- there was no long-term
# tier at all for skmem-pg; recovery had to mine three accidental,
# undocumented full dumps because 14 days of daily retention couldn't
# reach back far enough.
#
# Strategy: stub `docker` so the script never touches a real container,
# and drive day-of-week/day-of-month/timestamp via override env vars so
# the test is deterministic without needing to mock the real clock.

set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/deploy/ops/skmem-pg-backup.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin"
cat >"$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
# fake: docker exec skmem-pg pg_dump -U postgres -d skmemory -Fc
# just emit some deterministic non-empty bytes to stdout.
echo "fake-dump-content-$$"
STUB
chmod +x "$TMP/bin/docker"

DST="$TMP/backups"
fail=0

run_day() {
    local ts="$1" dow="$2" dom="$3"
    SKMEM_BACKUP_DIR="$DST" \
    SKMEM_BACKUP_TS_OVERRIDE="$ts" \
    SKMEM_BACKUP_DOW_OVERRIDE="$dow" \
    SKMEM_BACKUP_DOM_OVERRIDE="$dom" \
    SKMEM_BACKUP_RETAIN=14 \
    SKMEM_BACKUP_WEEKLY_RETAIN=2 \
    SKMEM_BACKUP_MONTHLY_RETAIN=2 \
    PATH="$TMP/bin:$PATH" \
        bash "$SCRIPT" >"$TMP/run-$ts.log" 2>&1
}

# Three "weeks," one Sunday each -- weekly tier should accumulate up to
# WEEKLY_RETAIN=2 and prune the oldest.
run_day "20260101-030000" 4 01   # Thu, 1st of month -> also monthly
run_day "20260104-030000" 7 04   # Sun -> weekly #1
run_day "20260111-030000" 7 11   # Sun -> weekly #2
run_day "20260118-030000" 7 18   # Sun -> weekly #3 (should prune #1)
run_day "20260201-030000" 7 01   # Sun AND 1st -> weekly #4 + monthly #2

weekly_count=$(ls -1 "$DST/weekly" 2>/dev/null | wc -l)
monthly_count=$(ls -1 "$DST/monthly" 2>/dev/null | wc -l)

if [[ "$weekly_count" -ne 2 ]]; then
    echo "FAIL: expected 2 files in weekly/ (retain=2), got $weekly_count"
    ls -la "$DST/weekly" 2>&1
    fail=1
else
    echo "PASS: weekly tier retained exactly 2 (pruned older Sundays)"
fi

if ! ls "$DST/weekly" 2>/dev/null | grep -q "20260118"; then
    echo "FAIL: expected the 2026-01-18 Sunday dump (2nd-newest) to survive pruning"
    fail=1
else
    echo "PASS: 2026-01-18 Sunday dump survived"
fi
if ! ls "$DST/weekly" 2>/dev/null | grep -q "20260201"; then
    echo "FAIL: expected the newest (2026-02-01) Sunday dump to survive"
    fail=1
else
    echo "PASS: newest Sunday dump present"
fi
if ls "$DST/weekly" 2>/dev/null | grep -q "20260104"; then
    echo "FAIL: oldest Sunday dump (2026-01-04) should have been pruned"
    fail=1
else
    echo "PASS: oldest Sunday dump correctly pruned"
fi
if ls "$DST/weekly" 2>/dev/null | grep -q "20260111"; then
    echo "FAIL: 2nd-oldest Sunday dump (2026-01-11) should have been pruned too (retain=2)"
    fail=1
else
    echo "PASS: 2nd-oldest Sunday dump correctly pruned"
fi

if [[ "$monthly_count" -ne 2 ]]; then
    echo "FAIL: expected 2 files in monthly/ (retain=2), got $monthly_count"
    ls -la "$DST/monthly" 2>&1
    fail=1
else
    echo "PASS: monthly tier has exactly 2 entries (Jan 1 + Feb 1)"
fi

# Daily tier must still have run on every invocation (5 dumps, under
# RETAIN=14, nothing pruned).
daily_count=$(ls -1 "$DST"/skmem-pg-skmemory-*.dump 2>/dev/null | wc -l)
if [[ "$daily_count" -ne 5 ]]; then
    echo "FAIL: expected 5 daily dumps (one per run, retain=14), got $daily_count"
    fail=1
else
    echo "PASS: daily tier unaffected, all 5 runs present"
fi

exit $fail
