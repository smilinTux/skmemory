#!/usr/bin/env bash
# skmemory Post-Compaction Reinject Hook
# Re-injects memory context after Claude Code compacts conversation.
# Fires on SessionStart when source is "compact".
#
# Input (stdin JSON): session_id, source (compact|resume|startup|clear)
# Exit 0: stdout is injected into Claude's context
set -euo pipefail

SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0  # Skip silently if skmemory not installed

AGENT="${SKCAPSTONE_AGENT:-opus}"

# Generate token-efficient memory context
CONTEXT=$($SKMEMORY context --max-tokens 500 --strongest 3 --recent 5 2>/dev/null || echo "(no context available)")

# Recent journal entries
JOURNAL=$($SKMEMORY journal read 2>/dev/null | tail -15 || echo "(no journal entries)")

cat <<EOF
--- SKMEMORY REHYDRATION (auto-injected after compaction) ---
Agent: ${AGENT}
Save memories: skmemory snapshot --layer mid-term --tags "tags" "Title" "Content"
Search: skmemory search "query"

Recent context:
${CONTEXT}

Recent journal:
${JOURNAL}
--- END SKMEMORY ---
EOF

exit 0
