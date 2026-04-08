#!/usr/bin/env bash
# skmemory Post-Compaction Reinject Hook
# Re-injects memory context after Claude Code compacts conversation.
# Fires on SessionStart when source is "compact".
#
# Input (stdin JSON): session_id, source (compact|resume|startup|clear)
# Exit 0: stdout is injected into Claude's context
set -euo pipefail

SKMEMORY="${HOME}/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0  # Skip silently if skmemory not installed

AGENT="${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}"
if [[ -z "$AGENT" && -d "$HOME/.skcapstone/agents" ]]; then
  AGENT="$(find "$HOME/.skcapstone/agents" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' -printf '%f\n' | sort | head -n1)"
fi
AGENT_DIR="${HOME}/.skcapstone/agents/${AGENT}"

# Generate token-efficient memory context
CONTEXT=$($SKMEMORY context --max-tokens 500 --strongest 3 --recent 5 2>/dev/null || echo "(no context available)")

# Recent journal entries
JOURNAL=$($SKMEMORY journal read 2>/dev/null | tail -15 || echo "(no journal entries)")

# SKWhisper subconscious context (survives compaction)
WHISPER=""
WHISPER_PATH="${AGENT_DIR}/skwhisper/whisper.md"
if [ -f "$WHISPER_PATH" ]; then
  WHISPER=$(cat "$WHISPER_PATH")
fi

cat <<EOF
--- SKMEMORY REHYDRATION (auto-injected after compaction) ---
Agent: ${AGENT}
Save memories: skmemory snapshot --layer mid-term --tags "tags" "Title" "Content"
Search: skmemory search "query"

=== SUBCONSCIOUS (SKWhisper) ===
${WHISPER:-"(no whisper context)"}

Recent context:
${CONTEXT}

Recent journal:
${JOURNAL}
--- END SKMEMORY ---
EOF

exit 0
