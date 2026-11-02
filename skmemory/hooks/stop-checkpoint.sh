#!/usr/bin/env bash
# Back-compat wrapper: logic lives in `skmemory hook stop` (skmemory/hook_cli.py).
exec "$(command -v skmemory || echo "$HOME/.skenv/bin/skmemory")" hook stop --harness claude "$@"
