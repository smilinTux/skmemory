"""`skmemory hooks install|uninstall|status`: wire `skmemory hook` into each harness.

Mechanism per harness (researched against the installed versions):

    claude    ~/.claude/settings.json "hooks": SessionStart (startup|clear ->
              session-start, compact|resume -> post-compact), PreCompact,
              SessionEnd, Stop. Command hooks, JSON additionalContext output.
    codex     ~/.codex/hooks.json, same event/matcher/handler shape as Claude
              (Codex >= 0.16x ships lifecycle hooks on by default). Codex only
              runs hooks the user has trusted; the installer records trust for
              the hooks it writes when `codex app-server` is available, and
              otherwise tells the user to approve them once with /hooks.
    opencode  plugin file in ~/.config/opencode/plugins/ using
              experimental.chat.system.transform (cached per session),
              experimental.session.compacting, session.idle/deleted, dispose.
    pi        extension file in ~/.pi/agent/extensions/ using session_start +
              before_agent_start (systemPrompt), session_before_compact,
              session_shutdown.

Every command written references the absolute path of the installed
`skmemory` console script, never a repo checkout. Installing replaces legacy
SK hook entries (skmemory/hooks/*.sh, skwhisper-inject.sh, skwhisper-save.sh,
bare `skcapstone`) and our own previous entries, and leaves every other hook
untouched. A timestamped backup is written before any config file changes.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

ALL_HARNESSES = ("claude", "codex", "opencode", "pi")
MANAGED_MARK = "skmemory-managed"
_ENV_TO_HARNESS = {"claude-code": "claude", "codex": "codex", "opencode": "opencode", "pi": "pi"}

# Commands we own or supersede. Anything else is a user hook and is preserved.
_LEGACY_PATTERNS = (
    re.compile(r"skmemory/hooks/[\w.-]+\.sh\b"),
    re.compile(r"skwhisper-(inject|save)\.sh\b"),
    re.compile(r"^\s*\S*skcapstone\s*$"),
    re.compile(r"skmemory\.hooks\.claude_code_hooks"),
)
_OURS = re.compile(r"(^|[/\s'\"])skmemory['\"]?\s+hook\s|-m\s+skmemory\.cli\s+hook\s")


def is_managed_command(cmd: str) -> bool:
    """True for commands this installer owns or replaces."""
    cmd = str(cmd or "")
    return bool(_OURS.search(cmd)) or any(p.search(cmd) for p in _LEGACY_PATTERNS)


def is_legacy_command(cmd: str) -> bool:
    return any(p.search(str(cmd or "")) for p in _LEGACY_PATTERNS)


# ── paths ────────────────────────────────────────────────────────────────────


def skmemory_bin() -> str:
    """Absolute path of the installed `skmemory` console script."""
    here = Path(sys.executable).parent
    for name in ("skmemory", "skmemory.exe"):
        cand = here / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.absolute())
    found = shutil.which("skmemory")
    if found:
        return str(Path(found).absolute())
    return f"{sys.executable} -m skmemory.cli"


def _cmd(event: str, harness: str, exe: str | None = None) -> str:
    exe = exe or skmemory_bin()
    parts = exe.split(" -m ", 1)
    base = shlex.quote(parts[0]) + (f" -m {parts[1]}" if len(parts) == 2 else "")
    return f"{base} hook {event} --harness {harness}"


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def codex_hooks_path() -> Path:
    return codex_home() / "hooks.json"


def opencode_plugin_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "opencode" / "plugins" / "skmemory-context.js"


def pi_extension_path() -> Path:
    base = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(base).expanduser() / "extensions" / "skmemory-context.ts"


# ── generic file plumbing ────────────────────────────────────────────────────


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bak = path.with_name(f"{path.name}.bak-skmemory-{stamp}")
    n = 1
    while bak.exists():
        bak = path.with_name(f"{path.name}.bak-skmemory-{stamp}-{n}")
        n += 1
    shutil.copy2(path, bak)
    return bak


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (after install)",
        )
    )


def _apply(path: Path, old: str, new: str, dry_run: bool) -> dict:
    if old == new:
        return {"path": str(path), "action": "unchanged"}
    res = {"path": str(path), "action": "would-update" if path.exists() else "would-create"}
    res["diff"] = _diff(path, old, new)
    if dry_run:
        return res
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _backup(path)
    tmp = path.with_name(path.name + ".skmemory-tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    res["action"] = "updated" if bak else "created"
    if bak:
        res["backup"] = str(bak)
    return res


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ── Claude Code / Codex JSON hook maps ───────────────────────────────────────


def _desired_groups(harness: str, exe: str) -> dict[str, list[dict]]:
    def h(event: str, timeout: int) -> dict:
        return {"type": "command", "command": _cmd(event, harness, exe), "timeout": timeout}

    return {
        "SessionStart": [
            {"matcher": "startup|clear", "hooks": [h("session-start", 30)]},
            {"matcher": "compact|resume", "hooks": [h("post-compact", 20)]},
        ],
        "PreCompact": [{"matcher": "", "hooks": [h("pre-compact", 30)]}],
        # Codex clamps SessionEnd timeouts to 1-3s; the hook detaches its writes.
        "SessionEnd": [
            {"matcher": "", "hooks": [h("session-end", 3 if harness == "codex" else 30)]}
        ],
        "Stop": [{"matcher": "", "hooks": [h("stop", 10)]}],
    }


def _strip_managed(hooks: dict) -> tuple[dict, list[str]]:
    """Remove our/legacy handlers; drop groups left empty. Returns (hooks, removed)."""
    removed: list[str] = []
    out: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept_groups = []
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                kept_groups.append(g)
                continue
            kept = []
            for hk in g["hooks"]:
                cmd = hk.get("command", "") if isinstance(hk, dict) else ""
                if cmd and is_managed_command(cmd):
                    removed.append(f"{event}: {cmd}")
                else:
                    kept.append(hk)
            if kept:
                kept_groups.append({**g, "hooks": kept})
            elif not g["hooks"]:
                kept_groups.append(g)
        if kept_groups:
            out[event] = kept_groups
    return out, removed


def merge_hook_map(
    hooks: dict, harness: str, exe: str, install: bool = True
) -> tuple[dict, list[str]]:
    stripped, removed = _strip_managed(hooks or {})
    if install:
        for event, groups in _desired_groups(harness, exe).items():
            stripped.setdefault(event, [])
            stripped[event].extend(groups)
    return stripped, removed


def _json_dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _install_json_hooks(path: Path, harness: str, exe: str, dry_run: bool, install: bool) -> dict:
    old = _read_text(path)
    try:
        data = json.loads(old) if old.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "path": str(path),
            "action": f"error: {path} is not valid JSON ({exc}); left untouched",
        }
    if not isinstance(data, dict):
        return {"path": str(path), "action": "error: top level is not an object; left untouched"}
    hooks, removed = merge_hook_map(data.get("hooks") or {}, harness, exe, install=install)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    new = _json_dump(data) if (data or old.strip()) else ""
    if not install and not old.strip():
        new = old
    # Keep formatting untouched when nothing semantically changed.
    try:
        if old.strip() and json.loads(old) == data:
            new = old
    except json.JSONDecodeError:
        pass
    res = _apply(path, old, new, dry_run)
    res["replaced"] = [r for r in removed if is_legacy_command(r.split(": ", 1)[1])]
    res["_data"] = data
    return res


# ── Codex hook trust ─────────────────────────────────────────────────────────


def codex_trust(dry_run: bool = False, hooks_data: dict | None = None) -> dict:
    """Record trust for our skmemory hooks in config.toml (verified via app-server)."""
    try:
        from .codex_trust import trust_skmemory_hooks
    except Exception as exc:  # pragma: no cover
        return {"action": f"skip: {exc}"}
    return trust_skmemory_hooks(dry_run=dry_run, hooks_data=hooks_data)


# ── per-harness entry points ─────────────────────────────────────────────────


def _template_file(path: Path, template: str, exe: str, dry_run: bool, install: bool) -> dict:
    from .harness import render

    old = _read_text(path)
    if old and MANAGED_MARK not in old:
        return {
            "path": str(path),
            "action": "error: exists and is not skmemory-managed; left untouched",
        }
    if install:
        return _apply(path, old, render(template, exe), dry_run)
    if not path.exists():
        return {"path": str(path), "action": "absent"}
    if dry_run:
        return {"path": str(path), "action": "would-remove"}
    path.unlink()
    return {"path": str(path), "action": "removed"}


def install_harness(
    harness: str, dry_run: bool = False, install: bool = True, exe: str | None = None
) -> dict:
    exe = exe or skmemory_bin()
    try:
        if harness == "claude":
            res = _install_json_hooks(claude_settings_path(), "claude", exe, dry_run, install)
            res.pop("_data", None)
            return res
        if harness == "codex":
            res = _install_json_hooks(codex_hooks_path(), "codex", exe, dry_run, install)
            data = res.pop("_data", None)
            if install and not str(res.get("action", "")).startswith("error"):
                res["trust"] = codex_trust(dry_run=dry_run, hooks_data=data)
            return res
        if harness == "opencode":
            return _template_file(
                opencode_plugin_path(), "opencode-plugin.js", exe, dry_run, install
            )
        if harness == "pi":
            return _template_file(pi_extension_path(), "pi-extension.ts", exe, dry_run, install)
    except Exception as exc:
        return {"action": f"error: {type(exc).__name__}: {exc}"}
    return {"action": f"error: unknown harness {harness}"}


def resolve_harnesses(choice: str | None) -> list[str]:
    choice = (choice or "auto").lower()
    if choice == "all":
        return list(ALL_HARNESSES)
    if choice == "auto":
        from .register import detect_environments

        return [_ENV_TO_HARNESS[e] for e in detect_environments() if e in _ENV_TO_HARNESS]
    names = [c.strip() for c in choice.split(",") if c.strip()]
    return [_ENV_TO_HARNESS.get(n, n) for n in names if _ENV_TO_HARNESS.get(n, n) in ALL_HARNESSES]


def install(harness: str | None = "auto", dry_run: bool = False) -> dict:
    exe = skmemory_bin()
    return {
        h: install_harness(h, dry_run=dry_run, install=True, exe=exe)
        for h in resolve_harnesses(harness)
    }


def uninstall(harness: str | None = "all", dry_run: bool = False) -> dict:
    exe = skmemory_bin()
    return {
        h: install_harness(h, dry_run=dry_run, install=False, exe=exe)
        for h in resolve_harnesses(harness)
    }


def _json_status(path: Path) -> dict:
    try:
        data = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError:
        return {"path": str(path), "state": "invalid-json"}
    ours, legacy = [], []
    for event, groups in (data.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            for hk in (g.get("hooks") or []) if isinstance(g, dict) else []:
                cmd = hk.get("command", "") if isinstance(hk, dict) else ""
                if _OURS.search(cmd):
                    ours.append(f"{event}[{g.get('matcher', '')}]: {cmd}")
                elif is_legacy_command(cmd):
                    legacy.append(f"{event}[{g.get('matcher', '')}]: {cmd}")
    state = "installed" if ours else ("legacy" if legacy else "not-installed")
    return {"path": str(path), "state": state, "hooks": ours, "legacy": legacy}


def status(harness: str | None = "all") -> dict:
    out = {"skmemory": skmemory_bin()}
    for h in resolve_harnesses(harness):
        if h == "claude":
            out[h] = _json_status(claude_settings_path())
        elif h == "codex":
            out[h] = _json_status(codex_hooks_path())
        else:
            p = opencode_plugin_path() if h == "opencode" else pi_extension_path()
            txt = _read_text(p)
            out[h] = {
                "path": str(p),
                "state": "installed"
                if MANAGED_MARK in txt
                else ("foreign-file" if txt else "not-installed"),
            }
    return out
