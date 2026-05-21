#!/usr/bin/env python3
"""Claude Code hooks for automatic memory capture.

Two hooks:
1. Stop hook — fires when Claude Code session ends.
   Reads the session transcript and runs the General Extractor
   to auto-capture decisions, milestones, problems, etc.

2. PreCompact hook — fires before context compression.
   Emergency save of current session context before it gets compressed.

Install by adding to ~/.claude/settings.json:
{
  "hooks": {
    "Stop": [{
      "type": "command",
      "command": "python3 -m skmemory.hooks.claude_code_hooks stop"
    }],
    "PreCompact": [{
      "type": "command",
      "command": "python3 -m skmemory.hooks.claude_code_hooks precompact"
    }]
  }
}
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("skmemory.hooks")

_HOOK_LOCK_FILE = "/tmp/skmemory-hook.lock"


def _acquire_hook_lock(timeout: int = 30) -> "IO | None":
    """Serialize concurrent hook calls to prevent memory pile-up.

    Multiple sessions ending simultaneously each spawn a fresh Python process
    that would load the full embedding model (~1.8GB). Serializing with a
    file lock keeps peak RSS bounded to one process at a time.

    Returns the open lock file handle (caller must keep it alive), or None
    if the lock could not be acquired within timeout seconds.
    """
    import time
    fh = open(_HOOK_LOCK_FILE, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.5)


def _get_store():
    """Lazy import to avoid circular deps and heavy startup cost.

    Constructs MemoryStore with no vector backend (flat JSON + SQLite only).
    This is intentional — hook breadcrumbs don't need semantic search.
    skwhisper digest --backlog handles vector indexing asynchronously.
    Avoids the ~1.8GB SentenceTransformer load the CLI would trigger.
    """
    from ..store import MemoryStore
    return MemoryStore()


def _get_session_id() -> str:
    """Get the current Claude Code session ID."""
    return os.environ.get("CLAUDE_SESSION_ID", "unknown")


def _get_agent() -> str:
    """Get the current agent name."""
    return os.environ.get("SKCAPSTONE_AGENT", os.environ.get("SKAGENT", "lumina"))


def _read_stdin_if_available() -> str:
    """Read conversation from stdin if piped."""
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except Exception as e:
        logger.warning("claude_code_hooks.py: %s", e)
        return ""


def handle_stop() -> None:
    """Called when a Claude Code session ends.

    Reads the conversation from stdin (Claude Code pipes it),
    runs the general extractor, and saves extracted memories.
    """
    from ..extractor import extract_memories
    from ..models import MemoryLayer

    conversation = _read_stdin_if_available()
    if not conversation or len(conversation) < 100:
        return

    extracted = extract_memories(conversation)
    if not extracted:
        return

    _lock = _acquire_hook_lock()
    if _lock is None:
        logger.warning("handle_stop: could not acquire hook lock within 30s, skipping")
        return

    try:
        store = _get_store()
        session_id = _get_session_id()

        saved = 0
        for mem in extracted:
            try:
                store.snapshot(
                    title=f"[auto-{mem.type}] {mem.content[:60]}",
                    content=mem.content,
                    layer=MemoryLayer.SHORT,
                    tags=["auto-extract", mem.type, f"session:{session_id}"],
                    source="claude-code-hook",
                    source_ref=f"session:{session_id}",
                    metadata={
                        "extraction_confidence": mem.confidence,
                        "extraction_type": mem.type,
                        "hook": "stop",
                    },
                )
                saved += 1
            except Exception as exc:
                logger.warning("Failed to save extracted memory: %s", exc)

        if saved:
            print(f"skmemory: auto-saved {saved} memories from session {session_id[:8]}")
    finally:
        try:
            fcntl.flock(_lock, fcntl.LOCK_UN)
            _lock.close()
        except Exception:
            pass


def handle_precompact() -> None:
    """Called before Claude Code compresses context.

    Emergency save — capture a session snapshot before context is lost.
    """
    from ..models import MemoryLayer

    conversation = _read_stdin_if_available()
    if not conversation or len(conversation) < 200:
        return

    _lock = _acquire_hook_lock()
    if _lock is None:
        logger.warning("handle_precompact: could not acquire hook lock within 30s, skipping")
        return

    try:
        store = _get_store()
        session_id = _get_session_id()

        # Save the last 4K chars (most recent/relevant context)
        content = conversation[-4000:]
        try:
            store.snapshot(
                title=f"Pre-compact session snapshot ({session_id[:8]})",
                content=content,
                layer=MemoryLayer.SHORT,
                tags=["pre-compact", "auto-save", f"session:{session_id}"],
                source="claude-code-hook",
                source_ref=f"session:{session_id}",
                metadata={"hook": "precompact", "original_length": len(conversation)},
            )
            print(f"skmemory: pre-compact snapshot saved for session {session_id[:8]}")
        except Exception as exc:
            logger.warning("Failed to save pre-compact snapshot: %s", exc)
    finally:
        try:
            fcntl.flock(_lock, fcntl.LOCK_UN)
            _lock.close()
        except Exception:
            pass


def install_hooks(settings_path: Path | None = None) -> bool:
    """Add skmemory hooks to Claude Code settings.json.

    Args:
        settings_path: Path to settings.json. Defaults to ~/.claude/settings.json.

    Returns:
        True if hooks were installed, False if already present or failed.
    """
    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as e:
            logger.warning("claude_code_hooks.py: %s", e)
            settings = {}

    hooks = settings.setdefault("hooks", {})

    stop_cmd = "python3 -m skmemory.hooks.claude_code_hooks stop"
    precompact_cmd = "python3 -m skmemory.hooks.claude_code_hooks precompact"

    modified = False

    # Add Stop hook
    stop_hooks = hooks.setdefault("Stop", [])
    if not any(h.get("command") == stop_cmd for h in stop_hooks):
        stop_hooks.append({"type": "command", "command": stop_cmd})
        modified = True

    # Add PreCompact hook
    precompact_hooks = hooks.setdefault("PreCompact", [])
    if not any(h.get("command") == precompact_cmd for h in precompact_hooks):
        precompact_hooks.append({"type": "command", "command": precompact_cmd})
        modified = True

    if modified:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2))
        return True

    return False


def main() -> None:
    """CLI entry point for Claude Code hooks."""
    if len(sys.argv) < 2:
        print("Usage: python -m skmemory.hooks.claude_code_hooks [stop|precompact|install]")
        sys.exit(1)

    action = sys.argv[1]
    if action == "stop":
        handle_stop()
    elif action == "precompact":
        handle_precompact()
    elif action == "install":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
        if install_hooks(path):
            print("skmemory: Claude Code hooks installed")
        else:
            print("skmemory: hooks already installed or failed")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
