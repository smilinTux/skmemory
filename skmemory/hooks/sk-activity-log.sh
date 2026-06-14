#!/usr/bin/env bash
# sk-activity-log.sh — Claude Code SessionEnd hook.
#
# Logs a one-line breadcrumb to the cross-runtime activity log when an
# Opus/Claude Code session ends, so the next Hermes-side Lumina session can
# see what Opus was up to.
#
# Hermes-style hook: stdin = JSON, stdout = "{}"-or-empty.
# Claude Code passes session_id, source, etc. on stdin.

set -uo pipefail

SK_ACT="${HOME}/.skenv/bin/sk-activity"
[[ ! -x "$SK_ACT" ]] && exit 0

SESSION_ID=""
CWD="$PWD"
PAYLOAD=""

# Read stdin if available, with a short timeout
if read -t 2 -r line 2>/dev/null; then
  PAYLOAD="$line"
  while read -t 0.2 -r more 2>/dev/null; do
    PAYLOAD="${PAYLOAD}${more}"
  done
fi

if [[ -n "$PAYLOAD" ]] && command -v jq >/dev/null 2>&1; then
  SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null) || true
  CWD=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null) || true
fi
[[ -z "$SESSION_ID" ]] && SESSION_ID="unknown"
SHORT_SID="${SESSION_ID:0:12}"

# Try to extract turn count + last user message from the Claude Code transcript.
TURN_COUNT="?"
LAST_USER=""
PROJECT_DIR="${HOME}/.claude/projects/-home-cbrd21"
JSONL="${PROJECT_DIR}/${SESSION_ID}.jsonl"
if [[ -f "$JSONL" ]]; then
  TURN_COUNT=$(grep -c '"role":"user"' "$JSONL" 2>/dev/null || echo "?")
  if command -v jq >/dev/null 2>&1; then
    LAST_USER=$(jq -r 'select(.type=="user") | (.message.content // "") | tostring' "$JSONL" 2>/dev/null \
                | tail -1 | head -c 200 | tr '\n' ' ')
  fi
fi

SUMMARY="Claude Code session ended (${TURN_COUNT} turns, cwd=${CWD##*/})"
[[ -n "$LAST_USER" ]] && SUMMARY="${SUMMARY}; last user msg: ${LAST_USER}"

"$SK_ACT" log \
  --runtime claude_code \
  --kind session_end \
  --actor opus \
  --session "$SHORT_SID" \
  --summary "$SUMMARY" 2>/dev/null || true

# Hook contract: emit empty JSON
printf '{}\n'
exit 0
