"""Progressive disclosure: index rows, source_ref parsing, raw episode expand.

Keys versus values (SKMEM-P3-07). A search can return a cheap index row (the
key: id, title, date, rank score, short snippet), ``memory_recall`` returns the
full memory (the value), and ``memory_expand`` returns the raw episode slice a
digest was made from, read from the session JSONL under the agent's own
sessions directory.

source_ref grammar for session digests::

    session:<session_id>                 whole session
    session:<session_id>@<start>-<end>   byte range in <session_id>.jsonl
    session:<session_id>@L<n>            line n (1-based) of the transcript

Token figures are a chars/4 estimate, not a tokenizer count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNIPPET_CHARS = 160
EXPAND_DEFAULT_CHARS = 4000
EXPAND_MAX_CHARS = 20000
EXPAND_LINE_WINDOW = 10

_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_REF_RE = re.compile(
    r"^session:(?P<sid>[^@]+)(?:@(?:L(?P<line>\d+)|(?P<start>\d+)-(?P<end>\d+)))?$"
)


def est_tokens(text: str) -> int:
    """Rough token count: characters / 4 (an estimate, not a tokenizer)."""
    return (len(text) + 3) // 4


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def snippet(memory: Any, limit: int = SNIPPET_CHARS) -> str:
    body = _flat(getattr(memory, "summary", "") or getattr(memory, "content", ""))
    return body if len(body) <= limit else body[: limit - 3].rstrip() + "..."


def index_row(
    memory: Any,
    rank: int,
    full_row: dict,
    *,
    superseded: bool = False,
    superseded_by: str | None = None,
) -> dict:
    """The cheap key for one hit. ``score`` is rank-derived (1/rank): the store
    returns an ordered list and keeps no final per-hit score."""
    import json

    ref = getattr(memory, "source_ref", "") or ""
    row = {
        "id": memory.id,
        "title": memory.title,
        "date": str(getattr(memory, "created_at", "") or ""),
        "layer": getattr(getattr(memory, "layer", ""), "value", getattr(memory, "layer", "")),
        "score": round(1.0 / rank, 4),
        "score_kind": "rank",
        "snippet": snippet(memory),
        "source_ref": ref,
        "expandable": parse_source_ref(ref) is not None,
        "superseded": superseded,
        "superseded_by": superseded_by,
    }
    row["est_tokens"] = est_tokens(json.dumps(row, default=str))
    row["est_tokens_full"] = est_tokens(json.dumps(full_row, default=str))
    return row


@dataclass
class SourceRef:
    session_id: str
    line: int | None = None
    start: int | None = None
    end: int | None = None


def parse_source_ref(ref: str) -> SourceRef | None:
    m = _REF_RE.match((ref or "").strip())
    if not m or not _SID_RE.match(m.group("sid")):
        return None
    return SourceRef(
        m.group("sid"),
        line=int(m.group("line")) if m.group("line") else None,
        start=int(m.group("start")) if m.group("start") else None,
        end=int(m.group("end")) if m.group("end") else None,
    )


class ExpandError(Exception):
    """Expand refused or impossible; the message is safe to show a caller."""


def sessions_dir() -> Path:
    from .agents import get_agent_paths

    return get_agent_paths()["base"] / "sessions"


def _resolve_session_file(root: Path, sid: str) -> Path:
    try:
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise ExpandError("sessions directory not found") from exc
    candidate = root / f"{sid}.jsonl"
    try:
        real = candidate.resolve(strict=True)
    except OSError as exc:
        raise ExpandError("session file not found") from exc
    if not real.is_relative_to(root_real) or not real.is_file():
        raise ExpandError("source_ref resolves outside the agent's sessions directory")
    return real


def expand_episode(source_ref: str, root: Path, *, max_chars: int = EXPAND_DEFAULT_CHARS) -> dict:
    """Return the raw slice a ``source_ref`` points at, capped at ``max_chars``."""
    ref = parse_source_ref(source_ref)
    if ref is None:
        raise ExpandError("memory has no resolvable session source_ref")
    cap = max(1, min(int(max_chars), EXPAND_MAX_CHARS))
    path = _resolve_session_file(root, ref.session_id)
    size = path.stat().st_size
    with path.open("rb") as fh:
        if ref.start is not None and ref.end is not None:
            start = min(ref.start, size)
            end = max(start, min(ref.end, size))
            fh.seek(start)
            raw = fh.read(min(end - start, cap * 4))
            where = {"start": start, "end": end}
        elif ref.line is not None:
            lo = max(1, ref.line - EXPAND_LINE_WINDOW)
            hi = ref.line + EXPAND_LINE_WINDOW
            lines = []
            for n, ln in enumerate(fh, 1):
                if n > hi:
                    break
                if n >= lo:
                    lines.append(ln)
            raw = b"".join(lines)
            where = {"line": ref.line, "from_line": lo, "to_line": min(hi, lo + len(lines) - 1)}
        else:
            raw = fh.read(cap * 4)
            where = {"start": 0, "end": min(size, cap * 4)}
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > cap
    text = text[:cap]
    return {
        "session_id": ref.session_id,
        "range": where,
        "text": text,
        "truncated": truncated,
        "est_tokens": est_tokens(text),
    }
