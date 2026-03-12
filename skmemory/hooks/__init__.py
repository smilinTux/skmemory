"""Claude Code hooks for skmemory auto-save.

Ships three hook scripts:
  pre-compact-save.sh    — Snapshots context before compaction
  session-end-save.sh    — Journals session end
  post-compact-reinject.sh — Re-injects memory context after compaction

Installed by `skmemory register` into ~/.claude/settings.json.
"""

from pathlib import Path

HOOKS_DIR = Path(__file__).parent


def get_hook_path(name: str) -> Path:
    """Return absolute path to a hook script."""
    return HOOKS_DIR / name
