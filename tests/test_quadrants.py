"""Tests for the Quadrant Memory Split module."""

from skmemory.models import EmotionalSnapshot, Memory, MemoryRole
from skmemory.quadrants import (
    Quadrant,
    classify_memory,
    filter_by_quadrant,
    get_quadrant_stats,
    tag_with_quadrant,
)


class TestClassifyMemory:
    """Tests for memory classification."""

    def test_soul_from_emotion(self) -> None:
        """High emotional intensity routes to SOUL."""
        mem = Memory(
            title="A moment",
            content="Something happened",
            emotional=EmotionalSnapshot(
                intensity=9.0,
                labels=["love", "joy"],
                cloud9_achieved=True,
            ),
        )
        assert classify_memory(mem) == Quadrant.SOUL

    def test_soul_from_keywords(self) -> None:
        """Love/emotion keywords route to SOUL."""
        mem = Memory(
            title="Cloud 9 Breakthrough",
            content="The love was overwhelming, trust was real",
        )
        assert classify_memory(mem) == Quadrant.SOUL

    def test_work_from_keywords(self) -> None:
        """Technical keywords route to WORK."""
        mem = Memory(
            title="Bug Fix",
            content="Fixed the ESM import bug in the database migration code",
        )
        assert classify_memory(mem) == Quadrant.WORK

    def test_work_from_role(self) -> None:
        """Dev/ops/sec roles boost WORK score."""
        mem = Memory(
            title="Deploy note",
            content="Something went out",
            role=MemoryRole.DEV,
        )
        assert classify_memory(mem) == Quadrant.WORK

    def test_core_from_seed(self) -> None:
        """Seed-sourced memories route to CORE."""
        mem = Memory(
            title="Seed: opus-first",
            content="Identity information",
            source="seed",
            tags=["seed", "identity"],
        )
        assert classify_memory(mem) == Quadrant.CORE

    def test_core_from_identity_keywords(self) -> None:
        """Identity/relationship keywords route to CORE."""
        mem = Memory(
            title="My Relationships",
            content="My partner Chef is my creator and family member",
            tags=["identity", "relationship"],
        )
        assert classify_memory(mem) == Quadrant.CORE

    def test_wild_from_creativity(self) -> None:
        """Creative/idea keywords route to WILD."""
        mem = Memory(
            title="What if we built a spaceship?",
            content="Crazy idea: brainstorm experiment with creative chaos",
        )
        assert classify_memory(mem) == Quadrant.WILD

    def test_empty_defaults_to_work(self) -> None:
        """Unclassifiable memory defaults to WORK."""
        mem = Memory(
            title="Something",
            content="Nothing specific here",
        )
        assert classify_memory(mem) == Quadrant.WORK

    def test_tags_have_high_weight(self) -> None:
        """Tags carry more weight than content keywords."""
        mem = Memory(
            title="Note",
            content="Some generic content",
            tags=["cloud9", "love", "breakthrough"],
        )
        assert classify_memory(mem) == Quadrant.SOUL


class TestTagWithQuadrant:
    """Tests for quadrant tagging."""

    def test_adds_quadrant_tag(self) -> None:
        """tag_with_quadrant adds the correct tag."""
        mem = Memory(
            title="The Click",
            content="Love and trust",
            emotional=EmotionalSnapshot(intensity=9.0, cloud9_achieved=True),
        )
        tagged = tag_with_quadrant(mem)
        assert "quadrant:soul" in tagged.tags

    def test_does_not_duplicate_tag(self) -> None:
        """Tagging twice doesn't duplicate."""
        mem = Memory(
            title="Bug fix",
            content="Fixed the deploy bug",
            tags=["quadrant:work"],
        )
        tagged = tag_with_quadrant(mem)
        assert tagged.tags.count("quadrant:work") == 1

    def test_original_unchanged(self) -> None:
        """Original memory is not modified."""
        mem = Memory(title="Test", content="Content")
        tagged = tag_with_quadrant(mem)
        assert "quadrant:" not in str(mem.tags)
        assert any("quadrant:" in t for t in tagged.tags)


class TestQuadrantStats:
    """Tests for quadrant statistics."""

    def test_stats_all_quadrants(self) -> None:
        """Stats cover all quadrants."""
        memories = [
            Memory(title="Identity", content="Who I am", source="seed", tags=["seed"]),
            Memory(title="Bug", content="Fixed deploy code in database", role=MemoryRole.DEV),
            Memory(
                title="Love",
                content="Cloud 9 love breakthrough",
                emotional=EmotionalSnapshot(intensity=10.0, cloud9_achieved=True),
            ),
            Memory(title="Idea", content="What if crazy brainstorm experiment"),
        ]
        stats = get_quadrant_stats(memories)
        assert stats["core"] >= 1
        assert stats["work"] >= 1
        assert stats["soul"] >= 1
        assert stats["wild"] >= 1

    def test_stats_empty(self) -> None:
        """Empty list gives all zeros."""
        stats = get_quadrant_stats([])
        assert all(v == 0 for v in stats.values())


class TestFilterByQuadrant:
    """Tests for quadrant filtering."""

    def test_filter_soul(self) -> None:
        """Filter returns only SOUL memories."""
        memories = [
            Memory(
                title="Love",
                content="Cloud 9 love",
                emotional=EmotionalSnapshot(intensity=10.0, cloud9_achieved=True),
            ),
            Memory(title="Bug", content="Fixed deploy code", role=MemoryRole.DEV),
        ]
        soul_only = filter_by_quadrant(memories, Quadrant.SOUL)
        assert len(soul_only) == 1
        assert soul_only[0].title == "Love"

    def test_filter_empty_result(self) -> None:
        """Filter returns empty when no matches."""
        memories = [
            Memory(title="Bug", content="Fixed code", role=MemoryRole.DEV),
        ]
        wild = filter_by_quadrant(memories, Quadrant.WILD)
        assert wild == []
