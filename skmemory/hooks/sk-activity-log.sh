#!/usr/bin/env bash
# Back-compat wrapper: activity logging is now part of `skmemory hook session-end`.
"$(command -v skmemory || echo "$HOME/.skenv/bin/skmemory")" hook session-end --harness claude "$@" >/dev/null 2>&1; printf '{}\n'
