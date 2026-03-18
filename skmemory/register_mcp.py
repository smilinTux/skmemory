#!/usr/bin/env python3
"""
MCP Server Registration for SKMemory/SKCapstone

Auto-registers MCP servers with OpenCode, Claude Code, and OpenClaw.
Usage:
    python -m skmemory.register_mcp
    python -m skmemory.register_mcp --env opencode
    python -m skmemory.register_mcp --env claude
    python -m skmemory.register_mcp --env openclaw
    python -m skmemory.register_mcp --agent lumina
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def get_agent_name() -> str:
    """Get agent name from environment or default."""
    return os.environ.get("SKMEMORY_AGENT") or os.environ.get("SKCAPSTONE_AGENT") or "lumina"


def register_opencode(agent: str, dry_run: bool = False) -> bool:
    """Register SKMemory with OpenCode."""
    config_dir = Path.home() / ".opencode"
    config_file = config_dir / "mcp.json"

    if dry_run:
        print(f"[DRY-RUN] Would create: {config_file}")
        return True

    config_dir.mkdir(parents=True, exist_ok=True)

    # Build MCP config
    config = {
        "mcpServers": {
            "skmemory": {
                "command": "python",
                "args": ["-m", "skmemory.mcp_server"],
                "env": {
                    "SKMEMORY_AGENT": agent,
                    "SKMEMORY_HOME": str(Path.home() / ".skcapstone" / "agents" / agent),
                },
            },
            "skcapstone": {
                "command": "python",
                "args": ["-m", "skcapstone.mcp_server"],
                "env": {"SKCAPSTONE_AGENT": agent},
            },
        },
        "skills": [
            {
                "name": "skmemory",
                "path": str(Path.home() / "clawd" / "skcapstone-repos" / "skmemory" / "SKILL.md"),
            },
            {"name": "skcapstone", "path": str(Path.home() / "clawd" / "skcapstone" / "SKILL.md")},
        ],
    }

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"✓ Registered with OpenCode: {config_file}")
    return True


def register_claude(agent: str, dry_run: bool = False) -> bool:
    """Register SKMemory with Claude Code."""
    config_file = Path.home() / ".config" / "claude" / "claude_desktop_config.json"

    if dry_run:
        print(f"[DRY-RUN] Would create: {config_file}")
        return True

    config_file.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "mcpServers": {
            "skmemory": {
                "command": "python",
                "args": ["-m", "skmemory.mcp_server"],
                "env": {
                    "SKMEMORY_AGENT": agent,
                    "SKMEMORY_HOME": str(Path.home() / ".skcapstone" / "agents" / agent),
                },
            },
            "skcapstone": {
                "command": "python",
                "args": ["-m", "skcapstone.mcp_server"],
                "env": {"SKCAPSTONE_AGENT": agent},
            },
        }
    }

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"✓ Registered with Claude Code: {config_file}")
    return True


def register_openclaw(agent: str, dry_run: bool = False) -> bool:
    """Register SKMemory with OpenClaw (via plugin)."""
    config_file = Path.home() / ".openclaw" / "openclaw.json"

    if dry_run:
        print(f"[DRY-RUN] Would update: {config_file}")
        return True

    # Read existing config
    if config_file.exists():
        with open(config_file) as f:
            config = json.load(f)
    else:
        config = {}

    # Add plugins
    config.setdefault("plugins", {})
    config["plugins"]["skmemory"] = {
        "enabled": True,
        "path": str(Path.home() / "clawd" / "skcapstone-repos" / "skmemory" / "openclaw-plugin"),
    }
    config["plugins"]["skcapstone"] = {
        "enabled": True,
        "path": str(Path.home() / "clawd" / "skcapstone" / "openclaw-plugin"),
    }

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"✓ Registered with OpenClaw: {config_file}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Register SKMemory MCP servers with AI clients")
    parser.add_argument(
        "--env",
        choices=["opencode", "claude", "openclaw", "all"],
        default="all",
        help="Target environment (default: all)",
    )
    parser.add_argument(
        "--agent", default=None, help="Agent name (default: SKMEMORY_AGENT env var or 'lumina')"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )

    args = parser.parse_args()

    agent = args.agent or get_agent_name()

    print(f"Registering MCP servers for agent: {agent}")
    if args.dry_run:
        print("[DRY-RUN MODE - No changes will be made]")
    print()

    results = []

    if args.env in ("opencode", "all"):
        results.append(("OpenCode", register_opencode(agent, args.dry_run)))

    if args.env in ("claude", "all"):
        results.append(("Claude Code", register_claude(agent, args.dry_run)))

    if args.env in ("openclaw", "all"):
        results.append(("OpenClaw", register_openclaw(agent, args.dry_run)))

    print()
    print("=" * 50)
    print("Registration Summary")
    print("=" * 50)

    for name, success in results:
        status = "✓" if success else "✗"
        print(f"{status} {name}")

    if all(success for _, success in results):
        print("\n✓ All MCP servers registered successfully!")
        print("\nNext steps:")
        print("  1. Restart your AI client")
        print("  2. Verify with: skmemory show-context")
        return 0
    else:
        print("\n✗ Some registrations failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
