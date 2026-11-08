"""Importer for Claude Code harness memory directories.

A harness memory dir (``~/.claude/projects/<project>/memory/``) holds one-fact
markdown files with YAML frontmatter (``name``, ``description`` and a ``type`` of
user, feedback, project or reference, either top level or under ``metadata:``),
plus an index ``MEMORY.md`` and helper scripts. This importer turns each fact
file into one skmemory Memory so the harness dir can be retired.

Mapping:

* kind: feedback -> rule, project -> fact, reference -> fact, user -> preference.
  Any other type (infrastructure, incident, ...) or a file with no frontmatter
  maps to fact, and the raw type is kept as a tag and in metadata.
* layer: long-term for user and feedback (identity-level, permanent), mid-term
  for project and reference (they age). Unknown types go mid-term.
* source_tier user-stated. The audience is the store default (``@chef-only``);
  the importer never sets a wider one.
* title: the frontmatter ``name`` (plus the description when the name is a bare
  slug). The description is also kept as ``summary``. content is the body,
  ``[[links]]`` text untouched, linked slugs in ``metadata.harness_links``.
* tags: ``harness-memory``, the type, and ``index:<heading>`` for every
  MEMORY.md section that lists the file (links ``[x](slug.md)`` and bare
  ``slug`` tokens are both recognised).
* source and source_ref: the absolute file path.
* event_at: the first ISO date in the description or the first lines of the
  body, else the file mtime.

Overlaps. The same filename in more than one dir is one slug. Identical content
(same type, name, description and body) becomes ONE memory whose
``metadata.harness_paths`` lists every path. Diverged content becomes one
memory per version, chained with the store's own supersession: the newest by
mtime is the primary (open); each older version is written first and the next
one is written with ``supersedes_id``, so the older ones are closed, tagged
``superseded-variant`` and rank below the primary but stay recoverable.

Idempotent: a version is skipped when any of its paths is already a
``source_ref`` or listed in ``metadata.harness_paths``, or when its content
hash is already present. An edited file is therefore not re-imported; delete
the memory first to refresh it.

The write path switches off write-time triage for the run (an importer of a
source of truth must not silently drop a near-duplicate or spend LLM calls) and
lifts the decompose and overflow limits so one file stays one Memory.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..models import MemoryLayer

TAG = "harness-memory"
KIND_BY_TYPE = {"feedback": "rule", "project": "fact", "reference": "fact", "user": "preference"}
LAYER_BY_TYPE = {
    "user": MemoryLayer.LONG,
    "feedback": MemoryLayer.LONG,
    "project": MemoryLayer.MID,
    "reference": MemoryLayer.MID,
}
SKIP_NAMES = {"MEMORY.md": "index file", "check_index.py": "helper script"}
_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_DATE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_NO_LIMIT = 10**9


@dataclass
class Fact:
    """One parsed fact file."""

    path: str
    slug: str
    name: str
    description: str
    type: str
    body: str
    mtime: float
    sections: list[str] = field(default_factory=list)
    has_frontmatter: bool = True

    @property
    def sha(self) -> str:
        """Hash of everything that defines the fact (frontmatter noise excluded)."""
        blob = "\x00".join([self.type, self.name, self.description, self.body.strip()])
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def kind(self) -> str:
        return KIND_BY_TYPE.get(self.type, "fact")

    @property
    def layer(self) -> MemoryLayer:
        return LAYER_BY_TYPE.get(self.type, MemoryLayer.MID)

    @property
    def links(self) -> list[str]:
        return sorted({m.strip() for m in _LINK.findall(self.body)})

    @property
    def title(self) -> str:
        name = self.name or self.slug
        if self.description and not re.search(r"\s", name):
            return f"{name}: {self.description}"[:200]
        return name[:200]

    @property
    def event_at(self) -> str:
        head = self.description + "\n" + "\n".join(self.body.strip().splitlines()[:3])
        m = _DATE.search(head)
        if m:
            try:
                return (
                    datetime.strptime(m.group(1), "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                )
            except ValueError:
                pass
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).isoformat()


@dataclass
class Version:
    """One distinct content version of a slug, with every path that holds it."""

    sha: str
    facts: list[Fact]  # newest first

    @property
    def lead(self) -> Fact:
        return self.facts[0]

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.facts]


@dataclass
class Plan:
    """What an import would do, computed without touching any store."""

    slugs: dict[str, list[Version]] = field(default_factory=dict)  # versions newest first
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def versions(self) -> Iterator[tuple[str, int, Version]]:
        for slug, vs in self.slugs.items():
            for i, v in enumerate(vs):
                yield slug, i, v

    def report(self) -> dict[str, Any]:
        """Counts for the dry run."""
        by_type: Counter[str] = Counter()
        by_layer: Counter[str] = Counter()
        by_section: Counter[str] = Counter()
        identical = diverged = 0
        for vs in self.slugs.values():
            if len(vs) > 1:
                diverged += 1
            elif len(vs[0].facts) > 1:
                identical += 1
        for _, _, v in self.versions():
            f = v.lead
            by_type[f.type] += 1
            by_layer[f.layer.value] += 1
            for s in f.sections or ["(not in any index)"]:
                by_section[s] += 1
        files = sum(len(v.facts) for _, _, v in self.versions())
        return {
            "fact_files": files,
            "memories": sum(by_type.values()),
            "by_type": dict(by_type),
            "by_layer": dict(by_layer),
            "by_section": dict(by_section),
            "overlap_identical": identical,
            "overlap_diverged": diverged,
            "skipped": list(self.skipped),
            "no_frontmatter": sorted(
                f.path for _, _, v in self.versions() for f in v.facts if not f.has_frontmatter
            ),
            "unknown_type": sorted(
                {
                    f.path
                    for _, _, v in self.versions()
                    for f in v.facts
                    if f.type not in KIND_BY_TYPE
                }
            ),
        }


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """Split ``text`` into (frontmatter dict, body, had_frontmatter)."""
    m = _FM.match(text)
    if not m:
        return {}, text, False
    raw = m.group(1)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):  # tolerate unquoted colons: read simple key: value lines
        data = {}
        for line in raw.splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() and not line.startswith((" ", "\t")):
                data[k.strip()] = v.strip().strip("\"'")
    return data, text[m.end() :], True


def parse_index(directory: Path, stems: set[str]) -> dict[str, list[str]]:
    """Map slug -> index section headings that list it, from ``MEMORY.md``."""
    index = directory / "MEMORY.md"
    out: dict[str, list[str]] = {}
    if not index.is_file():
        return out
    section = ""
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        h = re.match(r"#{1,6}\s+(.*\S)", line)
        if h:
            section = re.sub(r"\s+", " ", h.group(1)).strip()
            continue
        if not section:
            continue
        for tok in _TOKEN.findall(line):
            stem = tok[:-3] if tok.endswith(".md") else tok
            if stem in stems and section not in out.setdefault(stem, []):
                out[stem].append(section)
    return out


def _read_dir(directory: Path, plan: Plan) -> list[Fact]:
    facts: list[Fact] = []
    files = sorted(directory.iterdir())
    stems = {p.stem for p in files if p.suffix == ".md" and p.name not in SKIP_NAMES}
    index = parse_index(directory, stems)
    for p in files:
        if p.name in SKIP_NAMES:
            plan.skipped.append((str(p), SKIP_NAMES[p.name]))
        elif p.is_dir():
            plan.skipped.append((str(p), "directory"))
        elif p.suffix != ".md":
            plan.skipped.append((str(p), "not markdown"))
        else:
            try:
                text = p.read_text(encoding="utf-8")
                mtime = p.stat().st_mtime
            except (OSError, UnicodeDecodeError) as exc:
                plan.skipped.append((str(p), f"unreadable: {type(exc).__name__}"))
                continue
            meta, body, had = parse_frontmatter(text)
            if not body.strip():
                plan.skipped.append((str(p), "empty body"))
                continue
            inner = meta.get("metadata")
            typ = meta.get("type") or (inner.get("type") if isinstance(inner, dict) else None)
            facts.append(
                Fact(
                    path=str(p.resolve()),
                    slug=p.stem,
                    name=str(meta.get("name") or "").strip(),
                    description=str(meta.get("description") or "").strip(),
                    type=str(typ or "unknown").strip().lower(),
                    body=body,
                    mtime=mtime,
                    sections=index.get(p.stem, []),
                    has_frontmatter=had,
                )
            )
    return facts


def build_plan(dirs: list[str | Path]) -> Plan:
    """Scan ``dirs`` and group the fact files into slugs and versions. Read only."""
    plan = Plan()
    by_slug: dict[str, list[Fact]] = {}
    for d in dirs:
        directory = Path(d).expanduser()
        if not directory.is_dir():
            raise FileNotFoundError(f"not a directory: {directory}")
        for fact in _read_dir(directory, plan):
            by_slug.setdefault(fact.slug, []).append(fact)
    for slug in sorted(by_slug):
        facts = sorted(by_slug[slug], key=lambda f: (-f.mtime, f.path))
        versions: dict[str, Version] = {}
        for f in facts:
            versions.setdefault(f.sha, Version(f.sha, [])).facts.append(f)
        ordered = sorted(versions.values(), key=lambda v: (-v.lead.mtime, v.lead.path))
        for v in ordered:  # union of index sections across identical copies
            merged: list[str] = []
            for f in v.facts:
                merged += [s for s in f.sections if s not in merged]
            v.lead.sections = merged
        plan.slugs[slug] = ordered
    return plan


def _tags(f: Fact, superseded: bool) -> list[str]:
    tags = [TAG, f.type] + [f"index:{s}" for s in f.sections]
    if superseded:
        tags.append("superseded-variant")
    return tags


@contextmanager
def _one_file_one_memory(store: Any) -> Iterator[None]:
    """Keep one file as one Memory and make the run deterministic."""
    saved = (
        getattr(store, "decompose_min_length", None),
        getattr(store, "max_content_length", None),
        os.environ.get("SKMEMORY_WRITE_TRIAGE"),
    )
    store.decompose_min_length = _NO_LIMIT
    store.max_content_length = _NO_LIMIT
    os.environ["SKMEMORY_WRITE_TRIAGE"] = "off"
    try:
        yield
    finally:
        store.decompose_min_length, store.max_content_length = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("SKMEMORY_WRITE_TRIAGE", None)
        else:
            os.environ["SKMEMORY_WRITE_TRIAGE"] = saved[2]


def _existing(store: Any) -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    shas: set[str] = set()
    for m in store.list_memories(tags=[TAG], limit=_NO_LIMIT):
        paths.add(m.source_ref)
        paths.update(m.metadata.get("harness_paths") or [])
        if m.metadata.get("harness_sha"):
            shas.add(m.metadata["harness_sha"])
    return paths, shas


def import_harness_memory(store: Any, dirs: list[str | Path]) -> dict[str, Any]:
    """Write the memories for ``dirs`` into ``store``. Returns counters."""
    plan = build_plan(dirs)
    known_paths, known_shas = _existing(store)
    stats = {"imported": 0, "skipped_existing": 0, "superseded_variants": 0}
    with _one_file_one_memory(store):
        for slug, versions in plan.slugs.items():
            previous_id: str | None = None
            for i in range(len(versions) - 1, -1, -1):  # oldest first, newest last
                v = versions[i]
                if v.sha in known_shas or any(p in known_paths for p in v.paths):
                    stats["skipped_existing"] += 1
                    continue
                f = v.lead
                superseded = i > 0
                mem = store.snapshot(
                    title=f.title,
                    content=f.body.strip(),
                    layer=f.layer,
                    tags=_tags(f, superseded),
                    source=f.path,
                    source_ref=f.path,
                    metadata={
                        "harness_slug": slug,
                        "harness_type": f.type,
                        "harness_paths": v.paths,
                        "harness_sha": v.sha,
                        "harness_links": f.links,
                        "harness_description": f.description,
                        "harness_sections": f.sections,
                    },
                    kind=f.kind,
                    source_tier="user-stated",
                    event_at=f.event_at,
                    supersedes_id=previous_id,
                )
                previous_id = mem.id
                stats["imported"] += 1
                stats["superseded_variants"] += int(superseded)
    return stats


def format_report(report: dict[str, Any]) -> str:
    """Human-readable dry-run report."""
    lines = [
        f"Fact files: {report['fact_files']}  Memories to write: {report['memories']}",
        "By type: " + ", ".join(f"{k}={v}" for k, v in sorted(report["by_type"].items())),
        "By layer: " + ", ".join(f"{k}={v}" for k, v in sorted(report["by_layer"].items())),
        f"Overlapping slugs: identical={report['overlap_identical']} "
        f"diverged={report['overlap_diverged']}",
        f"Files without frontmatter: {len(report['no_frontmatter'])}",
        f"Files with a type outside user/feedback/project/reference: {len(report['unknown_type'])}",
        "By index section:",
    ]
    lines += [
        f"  {n:4d}  {s}" for s, n in sorted(report["by_section"].items(), key=lambda x: -x[1])
    ]
    lines.append(f"Skipped ({len(report['skipped'])}):")
    lines += [f"  {why}: {path}" for path, why in report["skipped"]]
    return "\n".join(lines)
