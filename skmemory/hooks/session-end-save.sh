#!/usr/bin/env bash
# skmemory Session End Hook
# Extracts real conversation content and saves to skmemory when session ends.
# This is the last chance to capture what happened before the session is gone.
#
# Input (stdin JSON): session_id, reason (clear|logout|prompt_input_exit|other), cwd, transcript_path
# Exit 0: always — never block session end
set -euo pipefail

SKMEMORY="${HOME}/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0

AGENT="${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}"
if [[ -z "$AGENT" && -d "$HOME/.skcapstone/agents" ]]; then
  AGENT="$(find "$HOME/.skcapstone/agents" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' -printf '%f\n' | sort | head -n1)"
fi
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")
REASON=$(echo "$INPUT" | jq -r '.reason // "unknown"' 2>/dev/null || echo "unknown")
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"' 2>/dev/null || echo "unknown")
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
TIMESTAMP=$(date +%Y-%m-%d-%H%M)
SHORT_SID="${SESSION_ID:0:8}"

# Extract real conversation content from the transcript
SUMMARY=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  # Count conversation turns for context
  HUMAN_COUNT=$(grep -c '"role":"human"' "$TRANSCRIPT" 2>/dev/null || echo "0")

  # Skip trivial sessions (< 3 human messages = nothing worth saving beyond the marker)
  if [ "$HUMAN_COUNT" -ge 3 ]; then
    # Pull user messages (what was asked)
    HUMAN_MSGS=$(grep -o '"role":"human"[^}]*"content":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
      | tail -30 \
      | sed 's/.*"content":"//' | sed 's/"$//' \
      | head -c 2000 || echo "")

    # Pull assistant text responses
    ASSISTANT_MSGS=$(grep -oE '"type":"text","text":"[^"]{20,}"' "$TRANSCRIPT" 2>/dev/null \
      | tail -15 \
      | sed 's/"type":"text","text":"//' | sed 's/"$//' \
      | head -c 2000 || echo "")

    # Track files changed
    FILES_CHANGED=$(grep -oE '"tool_name":"(Write|Edit)".*"file_path":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
      | grep -oE '"file_path":"[^"]*"' \
      | sed 's/"file_path":"//;s/"$//' \
      | sort -u \
      | head -30 \
      | tr '\n' ', ' || echo "")

    # Track git commits made
    GIT_COMMITS=$(grep -oE 'git commit -m[^"]*"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
      | head -5 \
      | tr '\n' '; ' || echo "")

    SUMMARY="TURNS: ${HUMAN_COUNT}\n"
    if [ -n "$HUMAN_MSGS" ]; then
      SUMMARY="${SUMMARY}USER REQUESTS:\n${HUMAN_MSGS}\n\n"
    fi
    if [ -n "$ASSISTANT_MSGS" ]; then
      SUMMARY="${SUMMARY}WORK DONE:\n${ASSISTANT_MSGS}\n\n"
    fi
    if [ -n "$FILES_CHANGED" ]; then
      SUMMARY="${SUMMARY}FILES CHANGED: ${FILES_CHANGED}\n"
    fi
    if [ -n "$GIT_COMMITS" ]; then
      SUMMARY="${SUMMARY}GIT COMMITS: ${GIT_COMMITS}\n"
    fi
  fi
fi

# Determine the right memory layer based on session length
LAYER="short-term"
if [ "${HUMAN_COUNT:-0}" -ge 20 ]; then
  LAYER="mid-term"  # Substantial sessions get promoted
fi

# Always save at least a session marker
if [ -z "$SUMMARY" ]; then
  CONTENT="Session ${SHORT_SID} ended (${REASON}). Agent: ${AGENT}. CWD: ${CWD}. Time: ${TIMESTAMP}. Turns: ${HUMAN_COUNT:-0}."
else
  CONTENT=$(echo -e "${SUMMARY}" | head -c 4000)
fi

$SKMEMORY snapshot \
  --layer "${LAYER}" \
  --role general \
  --tags "auto-save,session-end,${REASON},session:${SHORT_SID},agent:${AGENT}" \
  --source "hook:session-end" \
  "Session ${SHORT_SID} ended (${AGENT}, ${HUMAN_COUNT:-0} turns)" \
  "${CONTENT}" \
  2>/dev/null || true

# Journal entry
$SKMEMORY journal write \
  --session-id "${SHORT_SID}" \
  --moments "Session ended (${REASON}), ${HUMAN_COUNT:-0} turns" \
  --feeling "session complete — content preserved" \
  --participants "${AGENT}" \
  --notes "CWD: ${CWD}. Reason: ${REASON}. Files: ${FILES_CHANGED:-none}" \
  "Session ${SHORT_SID} — ended" \
  2>/dev/null || true

exit 0
