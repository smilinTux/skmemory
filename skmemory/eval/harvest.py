"""Harvest real memory queries into a private candidates file (SKMEM-P1-04).

Reads, never writes, two session stores:

* Claude Code session JSONL (``~/.claude/projects/<project>/**/*.jsonl``):
  ``memory_search`` / ``memory_recall`` MCP tool calls, and ``skmemory search``
  or ``skmemory recall`` shell commands.
* Hermes sessions (``~/.hermes/sessions/session_*.json`` and ``*.jsonl``):
  ``memory_search`` / ``memory_recall`` function calls, from owner platforms only.

Only queries inside the window (default 90 days) and from the OWNER's sessions
are kept. Claude Code sessions in the given project dir are the owner's. A
Hermes session counts only when its platform is in ``OWNER_PLATFORMS`` (the
owner's terminal and the owner's own scheduled jobs); chat platforms such as
telegram can carry other people's messages and are excluded.

Queries are de-duplicated on a normalised form. Sensitive queries (sacred or
medical) are dropped here, counted, and never written or sent anywhere.

Deviation from the card: the recall_cache is a corpus decomposition cache, not a
query log, and no MCP server query log exists on this node, so neither is read.

Output is query text. It must stay outside the repo; see :func:`check_private_dir`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW_DAYS = 90
OWNER_PLATFORMS = frozenset({"cli", "cron"})
_TOOL_RE = re.compile(r"memory_(search|recall)$")
_SHELL_RE = re.compile(r"skmemory\s+(search|recall)\s+(.+)")

# ---------------------------------------------------------------------------
# Sensitivity: conservative on purpose. A false positive costs one query.
# ---------------------------------------------------------------------------

SENSITIVE_TAGS = frozenset(
    {
        "sacred",
        "private",
        "personal-history",
        "no-ritual",
        "medical",
        "health",
        "grief",
        "therapy",
        "mental-health",
        "chef-medical",
    }
)
_SENSITIVE_RE = re.compile(
    r"\b(sacred|medical|medic\w*|health (?:issue|condition|history|record|insurance|problem)\w*|diagnos\w*|transplant\w*|liver|cancer|chemo\w*|"
    r"surger\w+|hospital\w*|doctor|physician|prescri\w+|dosage|dose|nootropic\w*|bacopa|"
    r"therap\w+|psych\w+|depress\w*|suicid\w*|grief|griev\w*|funeral|widow\w*|sarah|"
    r"symptom\w*|illness|disease|blood (?:pressure|test|work|sugar|clot)|clinic\w*|pharma\w*|cholesterol|insulin|"
    r"medication\w*|rehab|addict\w*)\b",
    re.I,
)


def is_sensitive_text(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def is_sensitive_memory(title: str, content: str, tags: Iterable[str]) -> bool:
    if {str(t).casefold() for t in tags} & SENSITIVE_TAGS:
        return True
    return is_sensitive_text(title) or is_sensitive_text(content)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    qid: str  # sha256 of the normalised query, 16 hex
    query: str
    source: str  # claude_code | hermes
    session: str  # sha256 of the session id, 12 hex (never the raw id)
    ts: str  # ISO of the first sighting
    seen: int  # how many times the same query was issued


def normalise(query: str) -> str:
    return " ".join(query.split()).casefold()


def qid_of(query: str) -> str:
    return hashlib.sha256(normalise(query).encode()).hexdigest()[:16]


def _sess(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _usable(q: object) -> bool:
    if not isinstance(q, str):
        return False
    q = q.strip()
    if len(q) < 3 or len(q) > 400:
        return False
    return not re.fullmatch(r"[0-9a-f-]{8,36}", q)  # a bare id is a lookup, not a query


def _parse_ts(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str) and value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _shell_queries(cmd: str) -> Iterator[str]:
    for seg in re.split(r"[;|&\n]+", cmd):
        m = _SHELL_RE.search(seg)
        if not m:
            continue
        try:
            toks = shlex.split(m.group(2))
        except ValueError:
            continue
        for t in toks:
            if not t.startswith("-"):
                yield t  # the first positional is the query (search) or an id (recall)
                break


def iter_claude(project_dir: Path) -> Iterator[tuple[str, str, datetime | None, str]]:
    """Yield ``(query, session_id, ts, source)`` from Claude Code JSONL."""
    for f in sorted(project_dir.rglob("*.jsonl")):
        try:
            fh = f.open(errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "memory" not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                    continue
                sid = str(o.get("sessionId") or f.stem)
                ts = _parse_ts(o.get("timestamp"))
                for b in msg["content"]:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = str(b.get("name", "")), b.get("input") or {}
                    if _TOOL_RE.search(name) and isinstance(inp, dict):
                        yield inp.get("query"), sid, ts, "claude_code"
                    elif name == "Bash" and isinstance(inp, dict):
                        for q in _shell_queries(str(inp.get("command", ""))):
                            yield q, sid, ts, "claude_code"


def _hermes_calls(messages: list) -> Iterator[str]:
    for m in messages:
        if not isinstance(m, dict):
            continue
        for t in m.get("tool_calls") or []:
            fn = (t or {}).get("function") or {}
            if _TOOL_RE.search(str(fn.get("name", ""))):
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    continue
                if isinstance(args, dict):
                    yield args.get("query")


def iter_hermes(
    sessions_dir: Path, owner_platforms: frozenset[str] = OWNER_PLATFORMS
) -> Iterator[tuple[str, str, datetime | None, str]]:
    for f in sorted(sessions_dir.glob("session_*.json")):
        try:
            d = json.loads(f.read_text(errors="ignore"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("platform") not in owner_platforms:
            continue
        ts = _parse_ts(d.get("last_updated") or d.get("session_start"))
        for q in _hermes_calls(d.get("messages") or []):
            yield q, str(d.get("session_id") or f.stem), ts, "hermes"
    # The streaming ``*.jsonl`` exports carry no platform, so they cannot be proven
    # to be the owner's and are not read.


def harvest(
    sources: Iterable[tuple[str, str, datetime | None, str]],
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
) -> tuple[list[Candidate], dict]:
    """De-duplicate, window and sensitivity-filter raw query sightings."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    stats: Counter = Counter()
    seen: dict[str, dict] = {}
    for q, sid, ts, source in sources:
        stats["raw"] += 1
        if not _usable(q):
            stats["unusable"] += 1
            continue
        if ts is None or ts < cutoff:
            stats["outside_window"] += 1
            continue
        q = " ".join(q.split())
        if is_sensitive_text(q):
            stats["excluded_sensitive"] += 1
            continue
        key = normalise(q)
        if key in seen:
            seen[key]["seen"] += 1
            stats["duplicate"] += 1
            if ts < seen[key]["ts"]:
                seen[key]["ts"] = ts
            continue
        seen[key] = {"query": q, "source": source, "session": _sess(sid), "ts": ts, "seen": 1}
    out = [
        Candidate(
            qid_of(v["query"]),
            v["query"],
            v["source"],
            v["session"],
            v["ts"].isoformat(),
            v["seen"],
        )
        for v in seen.values()
    ]
    out.sort(key=lambda c: c.qid)
    stats["candidates"] = len(out)
    by_source = Counter(c.source for c in out)
    return out, {**dict(stats), "by_source": dict(by_source)}


# ---------------------------------------------------------------------------
# Private files
# ---------------------------------------------------------------------------


def check_private_dir(path: str | os.PathLike) -> Path:
    """The private dir must not sit inside the repo, any git work tree or ~/.skcapstone."""
    p = Path(path).expanduser().resolve()
    banned = [Path.home() / ".skcapstone", Path(__file__).resolve().parents[2]]
    for b in banned:
        b = b.resolve()
        if p == b or b in p.parents:
            raise ValueError(f"refusing private data under {b}")
    for anc in [p, *p.parents]:
        if (anc / ".git").exists():
            raise ValueError(f"refusing private data inside a git work tree ({anc})")
    return p


def ensure_private_dir(path: str | os.PathLike) -> Path:
    p = check_private_dir(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def write_candidates(private_dir: str | os.PathLike, cands: list[Candidate], stats: dict) -> Path:
    d = ensure_private_dir(private_dir)
    path = d / "candidates.jsonl"
    write_private(path, "".join(json.dumps(asdict(c), sort_keys=True) + "\n" for c in cands))
    write_private(d / "harvest-stats.json", json.dumps(stats, indent=2, sort_keys=True))
    return path


def load_candidates(path: str | os.PathLike) -> list[Candidate]:
    return [
        Candidate(**json.loads(ln)) for ln in Path(path).read_text().splitlines() if ln.strip()
    ]


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="harvest real memory queries (read-only)")
    p.add_argument("--private-dir", required=True)
    p.add_argument("--claude-dir", default=str(Path.home() / ".claude/projects/-home-cbrd21"))
    p.add_argument("--hermes-dir", default=str(Path.home() / ".hermes/sessions"))
    p.add_argument("--days", type=int, default=WINDOW_DAYS)
    a = p.parse_args()
    check_private_dir(a.private_dir)

    def chain():
        yield from iter_claude(Path(a.claude_dir))
        yield from iter_hermes(Path(a.hermes_dir))

    cands, stats = harvest(chain(), window_days=a.days)
    write_candidates(a.private_dir, cands, stats)
    print(json.dumps(stats, sort_keys=True))  # counts only, never query text


if __name__ == "__main__":
    main()
