"""SK* skill, MCP server, and OpenClaw plugin auto-registration.

Detects the user's development environments (OpenClaw, Claude Code, Cursor,
VS Code, OpenCode CLI, Codex, mcporter) and registers SKILL.md symlinks, MCP
server entries, and OpenClaw plugin manifests so everything works out-of-the-box.

This module is the shared engine — individual packages call it, and
skcapstone orchestrates registration for the whole suite.

Usage (from any SK* package):
    from skmemory.register import register_package, detect_environments

    register_package(
        name="skmemory",
        skill_md_path=Path(__file__).parent / "SKILL.md",
        mcp_command="skmemory-mcp",
        mcp_args=[],
        openclaw_plugin_path=Path(__file__).parent.parent / "openclaw-plugin" / "src" / "index.ts",
    )
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("skmemory.register")

# ── Environment detection ────────────────────────────────────────────────────


def detect_environments() -> list[str]:
    """Return list of detected development environments.

    Checks for:
      - openclaw: OpenClaw CLI installed
      - claude-code: ~/.claude/ directory
      - cursor: ~/.cursor/ directory
      - vscode: ~/.vscode/ or ~/.config/Code/ directory
      - opencode: ~/.opencode/ or opencode binary
      - codex: ~/.codex/ directory
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

    # Codex
    if (home / ".codex").is_dir():
        envs.append("codex")

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
    workspace: Path | None = None,
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


def register_codex_skill(
    name: str,
    skill_md_path: Path,
) -> dict:
    """Register a skill by symlinking its SKILL.md into ~/.codex/skills.

    Args:
        name: Package/skill name (e.g. "skmemory").
        skill_md_path: Absolute path to the source SKILL.md file.

    Returns:
        Dict describing what was done.
    """
    codex_root = Path.home() / ".codex" / "skills"
    skill_dir = codex_root / name
    target = skill_dir / "SKILL.md"

    result: dict = {"name": name, "action": "skip", "path": str(target)}

    if not skill_md_path.exists():
        result["action"] = "error"
        result["error"] = f"Source SKILL.md not found: {skill_md_path}"
        return result

    skill_dir.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        try:
            if target.resolve() == skill_md_path.resolve():
                result["action"] = "exists"
                return result
        except OSError:
            pass
        target.unlink()

    if target.exists():
        result["action"] = "exists"
        return result

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
    env: dict | None = None,
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
    env: dict | None = None,
    environments: list[str] | None = None,
) -> dict:
    """Register an MCP server in detected (or specified) environments.

    Writes to:
      - claude-code: ~/.claude/mcp.json
      - cursor: ~/.cursor/mcp.json
      - vscode: (skipped — requires workspace .vscode/)
      - opencode: ~/.opencode/mcp.json
      - codex: not yet supported for MCP config writing here
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

    supported_envs = {"claude-code", "cursor", "opencode", "mcporter"}
    environments = [env for env in environments if env in supported_envs]

    home = Path.home()
    results: dict = {}

    env_to_path: dict[str, Path] = {
        "claude-code": home / ".claude" / "mcp.json",
        "cursor": home / ".cursor" / "mcp.json",
        "opencode": home / ".opencode" / "mcp.json",
    }

    # OpenClaw: uses mcporter for MCP — no native mcpServers key in
    # openclaw.json. MCP registration for OpenClaw happens via mcporter.

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
            logger.warning("register.py: %s", exc)
            results[env_name] = f"error: {exc}"

    return results


# ── OpenClaw plugin registration ─────────────────────────────────────────────


def _get_openclaw_json_path() -> Path:
    """Return path to ~/.openclaw/openclaw.json."""
    return Path.home() / ".openclaw" / "openclaw.json"


def _is_openclaw_plugin_registered(plugin_path: Path) -> bool:
    """Check if plugin_path is already in openclaw.json plugins.load.paths."""
    oc_json = _get_openclaw_json_path()
    if not oc_json.is_file():
        return False
    data = _read_json(oc_json)
    paths = data.get("plugins", {}).get("load", {}).get("paths", [])
    resolved = str(plugin_path.resolve())
    return any(str(Path(p).resolve()) == resolved for p in paths)


def _upsert_openclaw_plugin_path(plugin_id: str, plugin_path: Path) -> None:
    """Add plugin to plugins.load.paths and plugins.installs in openclaw.json."""
    oc_json = _get_openclaw_json_path()
    data = _read_json(oc_json)

    plugins = data.setdefault("plugins", {})
    load = plugins.setdefault("load", {})
    paths = load.setdefault("paths", [])

    resolved = str(plugin_path.resolve())
    if resolved not in paths:
        paths.append(resolved)

    installs = plugins.setdefault("installs", {})
    # Point to the plugin directory (parent of src/index.ts)
    plugin_dir = str(plugin_path.resolve().parent.parent)
    installs[plugin_id] = {"path": plugin_dir, "linked": True}

    _write_json(oc_json, data)


def _ensure_openclaw_plugin_enabled(plugin_id: str) -> None:
    """Set plugins.entries.<plugin_id>.enabled = true (idempotent)."""
    oc_json = _get_openclaw_json_path()
    data = _read_json(oc_json)

    entries = data.setdefault("plugins", {}).setdefault("entries", {})
    entry = entries.setdefault(plugin_id, {})

    if entry.get("enabled") is True:
        return

    entry["enabled"] = True
    _write_json(oc_json, data)


def _detect_plugin_id(plugin_path: Path, fallback: str) -> str:
    """Read plugin id from openclaw.plugin.json manifest."""
    manifest = plugin_path.parent / "openclaw.plugin.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
            return data.get("id", fallback)
        except (json.JSONDecodeError, KeyError):
            pass
    return fallback


def register_openclaw_plugin(
    plugin_id: str,
    plugin_path: Path,
    dry_run: bool = False,
) -> str:
    """Register an OpenClaw plugin. Returns 'exists', 'created', or 'error:...'.

    1. Check _is_openclaw_plugin_registered() -> skip if already registered
    2. Try `openclaw plugins install --link <path>` via subprocess
    3. Fallback: write openclaw.json directly if CLI fails
    4. _ensure_openclaw_plugin_enabled()

    Args:
        plugin_id: Plugin identifier (e.g. "skmemory").
        plugin_path: Path to the plugin entry point (e.g. src/index.ts).
        dry_run: If True, only report what would be done.

    Returns:
        Action taken: "exists", "created", "dry-run", or "error:...".
    """
    if dry_run:
        return "dry-run"

    if not plugin_path.exists():
        return f"error: plugin not found: {plugin_path}"

    # Detect actual plugin ID from manifest
    actual_id = _detect_plugin_id(plugin_path, plugin_id)

    if _is_openclaw_plugin_registered(plugin_path):
        _ensure_openclaw_plugin_enabled(actual_id)
        return "exists"

    # Try CLI first
    plugin_dir = str(plugin_path.resolve().parent.parent)
    try:
        subprocess.run(
            ["openclaw", "plugins", "install", "--link", plugin_dir],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if _is_openclaw_plugin_registered(plugin_path):
            _ensure_openclaw_plugin_enabled(actual_id)
            return "created"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: write directly
    try:
        _upsert_openclaw_plugin_path(actual_id, plugin_path)
        _ensure_openclaw_plugin_enabled(actual_id)
        return "created"
    except Exception as exc:
        logger.warning("register.py: %s", exc)
        return f"error: {exc}"


# ── Claude Code hooks registration ───────────────────────────────────────────


def register_hooks(
    environments: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Register skmemory auto-save hooks in Claude Code settings.

    Adds hooks for:
      - PreCompact: save context to skmemory before compaction
      - SessionEnd: journal session end
      - SessionStart (compact): reinject memory context after compaction

    Args:
        environments: Target environments (auto-detect if None).
        dry_run: If True, only report what would be done.

    Returns:
        Dict with action taken: "created", "updated", "exists", "skip", or "error:...".
    """
    if environments is None:
        environments = detect_environments()

    if "claude-code" not in environments:
        return {"action": "skip", "reason": "claude-code not detected"}

    if dry_run:
        return {"action": "dry-run"}

    home = Path.home()
    settings_path = home / ".claude" / "settings.json"

    # Resolve hook script paths from the installed package
    hooks_dir = Path(__file__).parent / "hooks"
    pre_compact = str(hooks_dir / "pre-compact-save.sh")
    session_end = str(hooks_dir / "session-end-save.sh")
    post_compact = str(hooks_dir / "post-compact-reinject.sh")

    # Verify hook scripts exist
    for script in [pre_compact, session_end, post_compact]:
        if not Path(script).exists():
            return {"action": f"error: hook script not found: {script}"}

    desired_hooks = {
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": pre_compact}],
            }
        ],
        "SessionEnd": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": session_end}],
            }
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [{"type": "command", "command": post_compact}],
            }
        ],
    }

    try:
        data = _read_json(settings_path)
        existing_hooks = data.get("hooks", {})

        # Check if already configured
        needs_update = False
        for event, hook_list in desired_hooks.items():
            if event not in existing_hooks:
                needs_update = True
                break
            # Check if our hook command is already present
            existing_cmds = []
            for entry in existing_hooks[event]:
                for h in entry.get("hooks", []):
                    existing_cmds.append(h.get("command", ""))
            desired_cmd = hook_list[0]["hooks"][0]["command"]
            if desired_cmd not in existing_cmds:
                needs_update = True
                break

        if not needs_update:
            return {"action": "exists"}

        # Merge: add our hooks without removing existing ones
        for event, hook_list in desired_hooks.items():
            if event not in existing_hooks:
                existing_hooks[event] = hook_list
            else:
                # Check if our command is already there
                desired_cmd = hook_list[0]["hooks"][0]["command"]
                already_present = False
                for entry in existing_hooks[event]:
                    for h in entry.get("hooks", []):
                        if h.get("command") == desired_cmd:
                            already_present = True
                            break
                if not already_present:
                    existing_hooks[event].extend(hook_list)

        data["hooks"] = existing_hooks
        _write_json(settings_path, data)

        action = "updated" if settings_path.exists() else "created"
        return {"action": action}

    except Exception as exc:
        logger.warning("register.py: %s", exc)
        return {"action": f"error: {exc}"}


# ── High-level package registration ──────────────────────────────────────────


def register_package(
    name: str,
    skill_md_path: Path,
    mcp_command: str | None = None,
    mcp_args: list | None = None,
    mcp_env: dict | None = None,
    openclaw_plugin_path: Path | None = None,
    install_hooks: bool = False,
    workspace: Path | None = None,
    environments: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Register a skill, MCP server, hooks, and OpenClaw plugin in all detected environments.

    Args:
        name: Package/skill name.
        skill_md_path: Path to the SKILL.md file.
        mcp_command: MCP server command (None to skip MCP registration).
        mcp_args: MCP server arguments.
        mcp_env: MCP server environment variables.
        openclaw_plugin_path: Path to OpenClaw plugin entry (e.g. src/index.ts).
        install_hooks: If True, register Claude Code hooks for auto-save.
        workspace: Workspace root for skill symlinks.
        environments: Target environments (auto-detect if None).
        dry_run: If True, only report what would be done.

    Returns:
        Dict with 'skill', 'mcp', 'hooks', and 'openclaw_plugin' results.
    """
    if environments is None:
        environments = detect_environments()

    result: dict = {"name": name, "environments": environments}

    if dry_run:
        mcp_envs = [
            env for env in environments if env in {"claude-code", "cursor", "opencode", "mcporter"}
        ]
        result["skill"] = {
            "action": "dry-run",
            "path": str((workspace or Path.home() / "clawd") / "skills" / name / "SKILL.md"),
        }
        if "codex" in environments:
            result["codex_skill"] = {
                "action": "dry-run",
                "path": str(Path.home() / ".codex" / "skills" / name / "SKILL.md"),
            }
        if mcp_command:
            result["mcp"] = {env: "dry-run" for env in mcp_envs}
        if install_hooks:
            result["hooks"] = {"action": "dry-run"}
        if openclaw_plugin_path and "openclaw" in environments:
            result["openclaw_plugin"] = "dry-run"
        return result

    # Register skill
    result["skill"] = register_skill(name, skill_md_path, workspace)

    if "codex" in environments:
        result["codex_skill"] = register_codex_skill(name, skill_md_path)

    # Register MCP server
    if mcp_command is not None:
        result["mcp"] = register_mcp(
            name,
            mcp_command,
            mcp_args or [],
            env=mcp_env,
            environments=environments,
        )

    # Register Claude Code hooks
    if install_hooks:
        result["hooks"] = register_hooks(
            environments=environments,
            dry_run=dry_run,
        )

    # Register OpenClaw plugin
    if openclaw_plugin_path is not None and "openclaw" in environments:
        result["openclaw_plugin"] = register_openclaw_plugin(
            name,
            openclaw_plugin_path,
            dry_run=dry_run,
        )

    return result
