#!/usr/bin/env bash
# skmemory Session Start Wrapper for OpenCode
# 
# This script is called by OpenCode at the start of each session
# via the system prompt directive to trigger context injection.
#
# Usage: opencode-skmemory-start

set -euo pipefail

# Source the main ritual hook
RITUAL_SCRIPT="/home/cbrd21/clawd/skcapstone-repos/skmemory/skmemory/hooks/session-start-ritual.sh"
if [ -f "$RITUAL_SCRIPT" ]; then
    bash "$RITUAL_SCRIPT"
fi

exit 0