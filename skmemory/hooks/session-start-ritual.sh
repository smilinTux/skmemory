#!/usr/bin/env bash
# Back-compat wrapper: logic lives in `skmemory hook session-start` (skmemory/hook_cli.py).
exec "$(command -v skmemory || echo "$HOME/.skenv/bin/skmemory")" hook session-start --harness claude "$@"
