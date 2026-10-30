"""`skmemory hook <event>`: one fail-soft entrypoint for every coding harness.

Replaces the per-event bash hooks (session-start-ritual.sh,
post-compact-reinject.sh, pre-compact-save.sh, session-end-save.sh,
stop-checkpoint.sh, sk-activity-log.sh) and skwhisper's inject/save hooks.

Events:
    session-start  budgeted ritual context (startup / clear)
    post-compact   smaller budget context (compact / resume)
    pre-compact    save recent transcript content before compaction
    session-end    save session summary + journal, activity log, skwhisper digest
    stop           cheap breadcrumb checkpoint every 5th stop

Output contract per harness:
    claude, codex  context events print {"hookSpecificOutput": {...additionalContext}}
                   (both harnesses accept the same SessionStart JSON); other events
                   print nothing, except codex Stop which requires JSON ("{}").
    opencode, pi,  context events print plain markdown (the plugin/extension
    generic        injects it into the system prompt).

The hook never crashes the host: every path is wrapped, exit code is always 0,
and slow writes are handed to a fully detached child process so the harness
is not held open (Claude Code cancels hooks that hold its stdout on exit).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HARNESSES = ("claude", "codex", "opencode", "pi", "generic")
EVENTS = ("session-start", "post-compact", "pre-compact", "session-end", "stop")
JSON_HARNESSES = ("claude", "codex")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit", "apply_patch"}


# ── stdin ────────────────────────────────────────────────────────────────────


def read_stdin_json(stream=None, timeout: float = 2.0) -> dict:
    """Read the harness JSON payload from stdin if one is piped. Never blocks long."""
    stream = stream or sys.stdin
    try:
        if stream is None or stream.isatty():
            return {}
    except (AttributeError, ValueError):
        return {}
    try:
        import select

        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            return {}
    except Exception:
        pass  # not selectable (e.g. StringIO in tests): just read
    try:
        raw = stream.read()
    except Exception:
        return {}
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


# ── transcript parsing (Claude JSONL, Codex rollout JSONL, generic) ──────────


def _text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") in ("text", "input_text", "output_text")
                and isinstance(b.get("text"), str)
            ):
                out.append(b["text"])
    return out


def parse_transcript(path: str | None, max_bytes: int = 3_000_000) -> dict:
    """Return {"user": [...], "assistant": [...], "files": [...], "turns": n, "commits": [...]}."""
    res: dict = {"user": [], "assistant": [], "files": [], "turns": 0, "commits": []}
    if not path:
        return res
    p = Path(path).expanduser()
    if not p.is_file():
        return res
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return res
    files: list[str] = []
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        if msg is None and isinstance(rec.get("payload"), dict):
            msg = rec["payload"]
        if msg is None:
            msg = rec
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                    fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get(
                        "path"
                    )
                    if fp:
                        files.append(str(fp))
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    cmd = str((b.get("input") or {}).get("command", ""))
                    m = re.search(r"git commit[^\n]*?-m\s+[\"']([^\"'\n]{3,120})", cmd)
                    if m:
                        res["commits"].append(m.group(1))
        if msg.get("type") == "function_call" and msg.get("name") == "apply_patch":
            files.extend(
                re.findall(r"\*\*\* (?:Update|Add) File: (\S+)", str(msg.get("arguments", "")))
            )
        texts = [t for t in _text_blocks(content) if t.strip()]
        if role == "user":
            human = [t for t in texts if not t.lstrip().startswith("<")]
            if human:
                res["turns"] += 1
                res["user"].extend(human)
        elif role == "assistant":
            res["assistant"].extend(t for t in texts if len(t) >= 20)
    seen: set[str] = set()
    res["files"] = [f for f in files if not (f in seen or seen.add(f))]
    return res


def summarize_transcript(tr: dict, n_user: int, n_asst: int, cap: int = 4000) -> str:
    parts = [f"TURNS: {tr['turns']}"]
    if tr["files"]:
        parts.append("FILES CHANGED: " + ", ".join(tr["files"][:30]))
    if tr["commits"]:
        parts.append("GIT COMMITS: " + "; ".join(tr["commits"][:5]))
    if tr["user"]:
        u = "\n".join("- " + re.sub(r"\s+", " ", t)[:300] for t in tr["user"][-n_user:])
        parts.append("USER REQUESTS:\n" + u[:2000])
    if tr["assistant"]:
        a = "\n".join("- " + re.sub(r"\s+", " ", t)[:300] for t in tr["assistant"][-n_asst:])
        parts.append("WORK DONE:\n" + a[:2000])
    return "\n\n".join(parts)[:cap]


# ── detached persistence ─────────────────────────────────────────────────────


def _spawn_persist(payload: dict) -> bool:
    """Write payload to a 0600 temp file and hand it to a detached child."""
    try:
        fd, path = tempfile.mkstemp(prefix="skmemory-hook-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        env = dict(os.environ)
        agent = payload.get("agent") or ""
        if agent:
            env.update(SKAGENT=agent, SKCAPSTONE_AGENT=agent, SKMEMORY_AGENT=agent)
        env["SKMEMORY_NO_VECTOR"] = "1"
        subprocess.Popen(
            [sys.executable, "-m", "skmemory.hook_cli", "persist", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        return True
    except Exception:
        return False


def persist(path: str) -> int:
    """Child process: do the slow writes. Serialized per agent with a lock."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    agent = payload.get("agent") or "unknown"
    lock_fh = None
    try:
        import fcntl

        lock_fh = open(os.path.join(tempfile.gettempdir(), f"skmemory-hook-{agent}.lock"), "w")  # noqa: SIM115
        deadline = time.monotonic() + 90
        while True:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    return 0
                time.sleep(0.5)
    except Exception:
        pass
    snap = payload.get("snapshot")
    if snap:
        try:
            from .cli import _get_store
            from .models import MemoryLayer, MemoryRole

            store = _get_store(None, None, None, None, no_vector=True)
            store.snapshot(
                title=snap["title"],
                content=snap["content"],
                layer=MemoryLayer(snap.get("layer", "short-term")),
                role=MemoryRole("general"),
                tags=snap.get("tags", []),
                source=snap.get("source", "hook"),
            )
        except Exception:
            pass
    jr = payload.get("journal")
    if jr:
        try:
            from .journal import Journal, JournalEntry

            Journal().write_entry(JournalEntry(**jr))
        except Exception:
            pass
    if lock_fh is not None:
        lock_fh.close()
    act = payload.get("activity")
    if act:
        exe = shutil.which("sk-activity")
        if exe:
            with contextlib.suppress(Exception):
                subprocess.run([exe, "log", *act], timeout=20, capture_output=True)
    if payload.get("digest"):
        _skwhisper_digest(agent)
    return 0


def _skwhisper_digest(agent: str) -> None:
    exe = shutil.which("skwhisper")
    if not exe:
        return
    try:
        import fcntl

        with open(
            os.path.join(tempfile.gettempdir(), f"skwhisper-digest-{agent}.lock"), "w"
        ) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # another digest running for this agent: skip this cycle
            subprocess.run([exe, "digest"], timeout=180, capture_output=True)
            subprocess.run([exe, "curate"], timeout=120, capture_output=True)
    except Exception:
        pass


# ── events ───────────────────────────────────────────────────────────────────


def _short(sid: str | None, n: int = 8) -> str:
    return (sid or "unknown")[:n]


def _context_event(
    event: str, harness: str, budget: int | None, agent: str | None, payload: dict
) -> str:
    from .session_context import build_context

    src = str(payload.get("source") or "")
    if event == "session-start" and src in ("compact", "resume"):
        event = "post-compact"
    res = build_context(event=event, harness=harness, budget=budget, agent=agent)
    return res.text


def _emit_context(text: str, harness: str, out) -> None:
    if harness in JSON_HARNESSES:
        out.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": text,
                    }
                }
            )
        )
    else:
        out.write(text)
    out.write("\n")


def _save_event(event: str, harness: str, agent: str | None, payload: dict, args: dict) -> None:
    from .session_context import resolve_agent

    info = resolve_agent(agent)
    name = info.name or "unknown"
    sid = payload.get("session_id") or args.get("session_id") or "unknown"
    short = _short(sid)
    cwd = payload.get("cwd") or os.getcwd()
    transcript = payload.get("transcript_path") or args.get("transcript")
    tr = parse_transcript(transcript)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")

    if event == "pre-compact":
        trigger = payload.get("trigger") or "auto"
        content = (
            summarize_transcript(tr, 20, 10)
            if (tr["user"] or tr["assistant"])
            else f"Session {short} compacting ({trigger}). Agent: {name}. CWD: {cwd}. Time: {stamp}. (No transcript content extracted)"
        )
        _spawn_persist(
            {
                "agent": name,
                "snapshot": {
                    "title": f"Pre-compact session content ({name}, {short})",
                    "content": content,
                    "layer": "short-term",
                    "tags": [
                        "auto-save",
                        "pre-compact",
                        trigger,
                        f"session:{short}",
                        f"agent:{name}",
                        f"harness:{harness}",
                    ],
                    "source": "hook:pre-compact",
                },
                "journal": {
                    "title": f"Session {short} - pre-compaction",
                    "session_id": short,
                    "participants": [name],
                    "moments": [f"Context compaction ({trigger}, {harness})"],
                    "emotional_summary": "continuity preserved, real content saved",
                    "notes": f"Auto-saved by pre-compact hook. CWD: {cwd}. Files: {', '.join(tr['files'][:20]) or 'none'}",
                },
            }
        )
        return

    # session-end
    reason = payload.get("reason") or args.get("reason") or "other"
    turns = tr["turns"]
    content = (
        summarize_transcript(tr, 30, 15)
        if turns >= 3
        else f"Session {short} ended ({reason}). Agent: {name}. Harness: {harness}. CWD: {cwd}. Time: {stamp}. Turns: {turns}."
    )
    last_user = re.sub(r"\s+", " ", tr["user"][-1])[:200] if tr["user"] else ""
    runtime = {"claude": "claude_code"}.get(harness, harness)
    actor = os.environ.get("SK_ACTIVITY_ACTOR") or ("opus" if harness == "claude" else name)
    summary = f"{harness} session ended ({turns} turns, cwd={Path(str(cwd)).name})"
    if last_user:
        summary += f"; last user msg: {last_user}"
    _spawn_persist(
        {
            "agent": name,
            "snapshot": {
                "title": f"Session {short} ended ({name}, {turns} turns)",
                "content": content,
                "layer": "mid-term" if turns >= 20 else "short-term",
                "tags": [
                    "auto-save",
                    "session-end",
                    str(reason),
                    f"session:{short}",
                    f"agent:{name}",
                    f"harness:{harness}",
                ],
                "source": "hook:session-end",
            },
            "journal": {
                "title": f"Session {short} - ended",
                "session_id": short,
                "participants": [name],
                "moments": [f"Session ended ({reason}), {turns} turns"],
                "emotional_summary": "session complete, content preserved",
                "notes": f"Harness: {harness}. CWD: {cwd}. Reason: {reason}. Files: {', '.join(tr['files'][:30]) or 'none'}",
            },
            "activity": [
                "--runtime",
                runtime,
                "--kind",
                "session_end",
                "--actor",
                actor,
                "--session",
                _short(sid, 12),
                "--summary",
                summary,
            ],
            "digest": os.environ.get("SKMEMORY_HOOK_NO_DIGEST") != "1",
        }
    )


def _stop_event(harness: str, agent: str | None, payload: dict, args: dict) -> None:
    from .session_context import resolve_agent

    info = resolve_agent(agent)
    if not info.home or not info.home.is_dir():
        return
    sid = payload.get("session_id") or args.get("session_id") or "unknown"
    short = _short(sid)
    counter = (
        Path(tempfile.gettempdir())
        / f"skmemory-stop-counter-{re.sub(r'[^A-Za-z0-9_-]', '', short)}"
    )
    try:
        count = int(counter.read_text().strip() or 0) + 1
    except (OSError, ValueError):
        count = 1
    with contextlib.suppress(OSError):
        counter.write_text(str(count))
    if count % 5:
        return
    last_work = str(payload.get("last_assistant_message") or "")
    last_file = ""
    transcript = payload.get("transcript_path") or args.get("transcript")
    if transcript and not last_work:
        tr = parse_transcript(transcript, max_bytes=200_000)
        last_work = tr["assistant"][-1] if tr["assistant"] else ""
        last_file = tr["files"][-1] if tr["files"] else ""
    ck = info.home / "memory" / ".last_checkpoint"
    try:
        ck.parent.mkdir(parents=True, exist_ok=True)
        ck.write_text(
            json.dumps(
                {
                    "session_id": short,
                    "agent": info.name,
                    "harness": harness,
                    "stop_number": count,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last_work": re.sub(r"\s+", " ", last_work)[:300],
                    "last_file": last_file,
                },
                indent=2,
            )
        )
    except OSError:
        pass


def run_hook(
    event: str,
    harness: str = "generic",
    budget: int | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    transcript: str | None = None,
    reason: str | None = None,
    stdin=None,
    out=None,
) -> int:
    """Dispatch one hook event. Always returns 0."""
    out = out or sys.stdout
    harness = harness if harness in HARNESSES else "generic"
    args = {"session_id": session_id, "transcript": transcript, "reason": reason}
    try:
        payload = read_stdin_json(stdin)
    except Exception:
        payload = {}
    try:
        if event in ("session-start", "post-compact"):
            text = _context_event(event, harness, budget, agent, payload)
            _emit_context(text, harness, out)
        elif event in ("pre-compact", "session-end"):
            _save_event(event, harness, agent, payload, args)
        elif event == "stop":
            _stop_event(harness, agent, payload, args)
        if harness == "codex" and event == "stop":
            out.write("{}\n")
    except Exception as exc:  # never break the host
        if os.environ.get("SKMEMORY_HOOK_DEBUG") == "1":
            print(f"skmemory hook {event}: {type(exc).__name__}: {exc}", file=sys.stderr)
    with contextlib.suppress(Exception):
        out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python -m skmemory.hook_cli persist <file>` (internal) or `<event> [--harness h]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["persist"] and len(argv) > 1:
        return persist(argv[1])
    if not argv:
        return 0
    event = argv[0]
    harness = "generic"
    if "--harness" in argv:
        i = argv.index("--harness")
        if i + 1 < len(argv):
            harness = argv[i + 1]
    return run_hook(event, harness)


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        rc = 0
    sys.stdout.flush()
    os._exit(rc)
