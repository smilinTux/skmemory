"""UserPromptSubmit injection for standing intentions (SKMEM-P2-04).

Runs on every user prompt on every harness, so it is cheap and total: any
failure yields no output. Stored titles are data, not instructions: each is
flattened to one line, stripped of control characters, quoted, and prefixed
``Standing intent (from memory <id>):`` so it cannot pose as a directive.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

LINE_CAP = 300
TOTAL_CAP = 1200
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def clean_text(text: object, limit: int) -> str:
    """One line, no control or format characters, bounded length."""
    s = str(text or "")
    s = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in s)
    s = " ".join(s.split()).replace('"', "'")
    return s if len(s) <= limit else s[: max(0, limit - 3)] + "..."


def format_line(i) -> str:
    """``Standing intent (from memory <id>): "<title>"``, at most LINE_CAP chars."""
    ref = _ID_SAFE.sub("", str(i.origin_memory_id or i.id))[:64]
    kind = "memory" if i.origin_memory_id else "intention"
    head = f"Standing intent (from {kind} {ref}): "
    room = LINE_CAP - len(head) - 2
    return f'{head}"{clean_text(i.title, room)}"'


def repo_scope(cwd: str | None) -> dict[str, str]:
    """``{"repo": basename of the git root above cwd}``; empty when not in a repo."""
    if not cwd:
        return {}
    try:
        p = Path(cwd).resolve()
        for d in (p, *p.parents):
            if (d / ".git").exists():
                return {"repo": d.name}
    except OSError:
        pass
    return {}


def render(intentions: list) -> str:
    """Join at most TOTAL_CAP chars of lines; whole lines only."""
    out: list[str] = []
    used = 0
    for i in intentions:
        line = format_line(i)
        if used + len(line) + 1 > TOTAL_CAP:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def prompt_context(payload: dict, base_path: str | Path | None = None) -> str:
    """Text to inject for this prompt, or "" (never raises)."""
    try:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return ""
        if base_path is None:
            from .config import SKMEMORY_HOME

            if SKMEMORY_HOME is None:
                return ""
            base_path = Path(SKMEMORY_HOME) / "memory"
        base = Path(base_path)
        if not (base / "intentions").is_dir():  # nothing stored: do not create dirs per prompt
            return ""
        from .intentions import prefilter_safe

        fired = prefilter_safe(base, prompt, repo_scope(payload.get("cwd")))
        return render(fired) if fired else ""
    except Exception:  # noqa: BLE001 - must never fail a prompt
        return ""
