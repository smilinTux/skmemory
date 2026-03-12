"""Post-install auto-registration for skmemory.

Runs `skmemory register` automatically after pip install to ensure:
  - MCP server is registered in Claude Code, Cursor, etc.
  - Auto-save hooks are installed in Claude Code settings
  - Skill symlink is created

Called via:
  - `skmemory-post-install` console script (entry point)
  - `pip install skmemory && skmemory-post-install`
  - Automatically on first `skmemory` CLI invocation (if not yet registered)
"""

from __future__ import annotations

import sys
from pathlib import Path


def _is_registered() -> bool:
    """Check if hooks are already installed (quick check)."""
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        return False
    try:
        import json
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        return "PreCompact" in hooks and "SessionEnd" in hooks
    except Exception:
        return False


def run_post_install() -> None:
    """Register skmemory MCP server, hooks, and skill symlinks."""
    from .register import detect_environments, register_package, register_hooks

    print("skmemory: running post-install registration...")

    detected = detect_environments()
    if not detected:
        print("  No supported environments detected. Skipping.")
        return

    print(f"  Detected: {', '.join(detected)}")

    skill_md = Path(__file__).parent.parent / "SKILL.md"
    if not skill_md.exists():
        skill_md = Path(__file__).parent / "SKILL.md"

    result = register_package(
        name="skmemory",
        skill_md_path=skill_md,
        mcp_command="skmemory-mcp",
        mcp_args=[],
        install_hooks=True,
        environments=detected,
    )

    skill_action = result.get("skill", {}).get("action", "—")
    print(f"  Skill: {skill_action}")

    mcp = result.get("mcp", {})
    for env_name, action in mcp.items():
        print(f"  MCP ({env_name}): {action}")

    hooks = result.get("hooks", {})
    if hooks:
        print(f"  Hooks: {hooks.get('action', '—')}")

    print("skmemory: post-install complete.")


def main() -> None:
    """Entry point for skmemory-post-install console script."""
    try:
        run_post_install()
    except Exception as exc:
        # Never fail the install — registration is best-effort
        print(f"skmemory: post-install warning: {exc}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
