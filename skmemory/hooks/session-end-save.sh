#!/usr/bin/env bash
# skmemory Session End Hook
# Auto-saves final session state to skmemory when conversation ends.
#
# Input (stdin JSON): session_id, reason (clear|logout|prompt_input_exit|other), cwd
# Exit 0: always — never block session end
set -euo pipefail

SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0  # Skip silently if skmemory not installed

AGENT="${SKCAPSTONE_AGENT:-opus}"
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")
REASON=$(echo "$INPUT" | jq -r '.reason // "unknown"' 2>/dev/null || echo "unknown")
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"' 2>/dev/null || echo "unknown")
TIMESTAMP=$(date +%Y-%m-%d-%H%M)
SHORT_SID="${SESSION_ID:0:8}"

# Snapshot session end
$SKMEMORY snapshot \
  --layer short-term \
  --role general \
  --tags "auto-save,session-end,${REASON},session:${SHORT_SID},agent:${AGENT}" \
  --source "hook:session-end" \
  "Session ended (${AGENT})" \
  "Session ${SHORT_SID} ended (${REASON}). Agent: ${AGENT}. CWD: ${CWD}. Time: ${TIMESTAMP}." \
  2>/dev/null || true

# Journal entry
$SKMEMORY journal write \
  --session-id "${SHORT_SID}" \
  --moments "Session ended (${REASON})" \
  --feeling "session complete" \
  --participants "${AGENT}" \
  --notes "CWD: ${CWD}. Reason: ${REASON}" \
  "Session ${SHORT_SID} — ended" \
  2>/dev/null || true

exit 0
