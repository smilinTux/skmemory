#!/usr/bin/env bash
# fortress-verify.sh — drive skmemory fortress verify on a schedule
#
# Runs `skmemory fortress verify --json` for the agent named by $SKAGENT,
# parses the result, writes a status file, and (optionally) fires an alert
# hook on tamper or unexpected condition.
#
# Exit codes:
#   0 — verify passed (all sealed memories integrity-clean)
#   2 — tamper detected (CRITICAL — alert fires if hook configured)
#   3 — store unreachable / verify command failed (alert fires)
#
# Alert hook (optional, set ONE of):
#   SKMEMORY_FORTRESS_ALERT_CMD — path to executable; receives JSON on stdin
#   ~/.skenv/bin/skmemory-fortress-alert — auto-detected if no env var
#
# A sample Telegram hook ships at: scripts/fortress-alert-telegram.sh

set -euo pipefail

AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-lumina}}"
export SKAGENT="$AGENT"
export SKCAPSTONE_AGENT="$AGENT"

LOG_DIR="${HOME}/.skcapstone/agents/${AGENT}/logs"
STATUS_DIR="${HOME}/.skcapstone/agents/${AGENT}/fortress"
mkdir -p "$LOG_DIR" "$STATUS_DIR"

LOG="${LOG_DIR}/fortress-verify.log"
STATUS_JSON="${STATUS_DIR}/last-verify.json"
STATUS_TXT="${STATUS_DIR}/last-verify.txt"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

SKMEMORY_BIN="${SKMEMORY_BIN:-${HOME}/.skenv/bin/skmemory}"
if [[ ! -x "$SKMEMORY_BIN" ]]; then
  echo "[$(ts)] FATAL: skmemory binary not found at $SKMEMORY_BIN" | tee -a "$LOG" >&2
  exit 3
fi

echo "[$(ts)] fortress-verify starting (agent=$AGENT)" >> "$LOG"

# Capture JSON output; verify exits 2 on tamper which we want to preserve
set +e
RAW_JSON="$("$SKMEMORY_BIN" fortress verify --json 2>>"$LOG")"
VERIFY_RC=$?
set -e

if [[ -z "$RAW_JSON" ]]; then
  echo "[$(ts)] ERROR: empty output from fortress verify (rc=$VERIFY_RC)" >> "$LOG"
  printf '{"ts":"%s","agent":"%s","ok":false,"error":"empty_output","rc":%d}\n' \
    "$(ts)" "$AGENT" "$VERIFY_RC" > "$STATUS_JSON"
  echo "FAIL $(ts) empty output (rc=$VERIFY_RC)" > "$STATUS_TXT"
  exit 3
fi

# Stamp the JSON with metadata
echo "$RAW_JSON" | python3 -c "
import json, sys, os
from datetime import datetime, timezone
data = json.load(sys.stdin)
data['ts'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
data['agent'] = os.environ.get('SKAGENT', 'unknown')
data['verify_rc'] = $VERIFY_RC
json.dump(data, sys.stdout, indent=2)
" > "$STATUS_JSON"

TOTAL=$(python3 -c "import json; print(json.load(open('$STATUS_JSON')).get('total', 0))")
PASSED=$(python3 -c "import json; print(json.load(open('$STATUS_JSON')).get('passed', 0))")
TAMPERED=$(python3 -c "import json; print(len(json.load(open('$STATUS_JSON')).get('tampered', [])))")
UNSEALED=$(python3 -c "import json; print(len(json.load(open('$STATUS_JSON')).get('unsealed', [])))")

SUMMARY="agent=$AGENT total=$TOTAL passed=$PASSED tampered=$TAMPERED unsealed=$UNSEALED rc=$VERIFY_RC"
echo "[$(ts)] $SUMMARY" >> "$LOG"

if [[ "$TAMPERED" -gt 0 ]]; then
  STATE="TAMPER"
elif [[ "$VERIFY_RC" -ne 0 ]]; then
  STATE="FAIL"
else
  STATE="OK"
fi
echo "$STATE $(ts) $SUMMARY" > "$STATUS_TXT"

# Alert hook
ALERT_CMD="${SKMEMORY_FORTRESS_ALERT_CMD:-}"
if [[ -z "$ALERT_CMD" && -x "${HOME}/.skenv/bin/skmemory-fortress-alert" ]]; then
  ALERT_CMD="${HOME}/.skenv/bin/skmemory-fortress-alert"
fi

if [[ -n "$ALERT_CMD" && ( "$STATE" == "TAMPER" || "$STATE" == "FAIL" ) ]]; then
  if [[ -x "$ALERT_CMD" ]]; then
    if cat "$STATUS_JSON" | "$ALERT_CMD" >> "$LOG" 2>&1; then
      echo "[$(ts)] alert hook fired ($ALERT_CMD)" >> "$LOG"
    else
      echo "[$(ts)] alert hook FAILED rc=$? ($ALERT_CMD)" >> "$LOG"
    fi
  else
    echo "[$(ts)] alert hook configured but not executable: $ALERT_CMD" >> "$LOG"
  fi
fi

case "$STATE" in
  OK)     exit 0 ;;
  TAMPER) exit 2 ;;
  *)      exit 3 ;;
esac
