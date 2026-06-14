#!/usr/bin/env bash
# sk-activity-inject.sh — Claude Code SessionStart hook.
#
# Emits a compact context block showing the last ~15 cross-runtime activity
# entries (Hermes + Claude Code). Lets Opus see what Lumina-on-Hermes was just
# doing, and vice versa.
#
# Never blocks: exits 0 even if sk-activity isn't installed.

set -uo pipefail

SK_ACT="${HOME}/.skenv/bin/sk-activity"
if [[ -x "$SK_ACT" ]]; then
  "$SK_ACT" inject --n 15 --since 12h 2>/dev/null || true
fi

exit 0
