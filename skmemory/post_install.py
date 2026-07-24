"""Post-install auto-registration for skmemory.

Runs `skmemory register` automatically after pip install to ensure:
  - MCP server is registered in Claude Code, Cursor, etc.
  - Auto-save hooks are installed in Claude Code settings
  - Skill symlink is created
  - (Optional) Fortress verify systemd timer is enabled

Called via:
  - `skmemory-post-install` console script (entry point)
  - `pip install skmemory && skmemory-post-install`
  - Automatically on first `skmemory` CLI invocation (if not yet registered)

Fortress install:
  - TTY: prompts to install daily integrity-verify timer
  - Non-TTY: skipped (print hint); enable via
    `SKMEMORY_INSTALL_FORTRESS=1` env var or pass `--fortress` to the script
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
    except Exception as e:
        logger.warning("post_install.py: %s", e)
        return False


def _find_install_script() -> Path | None:
    """Locate scripts/install-systemd.sh from the package.

    Returns None when installed from a wheel without the scripts/ tree,
    which is the expected case for non-editable pip installs.
    """
    candidates = [
        Path(__file__).parent.parent / "scripts" / "install-systemd.sh",
        Path.home() / "clawd" / "skcapstone-repos" / "skmemory" / "scripts" / "install-systemd.sh",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _agent_name() -> str:
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    )


def _timer_already_enabled(agent: str) -> bool:
    """True if the fortress timer is enabled for this agent."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", f"skmemory-fortress-verify@{agent}.timer"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and "enabled" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def maybe_install_fortress_timer() -> None:
    """Offer to install the daily fortress-verify systemd timer.

    Skipped silently if:
      - Not on Linux (no systemd --user)
      - install-systemd.sh not findable (wheel install without source)
      - Timer already enabled for this agent
      - SKMEMORY_SKIP_FORTRESS=1 set
    """
    if os.environ.get("SKMEMORY_SKIP_FORTRESS") == "1":
        return
    if sys.platform != "linux":
        return

    script = _find_install_script()
    if script is None:
        return

    agent = _agent_name()
    if _timer_already_enabled(agent):
        print(f"  Fortress: timer already enabled for {agent}")
        return

    force = os.environ.get("SKMEMORY_INSTALL_FORTRESS") in {"1", "yes", "true"}
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    if not force and not is_tty:
        print("  Fortress: timer not enabled. To enable later:")
        print(f"    {script} --agents {agent} --fortress")
        print("    (or set SKMEMORY_INSTALL_FORTRESS=1 before re-running this script)")
        return

    if not force:
        try:
            ans = (
                input(
                    f"  Enable daily fortress integrity-verify timer for agent '{agent}'? [Y/n]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans and ans not in {"y", "yes"}:
            print("  Fortress: skipped.")
            return

    try:
        subprocess.run(
            ["bash", str(script), "--agents", agent, "--fortress", "--no-sync"],
            check=True,
        )
        print(f"  Fortress: timer enabled for {agent}.")
    except subprocess.CalledProcessError as exc:
        print(f"  Fortress: install script failed (rc={exc.returncode}); enable manually:")
        print(f"    {script} --agents {agent} --fortress")


def run_post_install() -> None:
    """Register skmemory MCP server, hooks, and skill symlinks."""
    from .register import detect_environments, register_package

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

    maybe_install_fortress_timer()

    # Optional skcapstone backbone integration (default-on by presence).
    # Registers the promotion sweep with the fleet scheduler and advertises
    # skmemory to service discovery. No-op when skcapstone is absent.
    try:
        from . import integration

        if integration.ensure_schedule():
            print("  skcapstone: registered promotion sweep with fleet scheduler")
            integration.register_self()
        else:
            print("  skcapstone: not present — using native scheduler/timer")
    except Exception as exc:  # never fail install on integration
        logger.debug("skcapstone integration skipped: %s", exc)

    print("skmemory: post-install complete.")


def main() -> None:
    """Entry point for skmemory-post-install console script."""
    try:
        run_post_install()
    except Exception as exc:
        logger.warning("post_install.py: %s", exc)
        # Never fail the install — registration is best-effort
        print(f"skmemory: post-install warning: {exc}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
