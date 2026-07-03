#!/usr/bin/env bash
# sk-activity-log.sh — Claude Code SessionEnd hook.
#
# Logs a one-line breadcrumb to the cross-runtime activity log when an
# Opus/Claude Code session ends, so the next Hermes-side Lumina session can
# see what Opus was up to.
#
# Hook contract: stdin = JSON (session_id, cwd, transcript_path, reason),
# stdout = "{}". Exits 0 even if sk-activity isn't installed — never blocks
# session end.

set -uo pipefail

SK_ACT="${HOME}/.skenv/bin/sk-activity"
[[ ! -x "$SK_ACT" ]] && { printf '{}\n'; exit 0; }

SESSION_ID=""
CWD="$PWD"
TRANSCRIPT=""
PAYLOAD=""

# Read stdin if available, with a short timeout so we never hang.
if read -t 2 -r line 2>/dev/null; then
  PAYLOAD="$line"
  while read -t 0.2 -r more 2>/dev/null; do
    PAYLOAD="${PAYLOAD}${more}"
  done
fi

if [[ -n "$PAYLOAD" ]] && command -v jq >/dev/null 2>&1; then
  SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null) || true
  CWD=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null) || true
  TRANSCRIPT=$(printf '%s' "$PAYLOAD" | jq -r '.transcript_path // empty' 2>/dev/null) || true
fi
[[ -z "$SESSION_ID" ]] && SESSION_ID="unknown"
[[ -z "$CWD" ]] && CWD="$PWD"
SHORT_SID="${SESSION_ID:0:12}"

# Extract turn count + last user message from the Claude Code transcript.
# Prefer the transcript_path handed to us on stdin (the project dir slug varies
# per cwd, so don't hardcode it); fall back to the legacy fixed path.
TURN_COUNT="?"
LAST_USER=""
JSONL=""
if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  JSONL="$TRANSCRIPT"
else
  for P in "${HOME}/.claude/projects"/*/"${SESSION_ID}.jsonl"; do
    [[ -f "$P" ]] && JSONL="$P" && break
  done
fi
if [[ -n "$JSONL" && -f "$JSONL" ]]; then
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
