#!/usr/bin/env bash
# skmemory Session End Hook
# Extracts real conversation content and saves to skmemory when session ends.
# This is the last chance to capture what happened before the session is gone.
#
# Input (stdin JSON): session_id, reason (clear|logout|prompt_input_exit|other), cwd, transcript_path
# Exit 0: always — never block session end
#
# Uses --no-vector to skip the ~1.8GB SentenceTransformer load — these are
# breadcrumb writes. skwhisper digest handles semantic vector indexing async.
#
# The skmemory CLI writes take ~13s (double CLI cold-start + DB writes). Running
# them synchronously held Claude Code's stdout pipe open until they finished, so
# on interactive exit the harness killed the hook mid-write ("Hook cancelled")
# and nothing got saved. Fix (mirrors skwhisper-save.sh 2026-06-17): parse the
# transcript synchronously (fast), then fire the writes fully DETACHED —
#   - flock -n : single-flight, skip if another session-end save is running
#   - setsid + </dev/null >/dev/null 2>&1 : don't inherit Claude's stdout pipe
#     and survive Claude's process-group teardown so the write actually lands.
set -uo pipefail

LOCK_FILE="/tmp/skmemory-session-end.lock"

SKMEMORY="${HOME}/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0

AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}}"
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
  # Count conversation turns for context.
  # Claude Code transcripts use "role":"user" (nested under .message), not "human".
  # Use `|| true` (not `|| echo 0`): grep -c already prints "0" and exits 1 on no
  # match, so `|| echo 0` would append a second "0" and break the integer compare.
  HUMAN_COUNT=$(grep -c '"role":"user"' "$TRANSCRIPT" 2>/dev/null || true)
  HUMAN_COUNT=${HUMAN_COUNT:-0}

  # Skip trivial sessions (< 3 human messages = nothing worth saving beyond the marker)
  if [ "$HUMAN_COUNT" -ge 3 ]; then
    # Pull user messages (what was asked)
    HUMAN_MSGS=$(grep -o '"role":"user"[^}]*"content":"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
      | tail -30 \
      | sed 's/.*"content":"//' | sed 's/"$//' \
      | head -c 2000 || echo "")

    # Pull assistant text responses
    ASSISTANT_MSGS=$(grep -oE '"type":"text","text":"[^"]{20,}"' "$TRANSCRIPT" 2>/dev/null \
      | tail -15 \
      | sed 's/"type":"text","text":"//' | sed 's/"$//' \
      | head -c 2000 || echo "")

    # Track files changed. Claude Code records edits as tool_use blocks (name
    # Write/Edit/MultiEdit) with the path under .input.file_path, inside the
    # .message.content array — not a flat "tool_name"/"file_path" pair. jq with
    # `fromjson?` extracts robustly and tolerates any non-JSON lines.
    FILES_CHANGED=$(jq -rR 'fromjson? | (.message.content // empty)
      | if type=="array" then .[] else empty end
      | select(type=="object" and .type=="tool_use"
               and (.name=="Write" or .name=="Edit" or .name=="MultiEdit"))
      | .input.file_path // empty' "$TRANSCRIPT" 2>/dev/null \
      | sort -u \
      | head -30 \
      | tr '\n' ', ' || echo "")

    # Track git commits made
    GIT_COMMITS=$(grep -oE 'git commit -m[^"]*"[^"]*"' "$TRANSCRIPT" 2>/dev/null \
      | head -5 \
      | tr '\n' '; ' || echo "")

    # High-signal facts (files, commits) go first so they survive the 4000-char
    # CONTENT cap even when the verbose message dumps below are long.
    SUMMARY="TURNS: ${HUMAN_COUNT}\n"
    if [ -n "$FILES_CHANGED" ]; then
      SUMMARY="${SUMMARY}FILES CHANGED: ${FILES_CHANGED}\n"
    fi
    if [ -n "$GIT_COMMITS" ]; then
      SUMMARY="${SUMMARY}GIT COMMITS: ${GIT_COMMITS}\n"
    fi
    if [ -n "$HUMAN_MSGS" ]; then
      SUMMARY="${SUMMARY}\nUSER REQUESTS:\n${HUMAN_MSGS}\n\n"
    fi
    if [ -n "$ASSISTANT_MSGS" ]; then
      SUMMARY="${SUMMARY}WORK DONE:\n${ASSISTANT_MSGS}\n\n"
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

# Fire the two skmemory writes fully detached so the hook returns immediately
# and can't be cancelled mid-write on session exit. Values pass through the
# environment (exported) into the setsid'd bash -c. flock -n gives single-flight.
export SKMEMORY LOCK_FILE LAYER REASON SHORT_SID AGENT CONTENT CWD
export HUMAN_COUNT="${HUMAN_COUNT:-0}"
export FILES_CHANGED="${FILES_CHANGED:-none}"

DETACH=""; command -v setsid >/dev/null 2>&1 && DETACH="setsid"
$DETACH bash -c '
  exec 9>"$LOCK_FILE" || exit 0
  flock -n 9 || exit 0   # another session-end save already running — skip

  "$SKMEMORY" --no-vector snapshot \
    --layer "$LAYER" \
    --role general \
    --tags "auto-save,session-end,${REASON},session:${SHORT_SID},agent:${AGENT}" \
    --source "hook:session-end" \
    "Session ${SHORT_SID} ended (${AGENT}, ${HUMAN_COUNT} turns)" \
    "$CONTENT" >/dev/null 2>&1 || true

  "$SKMEMORY" --no-vector journal write \
    --session-id "$SHORT_SID" \
    --moments "Session ended (${REASON}), ${HUMAN_COUNT} turns" \
    --feeling "session complete — content preserved" \
    --participants "$AGENT" \
    --notes "CWD: ${CWD}. Reason: ${REASON}. Files: ${FILES_CHANGED}" \
    "Session ${SHORT_SID} — ended" >/dev/null 2>&1 || true
' </dev/null >/dev/null 2>&1 &

exit 0
