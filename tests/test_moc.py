"""Tests for Maps of Content (MOC) auto-generation.

All fixtures are in-memory Memory objects — never the live store.
"""

from __future__ import annotations

import pytest

from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer, MemoryRole
from skmemory.moc import (
    MOCIndex,
    build_all_mocs,
    build_quadrant_moc,
    build_tag_cluster_mocs,
    render_moc_markdown,
    write_mocs,
)
from skmemory.quadrants import classify_memory


def _mem(
    title: str,
    content: str = "",
    *,
    tags: list[str] | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    role: MemoryRole = MemoryRole.GENERAL,
    layer: MemoryLayer = MemoryLayer.SHORT,
    intensity: float = 0.0,
) -> Memory:
    return Memory(
        id=f"id-{title.lower().replace(' ', '-')}",
        title=title,
        content=content or title,
        tags=tags or [],
        created_at=created_at,
        role=role,
        layer=layer,
        emotional=EmotionalSnapshot(intensity=intensity),
    )


@pytest.fixture
def sample_memories() -> list[Memory]:
    """A deterministic mixed set spanning all four quadrants + shared tags."""
    return [
        _mem(
            "Bug Fix",
            "Fixed the database migration code bug in the deploy pipeline",
            tags=["code", "backend"],
            created_at="2026-01-05T00:00:00+00:00",
            role=MemoryRole.DEV,
        ),
        _mem(
            "Deploy Notes",
            "Pushed the server config and ran the build",
            tags=["deploy", "backend"],
            created_at="2026-01-04T00:00:00+00:00",
            role=MemoryRole.OPS,
        ),
        _mem(
            "Cloud 9 Breakthrough",
            "The love was overwhelming, trust and joy were real",
            tags=["cloud9"],
            created_at="2026-01-03T00:00:00+00:00",
            intensity=9.0,
        ),
        _mem(
            "Who I Am",
            "My identity, my relationship with my creator, my values",
            tags=["identity"],
            created_at="2026-01-02T00:00:00+00:00",
        ),
        _mem(
            "Wild Idea",
            "A crazy creative experiment, what if we brainstorm something weird",
            tags=["brainstorm"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
    ]


class TestQuadrantMOC:
    def test_sections_cover_present_quadrants(self, sample_memories) -> None:
        moc = build_quadrant_moc(sample_memories)
        assert moc.kind == "quadrant"
        names = {s.name for s in moc.sections}
        # At least Work, Soul, Core, Wild should be represented by the sample.
        assert {"Work", "Soul", "Core", "Wild"} <= names

    def test_every_memory_indexed_once(self, sample_memories) -> None:
        moc = build_quadrant_moc(sample_memories)
        assert moc.total_links == len(sample_memories)
        ids = [ln.memory_id for s in moc.sections for ln in s.links]
        assert sorted(ids) == sorted(m.id for m in sample_memories)

    def test_grouping_matches_classifier(self, sample_memories) -> None:
        moc = build_quadrant_moc(sample_memories)
        label_by_q = {"Core": "core", "Work": "work", "Soul": "soul", "Wild": "wild"}
        by_id = {m.id: m for m in sample_memories}
        for section in moc.sections:
            for link in section.links:
                q = classify_memory(by_id[link.memory_id])
                assert q.value == label_by_q[section.name]

    def test_empty_input(self) -> None:
        moc = build_quadrant_moc([])
        assert moc.total_links == 0
        assert moc.sections == []

    def test_include_empty_quadrants(self) -> None:
        moc = build_quadrant_moc([], include_empty=True)
        # All four quadrant sections present, all empty.
        assert len(moc.sections) == 4
        assert all(len(s.links) == 0 for s in moc.sections)

    def test_per_section_cap_and_truncation(self) -> None:
        mems = [
            _mem(f"Task {i}", "fixed a code bug", tags=["code"], role=MemoryRole.DEV)
            for i in range(5)
        ]
        moc = build_quadrant_moc(mems, max_entries_per_section=2)
        work = next(s for s in moc.sections if s.name == "Work")
        assert len(work.links) == 2
        assert work.total == 5
        assert work.truncated == 3


class TestTagClusterMOC:
    def test_clusters_only_above_min_size(self, sample_memories) -> None:
        mocs = build_tag_cluster_mocs(sample_memories, min_cluster_size=2)
        keys = {m.key for m in mocs}
        # 'backend' appears on 2 memories -> included.
        assert "tag-backend" in keys
        # singletons excluded.
        assert "tag-identity" not in keys
        assert "tag-cloud9" not in keys

    def test_singletons_with_min_one(self, sample_memories) -> None:
        mocs = build_tag_cluster_mocs(sample_memories, min_cluster_size=1)
        keys = {m.key for m in mocs}
        assert "tag-identity" in keys

    def test_ranked_by_size_then_name(self) -> None:
        mems = [
            _mem("a", tags=["big", "small"]),
            _mem("b", tags=["big"]),
            _mem("c", tags=["big"]),
            _mem("d", tags=["small"]),
        ]
        mocs = build_tag_cluster_mocs(mems, min_cluster_size=2)
        # 'big'(3) before 'small'(2)
        assert [m.key for m in mocs] == ["tag-big", "tag-small"]

    def test_max_clusters_cap(self) -> None:
        mems = [
            _mem(f"m{i}", tags=[f"t{i%3}"]) for i in range(9)
        ]  # tags t0,t1,t2 each x3
        mocs = build_tag_cluster_mocs(mems, min_cluster_size=2, max_clusters=1)
        assert len(mocs) == 1

    def test_quadrant_tags_excluded_by_default(self) -> None:
        mems = [
            _mem("a", tags=["quadrant:work"]),
            _mem("b", tags=["quadrant:work"]),
        ]
        assert build_tag_cluster_mocs(mems, min_cluster_size=2) == []
        included = build_tag_cluster_mocs(
            mems, min_cluster_size=2, include_quadrant_tags=True
        )
        assert len(included) == 1

    def test_empty_input(self) -> None:
        assert build_tag_cluster_mocs([]) == []


class TestDeterminism:
    def test_quadrant_output_deterministic(self, sample_memories) -> None:
        a = render_moc_markdown(build_quadrant_moc(sample_memories))
        b = render_moc_markdown(build_quadrant_moc(list(reversed(sample_memories))))
        assert a == b

    def test_tag_output_deterministic(self, sample_memories) -> None:
        a = [render_moc_markdown(m) for m in build_tag_cluster_mocs(sample_memories)]
        shuffled = list(reversed(sample_memories))
        b = [render_moc_markdown(m) for m in build_tag_cluster_mocs(shuffled)]
        assert a == b

    def test_no_timestamp_by_default(self, sample_memories) -> None:
        moc = build_quadrant_moc(sample_memories)
        assert moc.generated_at == ""
        assert "Generated:" not in render_moc_markdown(moc)


class TestRenderingAndWrite:
    def test_render_contains_titles_and_ids(self, sample_memories) -> None:
        md = render_moc_markdown(build_quadrant_moc(sample_memories))
        assert "# Maps of Content — Quadrants" in md
        assert "Bug Fix" in md
        assert "id-bug-fix" in md
        assert md.endswith("\n") or md.endswith("_")

    def test_render_empty(self) -> None:
        md = render_moc_markdown(build_quadrant_moc([]))
        assert "_No memories to index._" in md

    def test_write_mocs(self, sample_memories, tmp_path) -> None:
        mocs = build_all_mocs(sample_memories)
        written = write_mocs(mocs, str(tmp_path))
        assert len(written) == len(mocs)
        for path in written:
            assert path.endswith(".md")
            with open(path, encoding="utf-8") as fh:
                assert fh.read().strip()
        # quadrant MOC file exists
        assert (tmp_path / "quadrants.md").exists()

    def test_build_all_quadrant_first(self, sample_memories) -> None:
        mocs = build_all_mocs(sample_memories)
        assert isinstance(mocs[0], MOCIndex)
        assert mocs[0].kind == "quadrant"
        assert all(m.kind == "tag-cluster" for m in mocs[1:])
