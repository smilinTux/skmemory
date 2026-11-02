#!/usr/bin/env bash
# Back-compat wrapper: logic lives in `skmemory hook pre-compact` (skmemory/hook_cli.py).
exec "$(command -v skmemory || echo "$HOME/.skenv/bin/skmemory")" hook pre-compact --harness claude "$@"
