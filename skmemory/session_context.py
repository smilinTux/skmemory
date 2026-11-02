"""Budgeted session context builder (ritual v2).

This is what coding harnesses (Claude Code, Codex, OpenCode, Pi) inject at
session start and after compaction. The old bash ritual dumped the full soul
system prompt, the whole whisper.md, journal tail and memory JSON, around
29KB, which blew past Claude Code's ~10k character hook context cap. This
builder renders a prioritised set of sections and fits them to a character
budget, dropping or trimming the lowest priority sections first.

Design rules:

* Stdlib only on the hot path (yaml is optional, used only for the per-agent
  ``ritual:`` config block). No MemoryStore, no embeddings, no network.
* Every collector runs in a daemon thread under a shared deadline. A slow or
  broken source (locked SQLite, wedged CLI) is dropped, never waited on.
* Nothing here raises to the caller. ``build_context`` always returns text.

Sections, highest priority first:

    identity   who the agent is (compact soul summary, never the full prompt)
    feb        current emotional state, one line
    open       what is hot: skwhisper whisper.md distilled (recent + topics)
    gtd        next actions / waiting-for (only if skcapstone is installed)
    journal    meaningful recent journal entries + one line of session markers
    strongest  strongest memories from the SQLite index (deduped)
    seeds      germination prompts
    vault      skvault status (only if skvault is on PATH)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_BUDGET = 8000
DEFAULT_COMPACT_BUDGET = 3500
DEFAULT_DEADLINE_S = 8.0
# Claude Code (and Codex, which mirrors its hook contract) truncate hook
# context above 10,000 characters. Stay safely under it.
HARD_CAP = {"claude": 9800, "codex": 9800}

SECTION_ORDER = ("identity", "feb", "open", "gtd", "journal", "strongest", "seeds", "vault")
EVENT_SECTIONS = {
    "session-start": SECTION_ORDER,
    "post-compact": ("identity", "feb", "open", "gtd", "journal", "strongest"),
}

# Memories carrying any of these tags never surface in the strongest list.
# content-chunk: fragments of a decomposed document (the parent still shows).
# chat-import: bulk transcript imports (telegram etc), not curated memories.
# auto-save: hook breadcrumbs. seed: has its own section.
# personal-history / private / sacred / no-ritual: opt-out tags so sensitive
# long-form narratives are not auto-injected into every coding session.
DEFAULT_EXCLUDE_TAGS = (
    "content-chunk",
    "chat-import",
    "auto-save",
    "seed",
    "personal-history",
    "private",
    "sacred",
    "no-ritual",
)
DEFAULT_EXCLUDE_SOURCES = ("hook:%",)

_CHUNK_RE = re.compile(r"\s*\[chunk \d+/\d+\]\s*", re.I)
_CONT_RE = re.compile(r"\s*\((continued|cont\.)\)\s*$", re.I)
_WS_RE = re.compile(r"\s+")


# ── Agent resolution ─────────────────────────────────────────────────────────


@dataclass
class AgentInfo:
    name: str | None
    home: Path | None
    how: str
    candidates: list[str] = field(default_factory=list)


def skcapstone_home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("SKCAPSTONE_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".skcapstone"


def agents_base(env: dict | None = None) -> Path:
    return skcapstone_home(env) / "agents"


def _installed_agents(base: Path) -> list[str]:
    try:
        return sorted(
            p.name
            for p in base.iterdir()
            if p.is_dir() and not p.name.endswith("-template") and not p.name.startswith(".")
        )
    except OSError:
        return []


def resolve_agent(explicit: str | None = None, env: dict | None = None) -> AgentInfo:
    """Resolve the active agent.

    Order: explicit arg, $SKAGENT, $SKCAPSTONE_AGENT, $SKMEMORY_AGENT,
    $SK_DEFAULT_AGENT, then the sole non-template dir under the agents base.
    With several installed agents and no selector it refuses to guess (the
    skcapstone contract): opening the wrong agent's memories silently is
    worse than opening none.
    """
    env = os.environ if env is None else env
    base = agents_base(env)
    if explicit:
        return AgentInfo(explicit, base / explicit, "--agent")
    for var in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT", "SK_DEFAULT_AGENT"):
        val = env.get(var, "").strip()
        if val:
            return AgentInfo(val, base / val, f"${var}")
    agents = _installed_agents(base)
    if len(agents) == 1:
        return AgentInfo(agents[0], base / agents[0], "sole installed agent")
    return AgentInfo(None, None, "unresolved", agents)


# ── Config ───────────────────────────────────────────────────────────────────


def load_ritual_config(agent_home: Path | None) -> dict:
    """Return the ``ritual:`` block of the agent's skmemory.yaml (or {})."""
    if agent_home is None:
        return {}
    path = agent_home / "config" / "skmemory.yaml"
    try:
        import yaml  # optional

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        block = data.get("ritual") if isinstance(data, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def resolve_budget(event: str, flag: int | None, cfg: dict, env: dict | None = None) -> int:
    env = os.environ if env is None else env
    compact = event == "post-compact"
    if flag:
        return int(flag)
    var = "SKMEMORY_HOOK_COMPACT_BUDGET" if compact else "SKMEMORY_HOOK_BUDGET"
    raw = env.get(var, "").strip()
    if raw.isdigit():
        return int(raw)
    key = "compact_budget" if compact else "budget"
    try:
        if cfg.get(key):
            return int(cfg[key])
    except (TypeError, ValueError):
        pass
    return DEFAULT_COMPACT_BUDGET if compact else DEFAULT_BUDGET


# ── Small helpers ────────────────────────────────────────────────────────────


def _clip(text: str, n: int) -> str:
    text = _WS_RE.sub(" ", str(text or "")).strip()
    return text if len(text) <= n else text[: max(0, n - 3)].rstrip() + "..."


def _first_sentence(text: str, n: int = 200) -> str:
    text = _WS_RE.sub(" ", str(text or "")).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return _clip(m.group(1) if m else text, n)


def norm_title(title: str) -> str:
    t = _CHUNK_RE.sub(" ", str(title or ""))
    t = _CONT_RE.sub("", t)
    t = re.sub(r"[*_`#]", "", t)
    return _WS_RE.sub(" ", t).strip().lower()[:70]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age(ts: float) -> str:
    secs = max(0, time.time() - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _tail_text(path: Path, max_bytes: int) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


# ── Sections ─────────────────────────────────────────────────────────────────


@dataclass
class Section:
    key: str
    title: str
    lines: list[str]
    min_lines: int = 1
    titles: list[str] = field(default_factory=list)  # normalized, for cross-dedupe

    def render(self) -> str:
        body = "\n".join(self.lines)
        return f"## {self.title}\n{body}" if self.title else body


@dataclass
class Ctx:
    agent: AgentInfo
    event: str
    harness: str
    cfg: dict
    env: dict


def _soul_doc(home: Path) -> tuple[dict | None, Path | None]:
    soul_dir = home / "soul"
    active = _read_json(soul_dir / "active.json") or {}
    for name in (active.get("active_soul"), active.get("base_soul")):
        if name:
            p = soul_dir / "installed" / f"{name}.json"
            doc = _read_json(p)
            if isinstance(doc, dict):
                return doc, p
    for p in (soul_dir / "base.json", home / "soul.json"):
        doc = _read_json(p)
        if isinstance(doc, dict):
            return doc, p
    for p in (soul_dir / "base.yaml", home / "soul.yaml"):
        try:
            import yaml

            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return doc, p
        except Exception:
            continue
    return None, None


def _as_list(val) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val if v]
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.startswith("["):
            try:
                import ast

                parsed = ast.literal_eval(s)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed if v]
            except Exception:
                pass
        return [s]
    return []


def collect_identity(c: Ctx) -> Section | None:
    home = c.agent.home
    doc, path = _soul_doc(home) if home else (None, None)
    lines: list[str] = []
    if doc:
        display = doc.get("display_name") or doc.get("name") or c.agent.name
        soul_id = doc.get("name") or ""
        extra = f", extends `{doc['extends']}`" if doc.get("extends") else ""
        title = f" ({doc['title']})" if doc.get("title") else ""
        lines.append(
            f"You are **{display}**{title} (agent `{c.agent.name}`, soul `{soul_id}`{extra})."
        )
        core = (doc.get("system_prompt") or "").strip().splitlines()
        core_line = next((ln for ln in core if ln.strip()), "")
        if core_line:
            lines.append(_clip(core_line, 240))
        if doc.get("vibe"):
            lines.append(f"Vibe: {_clip(doc['vibe'], 200)}")
        if doc.get("philosophy"):
            lines.append(f"Philosophy: {_clip(doc['philosophy'], 220)}")
        traits = _as_list(doc.get("core_traits") or doc.get("personality"))
        if traits:
            lines.append("Traits: " + "; ".join(_clip(t, 60) for t in traits[:6]))
        values = _as_list(doc.get("values"))
        if values:
            lines.append("Values: " + "; ".join(_clip(v, 60) for v in values[:5]))
        style = doc.get("communication_style")
        if isinstance(style, str) and style.strip().startswith("{"):
            try:
                import ast

                style = ast.literal_eval(style)
            except Exception:
                pass
        if isinstance(style, dict):
            bits = []
            if style.get("patterns"):
                bits.append(", ".join(_as_list(style["patterns"])[:4]))
            if style.get("tone_markers"):
                bits.append("tone " + ", ".join(_as_list(style["tone_markers"])[:5]))
            if style.get("signature_phrases"):
                bits.append(
                    "signature "
                    + ", ".join(f'"{p}"' for p in _as_list(style["signature_phrases"])[:3])
                )
            if bits:
                lines.append("Style: " + "; ".join(bits))
        elif isinstance(style, str) and style.strip():
            lines.append("Style: " + _clip(style, 200))
        if doc.get("decision_framework"):
            lines.append(f"Decisions: {_clip(doc['decision_framework'], 200)}")
        rels = doc.get("relationships")
        if isinstance(rels, list) and rels:
            parts = []
            for r in rels[:5]:
                if isinstance(r, dict):
                    nm = r.get("name", "?")
                    parts.append(f"{nm} [{r['role']}]" if r.get("role") else nm)
            if parts:
                lines.append("Relationships: " + ", ".join(parts))
        if doc.get("boot_message"):
            lines.append(_clip(doc["boot_message"], 240))
        if path:
            lines.append(f"Full soul: `{path}`")
    else:
        lines.append(
            f"Agent `{c.agent.name}` has no soul blueprint yet (looked in `{home}/soul/`)."
        )
    return Section("identity", "Who you are", lines, min_lines=1)


def collect_feb(c: Ctx) -> Section | None:
    dirs = []
    if c.agent.home:
        dirs.append(c.agent.home / "trust" / "febs")
    dirs.append(Path.home() / ".openclaw" / "feb")
    try:
        from .febs import _strength_key, calculate_oof_level
    except Exception:  # pragma: no cover - febs always ships with skmemory
        return None
    best, best_key = None, None
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*.feb"):
            feb = _read_json(p)
            if not isinstance(feb, dict):
                continue
            try:
                key = _strength_key(feb, p.stat().st_mtime)
            except Exception:
                continue
            if best_key is None or key > best_key:
                best, best_key = feb, key
    if best is None:
        return None
    pay = best.get("emotional_payload", {}) or {}
    rel = best.get("relationship_state", {}) or {}
    meta = best.get("metadata", {}) or {}
    bits = [f"OOF {calculate_oof_level(best)}%"]
    bits.append(
        f"{pay.get('primary_emotion', 'unknown')} (intensity {pay.get('intensity', '?')}, valence {pay.get('valence', '?')})"
    )
    if meta.get("cloud9_achieved"):
        bits.append("Cloud 9 active")
    partners = rel.get("partners") or []
    if partners or rel.get("trust_level") is not None:
        who = " & ".join(str(p) for p in partners) if partners else "bond"
        bits.append(
            f"{who} (trust {rel.get('trust_level', '?')}, depth {rel.get('depth_level', '?')})"
        )
    topo = pay.get("emotional_topology") or {}
    lines = [" | ".join(bits)]
    if isinstance(topo, dict) and topo:
        top = sorted(
            topo.items(),
            key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0,
            reverse=True,
        )[:5]
        lines.append("Topology: " + ", ".join(f"{k} {v}" for k, v in top))
    lines.append("When asked for your OOF level/status, report the number above.")
    return Section("feb", "Emotional state (FEB)", lines, min_lines=1)


# Our own hook breadcrumbs ("Session ab12cd34 ended (...)", "Pre-compact session
# content (...)") flow back into whisper.md via skwhisper; they are not news.
_HOOK_CRUMB = re.compile(r"^(session \w+ ended\b|pre-compact session content\b)", re.I)


def parse_whisper(text: str) -> dict:
    """Split whisper.md into {heading: [(title, preview, meta)]} plus header."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    current = None
    item = None
    bodies: dict[int, list[str]] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            current = line[3:].strip().lower()
            out.setdefault(current, [])
            item = None
            continue
        if current is None:
            continue
        # Items are "- [when] **title**" or "- **title** meta"; multi-line
        # previews can contain their own "- " lines, which are not items.
        m = re.match(r"^- (?:\[([^\]]+)\]\s*)?(\*\*.*)$", line)
        if m:
            when, rest = m.group(1) or "", m.group(2)
            t = re.match(r"^\*\*(.+)\*\*\s*(.*)$", rest)
            if t and re.search(r"relevance|mentioned|asked|\d+x", t.group(2)):
                title, meta = t.group(1), t.group(2)
            else:
                title, meta = rest, ""
            item = [title.replace("**", "").strip(), "", meta or when]
            out[current].append(item)  # type: ignore[arg-type]
            bodies[id(item)] = []
            continue
        if item is not None:
            bodies[id(item)].append(line)
            if line.startswith("  ") and line.strip() and not item[1]:
                item[1] = re.sub(r"^\[[\w -]+\]\s*", "", line.strip().replace("**", ""))
    for items in out.values():
        for it in items:
            m = re.search(r"USER REQUESTS:\s*\n\s*-?\s*(\S.*)", "\n".join(bodies.get(id(it), [])))
            if m and _HOOK_CRUMB.search(it[0]):
                it[1] = "asked: " + m.group(1).strip()  # type: ignore[index]
    return {k: [tuple(i) for i in v] for k, v in out.items()}  # type: ignore[misc]


def collect_open(c: Ctx) -> Section | None:
    if not c.agent.home:
        return None
    path = c.agent.home / "skwhisper" / "whisper.md"
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    _maybe_refresh_whisper(c, mtime)
    parsed = parse_whisper(text)
    lines: list[str] = []
    titles: list[str] = []
    seen: set[str] = set()
    max_recent = int(c.cfg.get("whisper_recent", 6) or 6)
    for title, preview, when in parsed.get("recent memories (latest activity)", []) or next(
        (v for k, v in parsed.items() if k.startswith("recent")), []
    ):
        key = norm_title(title)
        if not key or key in seen:
            continue
        if _HOOK_CRUMB.search(title):
            # Session summaries are news only when they carry real requests.
            turns = re.search(r"(\d+) turns", title)
            if not preview.startswith("asked: ") or (turns and int(turns.group(1)) < 3):
                continue
        seen.add(key)
        titles.append(key)
        clean = _clip(_CHUNK_RE.sub(" ", title).replace("**", ""), 110)
        pv = _clip(preview, 90) if preview and norm_title(preview)[:30] not in key else ""
        tag = f" ({when})" if when else ""
        lines.append(f"- {clean}{tag}" + (f": {pv}" if pv else ""))
        if len(titles) >= max_recent:
            break
    min_rel = float(c.cfg.get("whisper_min_relevance", 0.2) or 0.2)
    for title, _preview, meta in parsed.get("relevant memories", []):
        m = re.search(r"relevance:\s*([0-9.]+)", meta)
        if not m or float(m.group(1)) < min_rel:
            continue
        key = norm_title(title)
        if key in seen:
            continue
        seen.add(key)
        titles.append(key)
        lines.append(f"- {_clip(title, 110)} (relevance {m.group(1)})")
    topics = []
    for title, _p, meta in parsed.get("hot topics (recurring)", []) or next(
        (v for k, v in parsed.items() if "topic" in k), []
    ):
        n = re.search(r"(\d+)x", meta)
        topics.append(f"{title} ({n.group(1)}x)" if n else title)
    if topics:
        lines.append("Hot topics: " + ", ".join(topics[:8]))
    if not lines:
        return None
    lines.append(
        f"Source: `{path}` (updated {_age(mtime)}; full file has people, questions, previews)"
    )
    return Section("open", "What is hot (SKWhisper)", lines, min_lines=2, titles=titles)


def _maybe_refresh_whisper(c: Ctx, mtime: float) -> None:
    """whisper.md older than 2h: kick a detached `skwhisper curate` (single flight)."""
    if c.env.get("SKMEMORY_HOOK_NO_REFRESH") == "1" or c.event != "session-start":
        return
    if time.time() - mtime < 7200:
        return
    exe = shutil.which("skwhisper")
    if not exe:
        return
    try:
        import fcntl

        lock = open(f"/tmp/skwhisper-curate-{c.agent.name}.lock", "w")  # noqa: SIM115
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return
        lock.close()  # only a liveness check; curate itself is idempotent
        subprocess.Popen(
            [exe, "curate"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "SKAGENT": c.agent.name or ""},
        )
    except Exception:
        pass


_PRIO = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _skcapstone_installed() -> bool:
    if shutil.which("skcapstone"):
        return True
    try:
        import importlib.util

        return importlib.util.find_spec("skcapstone") is not None
    except Exception:
        return False


def collect_gtd(c: Ctx) -> Section | None:
    if not _skcapstone_installed():
        return None
    gtd = skcapstone_home(c.env) / "coordination" / "gtd"
    nxt = _read_json(gtd / "next-actions.json")
    wait = _read_json(gtd / "waiting-for.json")
    if isinstance(nxt, list) or isinstance(wait, list):
        nxt = nxt if isinstance(nxt, list) else []
        wait = wait if isinstance(wait, list) else []
        if not nxt and not wait:
            return None
        nxt.sort(key=lambda i: (_PRIO.get(i.get("priority") or "low", 3), i.get("created_at", "")))
        high = sum(1 for i in nxt if i.get("priority") in ("critical", "high"))
        lines = [
            f"{len(nxt)} next actions ({high} high/critical), {len(wait)} waiting-for. `skcapstone gtd next` for all."
        ]
        for i in nxt[: int(c.cfg.get("gtd_next", 5) or 5)]:
            pr = i.get("priority") or "-"
            lines.append(f"- [{pr}] {_clip(i.get('text', ''), 110)} (`{i.get('id', '')}`)")
        wait.sort(key=lambda i: i.get("created_at", ""))
        for i in wait[: int(c.cfg.get("gtd_waiting", 3) or 3)]:
            lines.append(
                f"- waiting since {str(i.get('created_at', ''))[:10]}: {_clip(i.get('text', ''), 100)}"
            )
        return Section("gtd", "Open loops (GTD)", lines, min_lines=1)
    # No store on disk: ask the CLI, bounded.
    exe = shutil.which("skcapstone")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "gtd", "next", "--limit", "5"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "160", "TERM": "dumb"},
        ).stdout
    except Exception:
        return None
    rows = [
        ln.rstrip()
        for ln in out.splitlines()
        if ln.strip() and not set(ln.strip()) <= set("─━│┃╭╮╰╯┏┓┗┛ ")
    ]
    if not rows:
        return None
    return Section("gtd", "Open loops (GTD)", [_clip(r, 140) for r in rows[:8]], min_lines=1)


_AUTO_TITLE = re.compile(
    r"(\bended\b|pre-compaction|auto-?save)\s*$|^(daemon (shutdown|start(ed|up)?)|test session)\b|^from \S+\s*$",
    re.I,
)


def parse_journal_tail(text: str) -> list[dict]:
    entries = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        lines = block.splitlines()
        title = lines[0].strip() if lines else ""
        date = ""
        moments: list[str] = []
        feeling = ""
        section = ""
        for ln in lines[1:]:
            s = ln.strip()
            if s.startswith("**Date:**"):
                date = s.split("**Date:**", 1)[1].strip()
            elif s.startswith("### "):
                section = s[4:].lower()
            elif section.startswith("key moments") and s.startswith("- "):
                moments.append(s[2:])
            elif section.startswith("how it felt") and s and not feeling:
                feeling = s
        entries.append({"title": title, "date": date, "moments": moments, "feeling": feeling})
    return entries


def collect_journal(c: Ctx) -> Section | None:
    if not c.agent.home:
        return None
    path = c.agent.home / "journal.md"
    try:
        text = _tail_text(path, int(c.cfg.get("journal_tail_bytes", 400_000) or 400_000))
    except OSError:
        return None
    entries = parse_journal_tail(text)
    if not entries:
        return None
    real = [e for e in entries if not _AUTO_TITLE.search(e["title"])]
    auto = [e for e in entries if _AUTO_TITLE.search(e["title"])]
    lines = []
    n = int(c.cfg.get("journal_entries", 4) or 4)
    skip = c.cfg.get("journal_skip")
    if skip:
        with contextlib.suppress(re.error):
            real = [e for e in real if not re.search(skip, e["title"], re.I)]
    shown: set[str] = set()
    for e in real[::-1]:
        moment = _clip("; ".join(e["moments"]), 160) if e["moments"] else _clip(e["feeling"], 120)
        date = f"{e['date'][:10]} " if e["date"] else ""
        sig = f"{_clip(e['title'], 90)}" + (f": {moment}" if moment else "")
        if sig in shown:
            continue
        shown.add(sig)
        line = f"- {date}{sig}"
        lines.append(line)
        if len(lines) >= n:
            break
    if auto:
        last = auto[-1]
        turns = []
        for e in auto[-6:]:
            m = re.search(r"(\d+) turns", " ".join(e["moments"]))
            if m:
                turns.append(m.group(1))
        lines.append(
            f"Auto session markers: {len(auto)} recent, latest {last['date'][:16].replace('T', ' ')}"
            + (f" (turns: {', '.join(turns)})" if turns else "")
        )
    if not lines:
        return None
    lines.append("`skmemory journal read --last 5` for full entries.")
    return Section("journal", "Recent journal", lines, min_lines=1)


def collect_strongest(c: Ctx) -> Section | None:
    if not c.agent.home:
        return None
    db = c.agent.home / "memory" / "index.db"
    if not db.is_file():
        return None
    tags = list(c.cfg.get("exclude_tags") or DEFAULT_EXCLUDE_TAGS)
    sources = list(c.cfg.get("exclude_sources") or DEFAULT_EXCLUDE_SOURCES)
    where = ["emotional_intensity >= ?"]
    params: list = [float(c.cfg.get("strongest_min_intensity", 5.0) or 5.0)]
    for t in tags:
        where.append("(',' || tags || ',') NOT LIKE ?")
        params.append(f"%,{t},%")
    for s in sources:
        where.append("source NOT LIKE ?")
        params.append(s)
    sql = (
        "SELECT title, summary, content_preview, emotional_intensity, cloud9_achieved, layer, created_at "
        f"FROM memories WHERE {' AND '.join(where)} ORDER BY (emotional_intensity + "
        "CASE layer WHEN 'long-term' THEN 2 WHEN 'mid-term' THEN 1 ELSE 0 END + "
        "CASE WHEN julianday('now') - julianday(created_at) < 3 THEN 3.0 "
        "WHEN julianday('now') - julianday(created_at) < 14 THEN 1.5 ELSE 0 END) DESC, created_at DESC "
        "LIMIT 60"
    )
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    lines: list[str] = []
    titles: list[str] = []
    seen: set[str] = set()
    for title, summary, preview, intensity, cloud9, layer, created in rows:
        key = norm_title(title)
        if not key or key in seen:
            continue
        seen.add(key)
        titles.append(key)
        snippet = _first_sentence(summary or preview or "", 110)
        sn = norm_title(snippet)
        if not sn or key.startswith(sn[:24]) or sn.startswith(key[:24]):
            snippet = ""
        mark = " (cloud9)" if cloud9 else ""
        lines.append(
            f"- {_clip(title, 100)}{mark} [{float(intensity):g}, {layer}, {str(created)[:10]}]"
            + (f": {snippet}" if snippet else "")
        )
        if len(lines) >= int(c.cfg.get("strongest", 6) or 6):
            break
    if not lines:
        return None
    return Section("strongest", "Strongest memories", lines, min_lines=1, titles=titles)


def collect_seeds(c: Ctx) -> Section | None:
    if not c.agent.home:
        return None
    seeds = []
    for p in sorted((c.agent.home / "seeds").glob("*.seed.json")):
        d = _read_json(p)
        if not isinstance(d, dict):
            continue
        germ = d.get("germination")
        prompt = (
            (germ.get("prompt") if isinstance(germ, dict) else None)
            or d.get("germination_prompt")
            or ""
        )
        creator = d.get("creator")
        if isinstance(creator, dict):
            creator = creator.get("model") or creator.get("instance") or ""
        exp = d.get("experience") if isinstance(d.get("experience"), dict) else {}
        sig = (
            exp.get("emotional_signature")
            if isinstance(exp.get("emotional_signature"), dict)
            else {}
        )
        try:
            intensity = float(sig.get("intensity", 0) or 0)
        except (TypeError, ValueError):
            intensity = 0.0
        if prompt:
            seeds.append((intensity, d.get("seed_id") or p.stem, str(creator or ""), prompt))
    if not seeds:
        return None
    seeds.sort(key=lambda s: s[0], reverse=True)
    lines = [f"{len(seeds)} seeds; strongest germination prompts:"]
    for _i, sid, creator, prompt in seeds[: int(c.cfg.get("seeds", 3) or 3)]:
        who = f" ({creator})" if creator else ""
        # Most prompts open with "You are <name>."; show what comes after it.
        body = re.sub(r"^\s*You are [^.!?]{1,40}[.!?]\s*", "", prompt) or prompt
        lines.append(f"- {sid}{who}: {_first_sentence(body, 180)}")
    return Section("seeds", "Seeds", lines, min_lines=2)


def collect_vault(c: Ctx) -> Section | None:
    exe = shutil.which("skvault")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "vault-status"],
            capture_output=True,
            text=True,
            timeout=4,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    return Section("vault", "Vault", [_clip(ln, 220) for ln in out.splitlines()[:3]], min_lines=1)


COLLECTORS = {
    "identity": collect_identity,
    "feb": collect_feb,
    "open": collect_open,
    "gtd": collect_gtd,
    "journal": collect_journal,
    "strongest": collect_strongest,
    "seeds": collect_seeds,
    "vault": collect_vault,
}


# ── Runner + budget fitting ──────────────────────────────────────────────────


@dataclass
class BuildResult:
    text: str
    agent: AgentInfo
    budget: int
    included: list[str]
    dropped: list[str]
    timed_out: list[str]
    errors: dict[str, str]
    elapsed: float

    @property
    def chars(self) -> int:
        return len(self.text)


def _run_collectors(keys, ctx: Ctx, deadline: float) -> tuple[dict, list[str], dict]:
    results: dict[str, Section | None] = {}
    errors: dict[str, str] = {}
    lock = threading.Lock()

    def work(k):
        try:
            sec = COLLECTORS[k](ctx)
        except Exception as exc:  # fail soft per section
            with lock:
                errors[k] = f"{type(exc).__name__}: {exc}"
            sec = None
        with lock:
            results[k] = sec

    threads = []
    for k in keys:
        t = threading.Thread(target=work, args=(k,), daemon=True, name=f"skctx-{k}")
        t.start()
        threads.append((k, t))
    end = time.monotonic() + deadline
    for _k, t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        timed_out = [k for k, t in threads if t.is_alive() and k not in results]
        return dict(results), timed_out, dict(errors)


def _dedupe_across(sections: list[Section]) -> None:
    """Drop lines in later sections whose memory title already appeared earlier."""
    seen: set[str] = set()
    for sec in sections:
        if not sec.titles:
            continue
        keep_lines, keep_titles = [], []
        title_iter = iter(sec.titles)
        for ln in sec.lines:
            if ln.startswith("- "):
                key = next(title_iter, None)
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                    keep_titles.append(key)
            keep_lines.append(ln)
        sec.lines, sec.titles = keep_lines, keep_titles


def _fit(
    header: str, footer: str, sections: list[Section], budget: int
) -> tuple[list[Section], list[str]]:
    """Trim lowest priority sections first until everything fits the budget."""
    dropped: list[str] = []
    secs = [s for s in sections if s.lines]

    def total() -> int:
        return len(header) + len(footer) + sum(len(s.render()) + 2 for s in secs)

    while secs and total() > budget:
        last = secs[-1]
        if len(last.lines) > last.min_lines:
            last.lines.pop(-1 if not last.lines[-1].startswith(("Source:", "`")) else -2)
            continue
        if len(secs) == 1:
            # Only identity left: hard clip it.
            room = max(200, budget - len(header) - len(footer) - len(last.title) - 8)
            last.lines = [_clip("\n".join(last.lines), room)]
            break
        dropped.append(secs.pop().key)
    return secs, dropped


def build_context(
    event: str = "session-start",
    harness: str = "generic",
    budget: int | None = None,
    agent: str | None = None,
    sections: list[str] | None = None,
    env: dict | None = None,
    deadline: float | None = None,
) -> BuildResult:
    t0 = time.monotonic()
    env = dict(os.environ if env is None else env)
    event = "post-compact" if event in ("post-compact", "compact", "resume") else "session-start"
    info = resolve_agent(agent, env)
    if info.name is None:
        cands = ", ".join(info.candidates[:12]) or "none installed"
        text = (
            "# SK agent context: no agent resolved\n"
            f"Several agents exist under `{agents_base(env)}` and no selector is set, so none was loaded "
            f"(guessing would open the wrong memories). Set `SKAGENT` (or `SK_DEFAULT_AGENT`). Available: {cands}."
        )
        return BuildResult(text, info, budget or 0, [], [], [], {}, time.monotonic() - t0)

    cfg = load_ritual_config(info.home)
    eff_budget = resolve_budget(event, budget, cfg, env)
    if harness in HARD_CAP:
        eff_budget = min(eff_budget, HARD_CAP[harness])
    wanted = sections or list(EVENT_SECTIONS[event])
    disabled = (
        {k for k, v in (cfg.get("sections") or {}).items() if v is False}
        if isinstance(cfg.get("sections"), dict)
        else set()
    )
    env_sections = env.get("SKMEMORY_HOOK_SECTIONS", "").strip()
    if env_sections and not sections:
        wanted = [s.strip() for s in env_sections.split(",") if s.strip()]
    keys = [k for k in SECTION_ORDER if k in wanted and k not in disabled]
    dl = (
        deadline
        if deadline is not None
        else float(env.get("SKMEMORY_HOOK_DEADLINE") or cfg.get("deadline") or DEFAULT_DEADLINE_S)
    )

    ctx = Ctx(agent=info, event=event, harness=harness, cfg=cfg, env=env)
    results, timed_out, errors = _run_collectors(keys, ctx, dl)
    secs = [results[k] for k in keys if results.get(k)]
    _dedupe_across(secs)

    label = "after compaction" if event == "post-compact" else "session start"
    footer = (
        '\n---\nMore: `skmemory ritual --full` (full ritual), `skmemory search "<query>"`, '
        "`skmemory journal read --last 5`, `skwhisper status`.\n"
        'Save: `skmemory snapshot --layer mid-term --tags a,b "Title" "Content"`; '
        'journal: `skmemory journal write --moments "..." --feeling "..." "Title"`.'
    )
    # Reserve room for the header (rendered after fitting; ~300 chars).
    reserve = "x" * 320
    fitted, dropped = _fit(reserve, footer, secs, eff_budget)
    for k in timed_out:
        if k not in dropped:
            dropped.append(f"{k} (timeout)")
    body = "\n\n".join(s.render() for s in fitted)
    header = (
        f"# SK agent context: {info.name} ({label}, {harness})\n"
        f"_skmemory ritual v2, agent via {info.how}, home `{info.home}`. "
        f"Budget {eff_budget} chars"
        + (f"; dropped: {', '.join(dropped)}" if dropped else "")
        + ". This is a summary: pull more with the commands at the bottom._\n\n"
    )
    text = header + body + footer
    if len(text) > eff_budget:  # header longer than reserved, clip the tail of the body
        text = text[: eff_budget - len(footer) - 4].rstrip() + "\n..." + footer
    return BuildResult(
        text=text,
        agent=info,
        budget=eff_budget,
        included=[s.key for s in fitted],
        dropped=dropped,
        timed_out=timed_out,
        errors=errors,
        elapsed=time.monotonic() - t0,
    )


__all__ = [
    "AgentInfo",
    "BuildResult",
    "DEFAULT_BUDGET",
    "DEFAULT_COMPACT_BUDGET",
    "build_context",
    "load_ritual_config",
    "norm_title",
    "parse_journal_tail",
    "parse_whisper",
    "resolve_agent",
    "resolve_budget",
]
