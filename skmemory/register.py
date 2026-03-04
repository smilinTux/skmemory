"""SK* skill and MCP server auto-registration.

Detects the user's development environments (OpenClaw, Claude Code, Cursor,
VS Code, OpenCode CLI, mcporter) and registers SKILL.md symlinks and MCP
server entries so everything works out-of-the-box.

This module is the shared engine — individual packages call it, and
skcapstone orchestrates registration for the whole suite.

Usage (from any SK* package):
    from skmemory.register import register_package, detect_environments

    register_package(
        name="skmemory",
        skill_md_path=Path(__file__).parent / "SKILL.md",
        mcp_command="skmemory-mcp",
        mcp_args=[],
    )
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional


# ── Environment detection ────────────────────────────────────────────────────


def detect_environments() -> list[str]:
    """Return list of detected development environments.

    Checks for:
      - openclaw: OpenClaw CLI installed
      - claude-code: ~/.claude/ directory
      - cursor: ~/.cursor/ directory
      - vscode: ~/.vscode/ or ~/.config/Code/ directory
      - opencode: ~/.opencode/ or opencode binary
      - mcporter: mcporter.json in known locations

    Returns:
        List of environment identifiers (e.g. ["claude-code", "mcporter"]).
    """
    home = Path.home()
    envs: list[str] = []

    # OpenClaw
    openclaw_lib = home / ".npm-global" / "lib" / "node_modules" / "openclaw"
    if openclaw_lib.is_dir() or shutil.which("openclaw"):
        envs.append("openclaw")

    # Claude Code
    if (home / ".claude").is_dir():
        envs.append("claude-code")

    # Cursor
    if (home / ".cursor").is_dir():
        envs.append("cursor")

    # VS Code
    if (home / ".vscode").is_dir() or (home / ".config" / "Code").is_dir():
        envs.append("vscode")

    # OpenCode CLI
    if (home / ".opencode").is_dir() or shutil.which("opencode"):
        envs.append("opencode")

    # mcporter
    mcporter_paths = [
        home / "clawd" / "config" / "mcporter.json",
        home / ".config" / "mcporter" / "mcporter.json",
    ]
    for p in mcporter_paths:
        if p.is_file():
            envs.append("mcporter")
            break

    return envs


# ── Skill registration ───────────────────────────────────────────────────────


def register_skill(
    name: str,
    skill_md_path: Path,
    workspace: Optional[Path] = None,
) -> dict:
    """Register a skill by symlinking its SKILL.md into the workspace skills dir.

    Args:
        name: Package/skill name (e.g. "skmemory").
        skill_md_path: Absolute path to the source SKILL.md file.
        workspace: Workspace root (defaults to ~/clawd/).

    Returns:
        Dict with 'skill' key describing what was done.
    """
    if workspace is None:
        workspace = Path.home() / "clawd"

    skill_dir = workspace / "skills" / name
    target = skill_dir / "SKILL.md"

    result: dict = {"name": name, "action": "skip", "path": str(target)}

    if not skill_md_path.exists():
        result["action"] = "error"
        result["error"] = f"Source SKILL.md not found: {skill_md_path}"
        return result

    skill_dir.mkdir(parents=True, exist_ok=True)

    # If target already exists and is correct, skip
    if target.is_symlink():
        try:
            if target.resolve() == skill_md_path.resolve():
                result["action"] = "exists"
                return result
        except OSError:
            pass
        # Remove broken or wrong symlink
        target.unlink()

    if target.exists():
        result["action"] = "exists"
        return result

    # Create symlink — prefer relative path
    try:
        rel = os.path.relpath(skill_md_path, skill_dir)
        target.symlink_to(rel)
    except (ValueError, OSError):
        target.symlink_to(skill_md_path)

    result["action"] = "created"
    return result


# ── MCP registration ─────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on error."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write dict as pretty-printed JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _upsert_mcp_entry(
    path: Path,
    name: str,
    command: str,
    args: list,
    env: Optional[dict] = None,
) -> str:
    """Add or update an MCP server entry in a JSON config file.

    Expects the file to have a top-level "mcpServers" key.

    Args:
        path: Path to the JSON config file.
        name: Server name.
        command: Command to run.
        args: Command arguments.
        env: Optional environment variables.

    Returns:
        "created", "updated", or "exists".
    """
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})

    entry: dict = {"command": command, "args": args}
    if env:
        entry["env"] = env

    if name in servers:
        if servers[name] == entry:
            return "exists"
        servers[name] = entry
        _write_json(path, data)
        return "updated"

    servers[name] = entry
    _write_json(path, data)
    return "created"


def register_mcp(
    name: str,
    command: str,
    args: list,
    env: Optional[dict] = None,
    environments: Optional[list[str]] = None,
) -> dict:
    """Register an MCP server in detected (or specified) environments.

    Writes to:
      - claude-code: ~/.claude/mcp.json
      - cursor: ~/.cursor/mcp.json
      - vscode: (skipped — requires workspace .vscode/)
      - opencode: ~/.opencode/mcp.json
      - mcporter: ~/clawd/config/mcporter.json or ~/.config/mcporter/mcporter.json

    Args:
        name: Server name (e.g. "skmemory").
        command: Command to run (e.g. "skmemory-mcp").
        args: Command arguments.
        env: Optional environment variables.
        environments: Target environments. If None, auto-detect.

    Returns:
        Dict mapping environment -> action taken.
    """
    if environments is None:
        environments = detect_environments()

    home = Path.home()
    results: dict = {}

    env_to_path: dict[str, Path] = {
        "claude-code": home / ".claude" / "mcp.json",
        "cursor": home / ".cursor" / "mcp.json",
        "opencode": home / ".opencode" / "mcp.json",
    }

    # mcporter: find first existing file
    mcporter_candidates = [
        home / "clawd" / "config" / "mcporter.json",
        home / ".config" / "mcporter" / "mcporter.json",
    ]
    for p in mcporter_candidates:
        if p.is_file():
            env_to_path["mcporter"] = p
            break

    for env_name in environments:
        path = env_to_path.get(env_name)
        if path is None:
            continue
        try:
            action = _upsert_mcp_entry(path, name, command, args, env)
            results[env_name] = action
        except Exception as exc:
            results[env_name] = f"error: {exc}"

    return results


# ── High-level package registration ──────────────────────────────────────────


def register_package(
    name: str,
    skill_md_path: Path,
    mcp_command: Optional[str] = None,
    mcp_args: Optional[list] = None,
    mcp_env: Optional[dict] = None,
    workspace: Optional[Path] = None,
    environments: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Register a skill and its MCP server in all detected environments.

    Args:
        name: Package/skill name.
        skill_md_path: Path to the SKILL.md file.
        mcp_command: MCP server command (None to skip MCP registration).
        mcp_args: MCP server arguments.
        mcp_env: MCP server environment variables.
        workspace: Workspace root for skill symlinks.
        environments: Target environments (auto-detect if None).
        dry_run: If True, only report what would be done.

    Returns:
        Dict with 'skill' and 'mcp' results.
    """
    if environments is None:
        environments = detect_environments()

    result: dict = {"name": name, "environments": environments}

    if dry_run:
        result["skill"] = {"action": "dry-run", "path": str(
            (workspace or Path.home() / "clawd") / "skills" / name / "SKILL.md"
        )}
        if mcp_command:
            result["mcp"] = {env: "dry-run" for env in environments}
        return result

    # Register skill
    result["skill"] = register_skill(name, skill_md_path, workspace)

    # Register MCP server
    if mcp_command is not None:
        result["mcp"] = register_mcp(
            name,
            mcp_command,
            mcp_args or [],
            env=mcp_env,
            environments=environments,
        )

    return result
