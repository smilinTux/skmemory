#!/usr/bin/env bash
# skmemory Stop Checkpoint Hook
# Lightweight checkpoint on every Claude response completion.
# Writes a breadcrumb so that if the system OOM's or crashes,
# we know what the last completed action was.
#
# Input (stdin JSON): session_id, cwd, stop_reason, transcript_path
# Exit 0: always — never block
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
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
SHORT_SID="${SESSION_ID:0:8}"
CHECKPOINT_FILE="${HOME}/.skcapstone/agents/${AGENT}/memory/.last_checkpoint"

# Only checkpoint every 5th stop to avoid spamming
# Use a simple counter file
COUNTER_FILE="${TMPDIR:-/tmp}/skmemory-stop-counter-${SHORT_SID}"
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo "0")
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

# Checkpoint every 5 stops
if [ $((COUNT % 5)) -ne 0 ]; then
  exit 0
fi

# Write a lightweight checkpoint with the last assistant message
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  LAST_WORK=$(tail -50 "$TRANSCRIPT" 2>/dev/null \
    | grep -oE '"type":"text","text":"[^"]{20,}"' \
    | tail -1 \
    | sed 's/"type":"text","text":"//' | sed 's/"$//' \
    | head -c 500 || echo "")

  LAST_FILE=$(tail -50 "$TRANSCRIPT" 2>/dev/null \
    | grep -oE '"file_path":"[^"]*"' \
    | tail -1 \
    | sed 's/"file_path":"//;s/"$//' || echo "")
fi

# Write checkpoint file (fast, no skmemory call)
cat > "$CHECKPOINT_FILE" <<EOF
{
  "session_id": "${SHORT_SID}",
  "agent": "${AGENT}",
  "stop_number": ${COUNT},
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "last_work": "$(echo "${LAST_WORK:-}" | sed 's/"/\\"/g' | head -c 300)",
  "last_file": "${LAST_FILE:-}"
}
EOF

exit 0
