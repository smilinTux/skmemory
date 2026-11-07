"""Read-only index over the skill stores, ranked with ledger scores (SKMEM-P4-03).

Stores (defaults, each configurable, missing directories skipped):
``hermes`` ~/.hermes/skills, ``clawd`` ~/clawd/skills, ``claude`` ~/.claude/skills,
``plugins`` ~/.claude/plugins/cache, ``skcapstone`` the skcapstone ``skills`` package.

The index never moves, edits or deletes a skill and writes nothing: it is built in
memory on each call. An entry is name, description, version hash (the same
``sha:<12 hex>`` as the outcome ledger), store and path. First-party ledger scores
join on ``(name, version hash)``. Imported counters (Hermes) join by name and are
kept in separate ``imported_*`` fields; they never feed ranking, the anti-pattern
flag or the served-never-followed flag.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .ledger import Ledger

logger = logging.getLogger(__name__)

DEFAULT_STORES: dict[str, str] = {
    "hermes": "~/.hermes/skills",
    "clawd": "~/clawd/skills",
    "claude": "~/.claude/skills",
    "plugins": "~/.claude/plugins/cache",
    "skcapstone": "",  # resolved from the installed skcapstone package when empty
}
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
MAX_DEPTH = 8
_TOKEN = re.compile(r"[a-z0-9]+")
_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:\s*(.*)$")


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    version: str
    store: str
    path: str
    plugin: str | None = None


@dataclass
class SkillHit:
    entry: SkillEntry
    score: float
    lexical: float
    ledger_net: float | None = None
    follow_rate: float | None = None
    served: int = 0
    followed: int = 0
    imported_served: int = 0
    imported_followed: int = 0
    anti_pattern: bool = False
    flag: str | None = None


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> dict[str, str]:
    """Tolerant ``---`` frontmatter parser: never raises, returns {} when unparseable.

    Handles plain and quoted scalars, ``>``/``|`` block scalars and indented
    continuation lines. Anything it cannot read is ignored, not an error.
    """
    try:
        lines = text.lstrip("﻿").splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        out: dict[str, str] = {}
        key: str | None = None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = _KEY.match(line)
            if m and not line[:1].isspace():
                key = m.group(1)
                val = m.group(2).strip()
                out[key] = "" if val[:1] in (">", "|") else _unquote(val)
            elif key is not None and line.strip():
                out[key] = (out[key] + " " + line.strip()).strip()
        return out
    except Exception:  # malformed input must never abort indexing
        return {}


def _first_text(text: str) -> str:
    body = text.lstrip("﻿")
    if body.startswith("---"):
        parts = body.split("\n---", 1)
        body = parts[1] if len(parts) > 1 else ""
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:300]
    return ""


def read_entry(path: Path, store: str, root: Path) -> SkillEntry | None:
    try:
        raw = path.read_bytes()
    except OSError as err:
        logger.warning("skills: cannot read %s (%s)", path, err)
        return None
    text = raw.decode("utf-8", "replace")
    fm = parse_frontmatter(text)
    name = (fm.get("name") or "").strip() or path.parent.name
    desc = (fm.get("description") or "").strip() or _first_text(text)
    plugin = None
    if store == "plugins":
        try:
            parts = path.relative_to(root).parts
            plugin = parts[1] if len(parts) > 1 else None
        except ValueError:
            pass
    return SkillEntry(
        name=name,
        description=desc,
        version="sha:" + hashlib.sha256(raw).hexdigest()[:12],
        store=store,
        path=str(path),
        plugin=plugin,
    )


def _walk(root: Path):
    base = len(root.parts)
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        if len(Path(dirpath).parts) - base >= MAX_DEPTH:
            dirs[:] = []
        if "SKILL.md" in files:
            yield Path(dirpath) / "SKILL.md"


def resolve_stores(overrides: dict[str, str] | None = None) -> dict[str, Path]:
    cfg = {**DEFAULT_STORES, **(overrides or {})}
    out: dict[str, Path] = {}
    for store, raw in cfg.items():
        if not raw and store == "skcapstone":
            try:
                import importlib.util

                spec = importlib.util.find_spec("skcapstone")
                raw = str(Path(spec.origin).parent / "skills") if spec and spec.origin else ""
            except (ImportError, ValueError):
                raw = ""
        if raw:
            out[store] = Path(raw).expanduser()
    return out


def build_index(overrides: dict[str, str] | None = None) -> tuple[list[SkillEntry], dict]:
    """Index every store. Returns (entries, stats) with per-store counts and skips."""
    entries: list[SkillEntry] = []
    stats: dict = {"stores": {}, "missing": [], "unreadable": 0}
    for store, root in resolve_stores(overrides).items():
        if not root.is_dir():
            stats["missing"].append(store)
            continue
        n = 0
        for p in _walk(root):
            e = read_entry(p, store, root)
            if e is None:
                stats["unreadable"] += 1
                continue
            entries.append(e)
            n += 1
        stats["stores"][store] = n
    return entries, stats


def find_collisions(entries: list[SkillEntry]) -> dict:
    """Same name in more than one store with different content hashes."""
    by_name: dict[str, list[SkillEntry]] = {}
    for e in entries:
        by_name.setdefault(e.name, []).append(e)
    colliding = {
        n: es
        for n, es in by_name.items()
        if len({e.store for e in es}) > 1 and len({e.version for e in es}) > 1
    }
    return {
        "names": len(colliding),
        "entries": sum(len(v) for v in colliding.values()),
        "examples": sorted(colliding)[:10],
    }


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def _lexical(query_tokens: list[str], e: SkillEntry) -> float:
    if not query_tokens:
        return 0.0
    name = set(_tokens(e.name))
    desc = _tokens(e.description)
    dset = set(desc)
    s = 0.0
    for t in query_tokens:
        if t in name:
            s += 3.0
        elif any(t in n for n in name):
            s += 1.5
        if t in dset:
            s += 1.0 + min(desc.count(t) - 1, 2) * 0.25
    return s / len(query_tokens)


def _ledger_join(entries: list[SkillEntry], ledger: Ledger | None) -> dict:
    if ledger is None:
        return {}
    scores = ledger.scores()
    exact = {(s.skill, s.version): s for s in scores}
    imp: dict[str, list[int]] = {}
    for s in scores:
        a = imp.setdefault(s.skill, [0, 0])
        a[0] += s.imported_served
        a[1] += s.imported_followed
    out = {}
    for e in entries:
        keys = [e.name] + ([f"{e.plugin}:{e.name}"] if e.plugin else [])
        sc = next((exact[(k, e.version)] for k in keys if (k, e.version) in exact), None)
        im = next((imp[k] for k in keys if k in imp), [0, 0])
        out[e.path] = (sc, im)
    return out


def rank(
    query: str,
    entries: list[SkillEntry],
    ledger: Ledger | None = None,
    limit: int = 10,
) -> list[SkillHit]:
    """Lexical match times a ledger multiplier. Anti-patterns sort last, flagged."""
    qt = _tokens(query)
    joined = _ledger_join(entries, ledger)
    hits: list[SkillHit] = []
    for e in entries:
        lex = _lexical(qt, e)
        if lex <= 0:
            continue
        sc, im = joined.get(e.path, (None, [0, 0]))
        h = SkillHit(
            entry=e, score=lex, lexical=lex, imported_served=im[0], imported_followed=im[1]
        )
        if sc is not None:
            # net in (-inf, inf) squashed to a multiplier in (0, 2); unscored stays 1.0
            h.ledger_net = sc.net
            h.follow_rate = sc.follow_rate
            h.served, h.followed = sc.served, sc.followed
            h.anti_pattern = sc.anti_pattern
            h.flag = sc.flag
            h.score = lex * (1.0 + sc.net / (abs(sc.net) + 5.0))
        hits.append(h)
    hits.sort(key=lambda h: (h.anti_pattern, -h.score, h.entry.name, h.entry.path))
    return hits[:limit]
