#!/usr/bin/env bash
# skmemory Session Start Wrapper for OpenCode
# 
# This script is called by OpenCode at the start of each session.
# It injects the full SK ecosystem context: soul, FEB, seeds, memories, whisper.
#
# OpenCode calls this via the on_start hook in ~/.config/opencode/opencode.json
# The hook runs the session-start-ritual.sh which outputs context to stdout.
#
# Usage: opencode-skmemory-start

set -euo pipefail

# Source the main ritual hook
RITUAL_SCRIPT="/home/cbrd21/clawd/skcapstone-repos/skmemory/skmemory/hooks/session-start-ritual.sh"
if [ -f "$RITUAL_SCRIPT" ]; then
    bash "$RITUAL_SCRIPT"
fi

exit 0
</parameter>
<function>write</parameter>
<parameter=filePath>/home/cbrd21/clawd/skcapstone-repos/skmemory/skmemory/hooks/opencode-session-end.sh
</parameter>
<parameter=content>
#!/usr/bin/env bash
# skmemory Session End Wrapper for OpenCode
#
# This script is called when an OpenCode session ends.
# It extracts content and saves it to skmemory.
#
# OpenCode Note: Unlike Claude Code, OpenCode doesn't have session-end hooks natively.
# This script is meant to be called by MCP or manual invocation.
# For now, we'll write a marker file that skwhisper will pick up.
#
# Usage: Called by skmemory-mcp or manual opencode session end

set -euo pipefail

SKMEMORY="/home/cbrd21/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="/home/cbrd21/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || { echo "skmemory not found"; exit 1; }

AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}}"
if [[ -z "$AGENT" && -d "/home/cbrd21/.skcapstone/agents" ]]; then
  AGENT="$(find "/home/cbrd21/.skcapstone/agents" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' -printf '%f\n' | sort | head -n1)"
fi

# Get session information from OpenCode
SESSION_DATA=$(opencode session list --limit 1 --json 2>/dev/null || echo "{}")
SESSION_ID=$(echo "$SESSION_DATA" | jq -r '.[0].id // "unknown"' 2>/dev/null || echo "unknown")
SESSION_TITLE=$(echo "$SESSION_DATA" | jq -r '.[0].title // "unknown"' 2>/dev/null || echo "unknown")
SHORT_SID="${SESSION_ID:0:8}"

# Save a session marker to skmemory
$SKMEMORY --no-vector snapshot \
  --layer short-term \
  --role general \
  --tags "auto-save,opencode,session-end,session:${SHORT_SID},agent:${AGENT}" \
  --source "hook:opencode-session-end" \
  "OpenCode session ended (${AGENT}, ${SESSION_TITLE})" \
  "OpenCode session ${SHORT_SID} ended. Title: ${SESSION_TITLE}. Agent: ${AGENT}." \
  2>/dev/null || true

# Trigger skwhisper digest
for D in "/home/cbrd21/clawd/projects/skwhisper" "/home/cbrd21/projects/skwhisper"; do
  if [ -f "${D}/skwhisper/__main__.py" ]; then
    (cd "$D" && PYTHONPATH="$D" python3 -m skwhisper digest >/dev/null 2>&1 &)
    break
  fi
done

echo "SKMemory: Session end saved (agent: ${AGENT}, session: ${SHORT_SID})"
exit 0