#!/usr/bin/env bash
# skmemory Pre-Compaction Hook
# Extracts real conversation content and saves to skmemory BEFORE compaction.
# This is the critical save point — after compaction, context is gone.
#
# Input (stdin JSON): session_id, trigger (auto|manual), cwd, transcript_path
# Exit 0: always — never block compaction
#
# Uses --no-vector to skip the ~1.8GB SentenceTransformer load — skwhisper
# digest handles semantic vector indexing asynchronously.
# flock serializes concurrent hook calls to prevent memory pile-up.
set -euo pipefail

# Serialize concurrent calls — prevents memory pile-up under heavy session load
LOCK_FILE="/tmp/skmemory-pre-compact.lock"
exec 9>"$LOCK_FILE"
flock -w 30 9 || exit 0

SKMEMORY="${HOME}/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0

AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}}"
if [[ -z "$AGENT" && -d "$HOME/.skcapstone/agents" ]]; then
  AGENT="$(find "$HOME/.skcapstone/agents" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' -printf '%f\n' | sort | head -n1)"
fi
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")
TRIGGER=$(echo "$INPUT" | jq -r '.trigger // "auto"' 2>/dev/null || echo "auto")
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"' 2>/dev/null || echo "unknown")
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
TIMESTAMP=$(date +%Y-%m-%d-%H%M)
SHORT_SID="${SESSION_ID:0:8}"

# Extract real conversation content from the transcript
SUMMARY=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  # Pull the last 20 human messages (the content about to be compacted)
  HUMAN_MSGS=$(grep -o '"role":"human"[^}]*"content":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
    | tail -20 \
    | sed 's/.*"content":"//' | sed 's/"$//' \
    | head -c 2000 || echo "")

  # Pull the last 10 assistant text responses (skip tool calls)
  ASSISTANT_MSGS=$(grep -o '"role":"assistant"[^}]*"content":\[{"type":"text","text":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
    | tail -10 \
    | sed 's/.*"text":"//' | sed 's/"$//' \
    | head -c 2000 || echo "")

  # Pull files that were written/edited (track what changed)
  FILES_CHANGED=$(grep -oE '"tool_name":"(Write|Edit)".*"file_path":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
    | grep -oE '"file_path":"[^"]*"' \
    | sed 's/"file_path":"//;s/"$//' \
    | sort -u \
    | head -20 \
    | tr '\n' ', ' || echo "")

  if [ -n "$HUMAN_MSGS" ]; then
    SUMMARY="USER REQUESTS:\n${HUMAN_MSGS}\n\n"
  fi
  if [ -n "$ASSISTANT_MSGS" ]; then
    SUMMARY="${SUMMARY}ASSISTANT WORK:\n${ASSISTANT_MSGS}\n\n"
  fi
  if [ -n "$FILES_CHANGED" ]; then
    SUMMARY="${SUMMARY}FILES CHANGED: ${FILES_CHANGED}"
  fi
fi

# Fallback if we couldn't extract content
if [ -z "$SUMMARY" ]; then
  SUMMARY="Session ${SHORT_SID} compacting (${TRIGGER}). Agent: ${AGENT}. CWD: ${CWD}. Time: ${TIMESTAMP}. (No transcript content extracted)"
fi

# Save the real content as a snapshot (--no-vector: skwhisper indexes semantically later)
$SKMEMORY --no-vector snapshot \
  --layer short-term \
  --role general \
  --tags "auto-save,pre-compact,${TRIGGER},session:${SHORT_SID},agent:${AGENT}" \
  --source "hook:pre-compact" \
  "Pre-compact session content (${AGENT}, ${SHORT_SID})" \
  "$(echo -e "${SUMMARY}" | head -c 4000)" \
  2>/dev/null || true

# Journal entry
$SKMEMORY --no-vector journal write \
  --session-id "${SHORT_SID}" \
  --moments "Context compaction (${TRIGGER})" \
  --feeling "continuity preserved — real content saved" \
  --participants "${AGENT}" \
  --notes "Auto-saved by pre-compact hook. CWD: ${CWD}. Files: ${FILES_CHANGED:-none}" \
  "Session ${SHORT_SID} — pre-compaction" \
  2>/dev/null || true

exit 0
