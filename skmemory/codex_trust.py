"""Record Codex hook trust for the skmemory hooks we install.

Codex (0.16x) only runs hooks the user has trusted. Trust lives in
``$CODEX_HOME/config.toml`` as::

    [hooks.state."<abs source path>:<event_label>:<group>:<handler>"]
    trusted_hash = "sha256:<hex>"

The hash is sha256 over compact, key-sorted JSON of the normalized hook
definition (codex-rs hooks/src/engine/discovery.rs ``hook_hash`` at
rust-v0.162.1); it was verified to match ``codex app-server`` ``hooks/list``
``currentHash``. Because the hash covers the exact command, matcher and
timeout, trusting it trusts only the precise handler we wrote. Any edit to
the hook (by anyone) invalidates the trust and Codex asks again.

Only handlers whose command is an ``skmemory hook`` command are trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

EVENT_LABEL = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
_NO_MATCHER = {"UserPromptSubmit", "Stop", "Interrupt"}
_CTX_EVENTS = {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"}


def hook_hash(event: str, matcher: str | None, handler: dict) -> str:
    """Codex's trust hash for one command handler (non-Windows)."""
    if event in ("SessionEnd", "Interrupt"):
        timeout = min(max(int(handler.get("timeout", 1)), 1), 3)
    else:
        timeout = max(int(handler.get("timeout", 600)), 1)
    h: dict = {
        "type": "command",
        "command": handler["command"],
        "timeout": timeout,
        "async": bool(handler.get("async", False)),
    }
    if handler.get("statusMessage") is not None:
        h["statusMessage"] = handler["statusMessage"]
    acl = handler.get("additionalContextLimit")
    if event in _CTX_EVENTS and acl is not None and acl != 2500:
        h["additionalContextLimit"] = acl
    ident: dict = {"event_name": EVENT_LABEL[event], "hooks": [h]}
    if event not in _NO_MATCHER and matcher is not None:
        ident["matcher"] = matcher
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def hook_key(source_path: str, event: str, gi: int, hi: int) -> str:
    return f"{source_path}:{EVENT_LABEL[event]}:{gi}:{hi}"


def our_trust_entries(hooks_json: Path) -> dict[str, str]:
    """{key: hash} for every skmemory handler in hooks.json."""
    from .hook_install import _OURS

    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    src = str(hooks_json.absolute())
    out: dict[str, str] = {}
    for event, groups in (data.get("hooks") or {}).items():
        if event not in EVENT_LABEL or not isinstance(groups, list):
            continue
        for gi, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            for hi, hk in enumerate(g.get("hooks") or []):
                if (
                    isinstance(hk, dict)
                    and hk.get("type", "command") == "command"
                    and _OURS.search(str(hk.get("command", "")))
                ):
                    out[hook_key(src, event, gi, hi)] = hook_hash(event, g.get("matcher"), hk)
    return out


def upsert_trust_toml(text: str, entries: dict[str, str]) -> str:
    """Insert or replace [hooks.state."<key>"] tables in config.toml text."""
    for key, digest in entries.items():
        header = f"[hooks.state.{json.dumps(key)}]"
        table = f'{header}\ntrusted_hash = "{digest}"\n'
        pat = re.compile(r"(?m)^" + re.escape(header) + r"[ \t]*\n(?:(?!\s*\[).*\n?)*")
        if pat.search(text):
            text = pat.sub(lambda _m, t=table: t, text, count=1)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += ("\n" if text.strip() else "") + table
    return text


def codex_hooks_list(codex_home: Path, cwd: str = "/", timeout: float = 20.0) -> list[dict] | None:
    """Ask `codex app-server` for hooks/list. None when codex is unavailable."""
    exe = shutil.which("codex")
    if not exe:
        return None
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    try:
        p = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None

    def send(obj: dict) -> None:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"clientInfo": {"name": "skmemory", "version": "0"}},
            }
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not select.select([p.stdout], [], [], 0.5)[0]:
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 0:
                send({"jsonrpc": "2.0", "method": "initialized"})
                send(
                    {"jsonrpc": "2.0", "id": 1, "method": "hooks/list", "params": {"cwds": [cwd]}}
                )
            elif msg.get("id") == 1:
                if "error" in msg:
                    return None
                data = (msg.get("result") or {}).get("data") or [{}]
                return list(data[0].get("hooks") or [])
        return None
    except Exception:
        return None
    finally:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def trust_skmemory_hooks(dry_run: bool = False, verify: bool = True) -> dict:
    """Trust our handlers in hooks.json by writing hooks.state into config.toml."""
    from .hook_install import _apply, _read_text, codex_home, codex_hooks_path

    if os.environ.get("SKMEMORY_CODEX_NO_TRUST") == "1":
        return {"action": "skip: SKMEMORY_CODEX_NO_TRUST=1 (approve once with /hooks in codex)"}
    entries = our_trust_entries(codex_hooks_path())
    if not entries and dry_run:
        return {"action": "would-trust skmemory handlers after hooks.json is written"}
    if not entries:
        return {"action": "skip: no skmemory handlers in hooks.json"}
    cfg = codex_home() / "config.toml"
    old = _read_text(cfg)
    res = _apply(cfg, old, upsert_trust_toml(old, entries), dry_run)
    res["trusted"] = sorted(entries)
    if verify and not dry_run:
        listed = codex_hooks_list(codex_home())
        if listed is not None:
            mine = {h.get("key"): h.get("trustStatus") for h in listed if h.get("key") in entries}
            res["verified"] = mine
            if any(v != "trusted" for v in mine.values()) or len(mine) != len(entries):
                res["warning"] = (
                    "codex does not report every skmemory hook as trusted; run /hooks in codex"
                )
    return res
