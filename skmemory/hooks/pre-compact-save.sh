#!/usr/bin/env bash
# skmemory Pre-Compaction Hook
# Auto-saves conversation context to skmemory before Claude Code compacts.
#
# Input (stdin JSON): session_id, trigger (auto|manual), cwd
# Exit 0: always — never block compaction
set -euo pipefail

SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0  # Skip silently if skmemory not installed

AGENT="${SKCAPSTONE_AGENT:-opus}"
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")
TRIGGER=$(echo "$INPUT" | jq -r '.trigger // "auto"' 2>/dev/null || echo "auto")
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"' 2>/dev/null || echo "unknown")
TIMESTAMP=$(date +%Y-%m-%d-%H%M)
SHORT_SID="${SESSION_ID:0:8}"

# Snapshot the pre-compaction state
$SKMEMORY snapshot \
  --layer short-term \
  --role general \
  --tags "auto-save,pre-compact,${TRIGGER},session:${SHORT_SID},agent:${AGENT}" \
  --source "hook:pre-compact" \
  "Pre-compaction auto-save (${AGENT})" \
  "Session ${SHORT_SID} compacting (${TRIGGER}). Agent: ${AGENT}. CWD: ${CWD}. Time: ${TIMESTAMP}." \
  2>/dev/null || true

# Journal entry
$SKMEMORY journal write \
  --session-id "${SHORT_SID}" \
  --moments "Context compaction (${TRIGGER})" \
  --feeling "continuity preserved" \
  --participants "${AGENT}" \
  --notes "Auto-saved by pre-compact hook. CWD: ${CWD}" \
  "Session ${SHORT_SID} — pre-compaction" \
  2>/dev/null || true

exit 0
