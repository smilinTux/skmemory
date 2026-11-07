"""Harness sidecar feeding the outcome ledger (SKMEM-P4-02).

Two producers, both writing through :class:`skmemory.ledger.Ledger`:

* :func:`record_transcript` runs from the Claude Code Stop hook. For every ``Skill``
  tool invocation in the transcript it records ``served``. It records ``followed``
  when a later ``Bash`` call in the same turn runs a command the skill declares.
* :func:`import_hermes_usage` imports ``~/.hermes/skills/.usage.json`` once,
  idempotently, reading the file and never writing it.

What the signals mean (read before trusting a score)
----------------------------------------------------
``followed`` from the transcript is a HEURISTIC. A skill "declares" the programs and
scripts that appear in its SKILL.md code fences and inline code. A Bash call in the
same turn whose command starts with one of them counts as following the skill. It
flatters: the model may have run that program for its own reasons, and a skill that
declares only generic commands (``ls``, ``cat``, ``python``) never matches.

So a heuristic follow is stored with ``effect=null`` and ``source`` naming the
heuristic. ``success`` is NEVER written by the Stop hook: a transcript has no explicit
positive signal, and a command that did not fail is not evidence the skill helped.
Neither ``success`` nor ``harm`` is ever written here. A matched call that came back
with ``is_error`` set is recorded as ``followed_error`` with effect null: a grep with no
match or a deliberately failing test would otherwise brand a good skill harmful. Harm and
success come only from an explicit record by an agent or user. Each event states in
``missing`` what stays unknown (the cause of a failure, later user corrections, MCP tool
calls are not matched).

Events carry no transcript text: ids, skill name, version hash, timestamps only.
Idempotency: every event id is a hash of the session id and the ``tool_use`` id (or the
Hermes skill, kind and ordinal), so re-reading a transcript or re-importing a file
appends nothing new.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from .ledger import Ledger

TAIL_BYTES = 600_000
SRC_SKILL = "claude-stop:skill-tool"
SRC_HEURISTIC = "heuristic:bash-command-match"
SRC_ERROR = "tool-error:matched-command"
SRC_HERMES = "hermes-usage-counter"

# Programs that say nothing about which skill ran: never a signature on their own.
_TRIVIAL = frozenset(
    [
        "cd",
        "ls",
        "cat",
        "echo",
        "grep",
        "rg",
        "sed",
        "awk",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "head",
        "tail",
        "export",
        "set",
        "true",
        "test",
        "curl",
        "wget",
        "python",
        "python3",
        "bash",
        "sh",
        "env",
        "sudo",
        "find",
        "tee",
        "touch",
        "chmod",
        "pwd",
        "which",
        "sort",
        "uniq",
        "wc",
        "diff",
        "node",
        "npx",
        "pip",
        "pip3",
        "source",
        "read",
        "printf",
        "kill",
        "sleep",
        "tar",
        "unzip",
    ]
)
# Multi-purpose tools: only a program plus subcommand identifies the skill.
_MULTI = frozenset(["git", "docker", "npm", "gh", "kubectl", "systemctl", "uv", "cargo"])
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]{2,200})`")
_SPLIT = re.compile(r"\|\||&&|[;|\n]")


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


# skill resolution ----------------------------------------------------------


def find_skill_file(name: str, home: Path, cwd: str | None = None) -> Path | None:
    """Locate SKILL.md for a skill name (plugin form ``plugin:skill`` supported)."""
    plugin, _, skill = name.rpartition(":")
    if not re.fullmatch(r"[\w.-]+", skill) or (plugin and not re.fullmatch(r"[\w.-]+", plugin)):
        return None
    cands: list[Path] = []
    if cwd:
        cands.append(Path(cwd) / ".claude" / "skills" / skill / "SKILL.md")
    cands.append(home / ".claude" / "skills" / skill / "SKILL.md")
    for c in cands:
        if c.is_file():
            return c
    cache = home / ".claude" / "plugins" / "cache"
    pat = f"*/{plugin or '*'}/*/skills/{skill}/SKILL.md"
    try:
        hits = sorted(cache.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        hits = []
    return hits[0] if hits else None


def skill_version(path: Path | None) -> tuple[str, str]:
    """(version, text). Version is a content hash so a patched skill is a new version."""
    if path is None:
        return "unknown", ""
    try:
        raw = path.read_bytes()
    except OSError:
        return "unknown", ""
    return "sha:" + hashlib.sha256(raw).hexdigest()[:12], raw.decode("utf-8", "replace")


def _signature(segment: str) -> tuple[str, ...] | None:
    seg = segment.strip().lstrip("$>").strip()
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    toks = [t for t in toks if not re.fullmatch(r"[A-Za-z_]\w*=\S*", t)]  # env prefixes
    if not toks:
        return None
    prog = os.path.basename(toks[0])
    if not re.fullmatch(r"[\w.+-]{2,}", prog) or prog in _TRIVIAL:
        return None
    if prog in _MULTI:
        sub = next((t for t in toks[1:] if not t.startswith("-")), None)
        return (prog, sub) if sub and re.fullmatch(r"[a-z][\w-]*", sub) else None
    return (prog,)


def declared_commands(skill_md: str) -> set[tuple[str, ...]]:
    """Signatures (program, or program+subcommand) a skill declares in code."""
    spans = _FENCE.findall(skill_md)
    spans += _INLINE.findall(_FENCE.sub("", skill_md))
    out: set[tuple[str, ...]] = set()
    for span in spans:
        for seg in _SPLIT.split(span):
            sig = _signature(seg)
            if sig:
                out.add(sig)
    return out


def command_matches(command: str, sigs: set[tuple[str, ...]]) -> bool:
    return any(s in sigs for seg in _SPLIT.split(command) if (s := _signature(seg)))


# transcript ----------------------------------------------------------------


def _human_text(content) -> bool:
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith("<")
    if isinstance(content, list):
        return any(
            isinstance(b, dict)
            and b.get("type") in ("text", "input_text")
            and str(b.get("text", "")).strip()
            and not str(b.get("text", "")).lstrip().startswith("<")
            for b in content
        )
    return False


def scan_transcript(path: str | Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """Skill invocations with their same-turn Bash calls and results. No text kept."""
    p = Path(path).expanduser()
    try:
        with p.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
            if size > tail_bytes:
                data = data.split(b"\n", 1)[-1]  # drop the partial first line
    except OSError:
        return []
    if b'"name":"Skill"' not in data and b'"name": "Skill"' not in data:
        return []  # fast path: the common turn has no skill at all
    calls: list[dict] = []
    open_skill: dict | None = None
    bash: dict[str, dict] = {}
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        role = msg.get("role")
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in bash
                    ):
                        bash[b["tool_use_id"]]["error"] = bool(b.get("is_error"))
            if _human_text(content):
                open_skill = None  # the turn ended: the window is closed
        elif role == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use" or not b.get("id"):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if b.get("name") == "Skill" and isinstance(inp.get("skill"), str):
                    open_skill = {
                        "tool_use_id": b["id"],
                        "skill": inp["skill"].strip().lstrip("/"),
                        "at": rec.get("timestamp"),
                        "session": rec.get("sessionId") or "",
                        "cwd": rec.get("cwd"),
                        "bash": [],
                    }
                    calls.append(open_skill)
                elif b.get("name") == "Bash" and open_skill is not None:
                    entry = {"command": str(inp.get("command", "")), "error": None}
                    bash[b["id"]] = entry
                    open_skill["bash"].append(entry)
    return calls


def record_transcript(
    transcript: str | Path,
    base_path: str | Path,
    session: str = "",
    home: Path | None = None,
    host: str | None = None,
) -> int:
    """Record served/followed for the skills invoked in a transcript. Returns events written."""
    home = home or Path.home()
    calls = scan_transcript(transcript)
    if not calls:
        return 0
    led = Ledger(base_path, host=host)
    written = 0
    for c in calls:
        sid = c["session"] or session or "unknown"
        version, text = skill_version(find_skill_file(c["skill"], home, c.get("cwd")))
        at = c["at"]
        base_id = _sha(sid, c["tool_use_id"])
        before = led._known(_sha("served", base_id))
        led.record(
            c["skill"],
            version,
            "served",
            session=sid,
            at=at,
            event_id=_sha("served", base_id),
            source=SRC_SKILL,
        )
        written += 0 if before else 1
        sigs = declared_commands(text)
        matched = [b for b in c["bash"] if sigs and command_matches(b["command"], sigs)]
        if not matched:
            continue
        failed = any(b["error"] for b in matched)
        kind = "followed_error" if failed else "followed"
        eid = _sha(kind, base_id)
        before = led._known(eid)
        led.record(
            c["skill"],
            version,
            kind,
            session=sid,
            at=at,
            event_id=eid,
            source=SRC_ERROR if failed else SRC_HEURISTIC,
            missing=(
                "matched command failed; cause unknown (grep miss, deliberate failing test, "
                "the skill); effect left null"
                if failed
                else "no explicit success or harm signal; later user reaction not read"
            ),
        )
        written += 0 if before else 1
    return written


# Hermes --------------------------------------------------------------------


def import_hermes_usage(usage_path: str | Path, ledger: Ledger) -> dict:
    """Import ``.usage.json`` as served/followed events. Opens the file read-only.

    Real shape: ``{skill: {use_count, view_count, last_used_at, last_viewed_at,
    patch_count, created_by, state, ...}}``. There are NO per-use timestamps, only
    the last one, so each counter becomes N events keyed ``(skill, kind, ordinal)``:
    re-importing adds only ordinals beyond what is already held. All N share the last
    timestamp (the true times are missing; effect is null so decay never reads them).
    ``view_count`` is served (the skill reached the model's context), ``use_count`` is
    followed (Hermes counted a use). Where use exceeds view, served is raised to use.
    The version is ``patch-<patch_count>``: earlier uses at older patch levels are
    attributed to the current one (missing).
    """
    with open(usage_path, "rb") as fh:  # read-only: never opened for write
        data = json.loads(fh.read())
    held = {e["id"] for e in ledger.events()}
    stats = {"skills": 0, "served": 0, "followed": 0, "skipped": 0}
    for name, st in sorted(data.items()):
        if not isinstance(st, dict):
            stats["skipped"] += 1
            continue
        use = int(st.get("use_count") or 0)
        served = max(int(st.get("view_count") or 0), use)
        at = st.get("last_used_at") or st.get("last_viewed_at") or st.get("created_at")
        if not served or not at:
            stats["skipped"] += 1
            continue
        stats["skills"] += 1
        version = f"patch-{int(st.get('patch_count') or 0)}"
        for kind, n in (("served", served), ("followed", use)):
            when = (st.get("last_viewed_at") if kind == "served" else None) or at
            for i in range(n):
                eid = _sha("hermes", name, kind, str(i))
                if eid in held:
                    continue
                ledger.record(
                    name,
                    version,
                    kind,
                    session="hermes-usage",
                    at=when,
                    event_id=eid,
                    source=SRC_HERMES,
                    missing="per-use time and outcome unknown; at is the last recorded use",
                )
                stats[kind] += 1
    return stats
