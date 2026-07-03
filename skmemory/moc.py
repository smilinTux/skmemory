"""
Maps of Content (MOC) — auto-generated index documents for memories.

A "Map of Content" (a term borrowed from the Zettelkasten / PKM world) is a
curated index page that links related notes together so you can navigate a
knowledge base by theme instead of by search. This module builds MOC indexes
over a collection of :class:`~skmemory.models.Memory` objects, grouping them
two ways:

    * **by quadrant** — Core / Work / Soul / Wild (see :mod:`skmemory.quadrants`)
    * **by tag cluster** — one MOC per tag that appears on enough memories

Everything here is **read-side aggregation**: it never mutates or writes back
to the store. Given a list of memories, it produces plain structured objects
(:class:`MOCIndex`) that can be rendered to Markdown and optionally written to
files.

Design goals:
    * Deterministic output — same input always yields byte-identical Markdown
      (stable sort keys, no wall-clock in the body unless a timestamp is passed).
    * Bounded work — caps on entries-per-section and number of tag clusters so
      a huge store can't blow up the index.
    * Zero side effects on memories.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .models import Memory
from .quadrants import Quadrant, classify_memory

# Bounds — keep MOC generation cheap even on a large store.
DEFAULT_MAX_ENTRIES_PER_SECTION = 50
DEFAULT_MAX_CLUSTERS = 40
DEFAULT_MIN_CLUSTER_SIZE = 2
_SUMMARY_MAX_CHARS = 200

# Quadrant presentation metadata (label + one-line description).
_QUADRANT_META: dict[Quadrant, tuple[str, str]] = {
    Quadrant.CORE: ("Core", "Identity, relationships, who you are"),
    Quadrant.WORK: ("Work", "Tasks, code, debugging, technical stuff"),
    Quadrant.SOUL: ("Soul", "Emotions, feelings, love, connection"),
    Quadrant.WILD: ("Wild", "Chaos, creativity, unexpected ideas, humor"),
}


class MOCLink(BaseModel):
    """A single entry in a MOC section — a link to one memory."""

    memory_id: str = Field(description="ID of the linked memory")
    title: str = Field(description="Memory title (link text)")
    summary: str = Field(default="", description="Short one-line gloss")
    layer: str = Field(default="", description="Memory layer (short/mid/long-term)")
    tags: list[str] = Field(default_factory=list, description="Memory tags")


class MOCSection(BaseModel):
    """A named group of links within a MOC index."""

    name: str = Field(description="Section heading")
    description: str = Field(default="", description="One-line section blurb")
    links: list[MOCLink] = Field(default_factory=list)
    total: int = Field(
        default=0,
        description="Total members before entries were capped (>= len(links))",
    )

    @property
    def truncated(self) -> int:
        """How many members were dropped by the per-section cap."""
        return max(0, self.total - len(self.links))


class MOCIndex(BaseModel):
    """A complete Map of Content — a title plus one or more sections."""

    key: str = Field(description="Stable slug identifying this MOC")
    title: str = Field(description="Human-readable MOC title")
    description: str = Field(default="", description="What this MOC indexes")
    kind: str = Field(default="", description="'quadrant' or 'tag-cluster'")
    sections: list[MOCSection] = Field(default_factory=list)
    generated_at: str = Field(
        default="",
        description="ISO timestamp; empty keeps output deterministic",
    )

    @property
    def total_links(self) -> int:
        """Total number of linked memories across all sections."""
        return sum(len(s.links) for s in self.sections)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _short_summary(memory: Memory) -> str:
    """Best available one-line gloss for a memory, trimmed."""
    text = (memory.summary or memory.content or "").strip().replace("\n", " ")
    if len(text) > _SUMMARY_MAX_CHARS:
        text = text[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


def _memory_sort_key(memory: Memory) -> tuple:
    """Deterministic ordering: newest first, then title, then id."""
    # created_at is an ISO string; reverse-lexicographic == newest first.
    return (memory.created_at or "", memory.title or "", memory.id)


def _to_link(memory: Memory) -> MOCLink:
    return MOCLink(
        memory_id=memory.id,
        title=memory.title,
        summary=_short_summary(memory),
        layer=getattr(memory.layer, "value", str(memory.layer)),
        tags=list(memory.tags),
    )


def _sorted_links(memories: list[Memory], cap: int) -> tuple[list[MOCLink], int]:
    """Sort memories deterministically, cap them, return (links, total)."""
    ordered = sorted(memories, key=_memory_sort_key, reverse=True)
    total = len(ordered)
    capped = ordered[: max(0, cap)] if cap else ordered
    return [_to_link(m) for m in capped], total


def _slugify(value: str) -> str:
    """Filesystem-safe lowercase slug."""
    out = []
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {" ", "-", "_", ":", "/"}:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "untitled"


# ─────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────


def build_quadrant_moc(
    memories: list[Memory],
    *,
    max_entries_per_section: int = DEFAULT_MAX_ENTRIES_PER_SECTION,
    include_empty: bool = False,
    timestamp: bool = False,
) -> MOCIndex:
    """Build a single MOC indexing all memories, one section per quadrant.

    Args:
        memories: Memories to index (not mutated).
        max_entries_per_section: Cap on links shown per quadrant.
        include_empty: If True, keep quadrants with no members as empty sections.
        timestamp: If True, stamp ``generated_at`` (breaks determinism).

    Returns:
        MOCIndex: One MOC with up to four quadrant sections.
    """
    buckets: dict[Quadrant, list[Memory]] = {q: [] for q in Quadrant}
    for mem in memories:
        buckets[classify_memory(mem)].append(mem)

    sections: list[MOCSection] = []
    for quadrant in Quadrant:  # enum order = CORE, WORK, SOUL, WILD (stable)
        members = buckets[quadrant]
        if not members and not include_empty:
            continue
        label, desc = _QUADRANT_META[quadrant]
        links, total = _sorted_links(members, max_entries_per_section)
        sections.append(
            MOCSection(name=label, description=desc, links=links, total=total)
        )

    return MOCIndex(
        key="quadrants",
        title="Maps of Content — Quadrants",
        description="Memories grouped by quadrant: Core, Work, Soul, Wild.",
        kind="quadrant",
        sections=sections,
        generated_at=(datetime.now(timezone.utc).isoformat() if timestamp else ""),
    )


def _extract_cluster_tags(memory: Memory, *, include_quadrant_tags: bool) -> list[str]:
    """Tags to cluster on for one memory (normalized, filtered)."""
    tags = []
    for raw in memory.tags:
        tag = raw.strip().lower()
        if not tag:
            continue
        if not include_quadrant_tags and tag.startswith("quadrant:"):
            continue
        tags.append(tag)
    return tags


def build_tag_cluster_mocs(
    memories: list[Memory],
    *,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    max_entries_per_section: int = DEFAULT_MAX_ENTRIES_PER_SECTION,
    include_quadrant_tags: bool = False,
    timestamp: bool = False,
) -> list[MOCIndex]:
    """Build one MOC per tag cluster (a tag shared by >= min_cluster_size memories).

    Each returned MOC has a single section listing the memories that carry the
    tag. Clusters are ranked by size (largest first), then tag name, and capped
    at ``max_clusters``.

    Args:
        memories: Memories to index (not mutated).
        min_cluster_size: Minimum members for a tag to become a MOC.
        max_clusters: Maximum number of tag MOCs returned.
        max_entries_per_section: Cap on links per cluster.
        include_quadrant_tags: If True, also cluster on ``quadrant:*`` tags.
        timestamp: If True, stamp ``generated_at`` (breaks determinism).

    Returns:
        list[MOCIndex]: Deterministically ordered tag-cluster MOCs.
    """
    tag_to_memories: dict[str, list[Memory]] = {}
    for mem in memories:
        for tag in _extract_cluster_tags(mem, include_quadrant_tags=include_quadrant_tags):
            tag_to_memories.setdefault(tag, []).append(mem)

    # Eligible clusters, ranked: size desc, then tag name asc (deterministic).
    eligible = [
        (tag, mems)
        for tag, mems in tag_to_memories.items()
        if len(mems) >= min_cluster_size
    ]
    eligible.sort(key=lambda item: (-len(item[1]), item[0]))
    eligible = eligible[: max(0, max_clusters)] if max_clusters else eligible

    stamp = datetime.now(timezone.utc).isoformat() if timestamp else ""
    mocs: list[MOCIndex] = []
    for tag, mems in eligible:
        links, total = _sorted_links(mems, max_entries_per_section)
        section = MOCSection(
            name=f"#{tag}",
            description=f"Memories tagged '{tag}'",
            links=links,
            total=total,
        )
        mocs.append(
            MOCIndex(
                key=f"tag-{_slugify(tag)}",
                title=f"Map of Content — #{tag}",
                description=f"All memories sharing the '{tag}' tag ({total}).",
                kind="tag-cluster",
                sections=[section],
                generated_at=stamp,
            )
        )
    return mocs


def build_all_mocs(
    memories: list[Memory],
    *,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    max_entries_per_section: int = DEFAULT_MAX_ENTRIES_PER_SECTION,
    include_empty_quadrants: bool = False,
    timestamp: bool = False,
) -> list[MOCIndex]:
    """Build the full MOC set: the quadrant MOC plus every tag-cluster MOC.

    Returns:
        list[MOCIndex]: Quadrant MOC first, then tag-cluster MOCs.
    """
    result = [
        build_quadrant_moc(
            memories,
            max_entries_per_section=max_entries_per_section,
            include_empty=include_empty_quadrants,
            timestamp=timestamp,
        )
    ]
    result.extend(
        build_tag_cluster_mocs(
            memories,
            min_cluster_size=min_cluster_size,
            max_clusters=max_clusters,
            max_entries_per_section=max_entries_per_section,
            timestamp=timestamp,
        )
    )
    return result


# ─────────────────────────────────────────────────────────────
# Rendering / output
# ─────────────────────────────────────────────────────────────


def render_moc_markdown(moc: MOCIndex) -> str:
    """Render a MOC index to a Markdown document (deterministic).

    Returns:
        str: Markdown text ending with a trailing newline.
    """
    lines: list[str] = [f"# {moc.title}", ""]
    if moc.description:
        lines.append(f"_{moc.description}_")
        lines.append("")
    if moc.generated_at:
        lines.append(f"Generated: {moc.generated_at}")
        lines.append("")

    if not moc.sections or moc.total_links == 0:
        lines.append("_No memories to index._")
        lines.append("")
        return "\n".join(lines)

    for section in moc.sections:
        heading = f"## {section.name} ({section.total})"
        lines.append(heading)
        if section.description:
            lines.append(f"_{section.description}_")
        lines.append("")
        if not section.links:
            lines.append("_(empty)_")
            lines.append("")
            continue
        for link in section.links:
            gloss = f" — {link.summary}" if link.summary else ""
            lines.append(f"- **{link.title}** (`{link.memory_id}`){gloss}")
        if section.truncated:
            lines.append(f"- _…and {section.truncated} more_")
        lines.append("")

    return "\n".join(lines)


def write_mocs(
    mocs: list[MOCIndex],
    out_dir: str,
) -> list[str]:
    """Render and write each MOC to ``out_dir`` as ``<key>.md``.

    Creates ``out_dir`` if needed. Returns the list of written file paths.
    This is the only function here that touches the filesystem, and it only
    writes MOC index files — it never reads or writes the memory store.

    Args:
        mocs: MOC indexes to write.
        out_dir: Destination directory for the ``.md`` files.

    Returns:
        list[str]: Absolute-ish paths of the files written, in input order.
    """
    from pathlib import Path

    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for moc in mocs:
        path = dest / f"{moc.key}.md"
        path.write_text(render_moc_markdown(moc), encoding="utf-8")
        written.append(str(path))
    return written
