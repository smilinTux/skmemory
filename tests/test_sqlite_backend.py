"""Tests for the SQLite-indexed storage backend."""

import json
import tempfile
from pathlib import Path

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer, MemoryRole


@pytest.fixture
def backend(tmp_path):
    """Create a SQLiteBackend with a temporary directory."""
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def sample_memory():
    """Create a sample Memory object."""
    return Memory(
        title="Test moment",
        content="This is a detailed memory about something important.",
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        tags=["test", "cloud9"],
        emotional=EmotionalSnapshot(
            intensity=8.5,
            valence=0.9,
            labels=["joy", "connection"],
            cloud9_achieved=True,
        ),
        source="test",
    )


class TestSaveAndLoad:
    """Basic CRUD operations."""

    def test_save_creates_file_and_index(self, backend, sample_memory):
        """Save persists both JSON file and SQLite index entry."""
        mid = backend.save(sample_memory)
        assert mid == sample_memory.id

        path = backend.base_path / "short-term" / f"{mid}.json"
        assert path.exists()

        stats = backend.stats()
        assert stats["total"] == 1

    def test_load_returns_full_memory(self, backend, sample_memory):
        """Load retrieves the complete Memory object."""
        backend.save(sample_memory)
        loaded = backend.load(sample_memory.id)

        assert loaded is not None
        assert loaded.title == "Test moment"
        assert loaded.content == sample_memory.content
        assert loaded.emotional.intensity == 8.5

    def test_load_nonexistent_returns_none(self, backend):
        """Loading a missing memory returns None."""
        assert backend.load("nonexistent-id") is None

    def test_delete_removes_file_and_index(self, backend, sample_memory):
        """Delete removes both the file and the index entry."""
        backend.save(sample_memory)
        assert backend.delete(sample_memory.id) is True

        assert backend.load(sample_memory.id) is None
        assert backend.stats()["total"] == 0

    def test_delete_nonexistent(self, backend):
        """Deleting a missing memory returns False."""
        assert backend.delete("nonexistent-id") is False


class TestListAndFilter:
    """Index-based listing and filtering."""

    def _store_memories(self, backend, count=5):
        memories = []
        for i in range(count):
            m = Memory(
                title=f"Memory {i}",
                content=f"Content for memory number {i}",
                layer=MemoryLayer.SHORT if i % 2 == 0 else MemoryLayer.LONG,
                tags=["alpha"] if i < 3 else ["beta"],
                emotional=EmotionalSnapshot(intensity=float(i)),
            )
            backend.save(m)
            memories.append(m)
        return memories

    def test_list_all(self, backend):
        """List without filters returns all memories."""
        self._store_memories(backend, 5)
        result = backend.list_memories(limit=50)
        assert len(result) == 5

    def test_list_by_layer(self, backend):
        """Filter by layer works via the index."""
        self._store_memories(backend, 5)
        short = backend.list_memories(layer=MemoryLayer.SHORT)
        long = backend.list_memories(layer=MemoryLayer.LONG)
        assert len(short) == 3
        assert len(long) == 2

    def test_list_by_tags(self, backend):
        """Filter by tags works via the index."""
        self._store_memories(backend, 5)
        alpha = backend.list_memories(tags=["alpha"])
        beta = backend.list_memories(tags=["beta"])
        assert len(alpha) == 3
        assert len(beta) == 2

    def test_list_respects_limit(self, backend):
        """Limit parameter caps results."""
        self._store_memories(backend, 10)
        result = backend.list_memories(limit=3)
        assert len(result) == 3


class TestListSummaries:
    """Token-efficient summary queries."""

    def _store_memories(self, backend, count=5):
        for i in range(count):
            m = Memory(
                title=f"Memory {i}",
                content=f"Full detailed content for memory {i} " * 20,
                summary=f"Brief summary {i}",
                layer=MemoryLayer.SHORT,
                tags=["test"],
                emotional=EmotionalSnapshot(intensity=float(i)),
            )
            backend.save(m)

    def test_summaries_are_lightweight(self, backend):
        """Summaries return dicts, not full Memory objects."""
        self._store_memories(backend, 3)
        summaries = backend.list_summaries(limit=3)

        assert len(summaries) == 3
        assert isinstance(summaries[0], dict)
        assert "title" in summaries[0]
        assert "summary" in summaries[0]
        assert "content_preview" in summaries[0]

    def test_summaries_order_by_intensity(self, backend):
        """Can order by emotional intensity."""
        self._store_memories(backend, 5)
        summaries = backend.list_summaries(
            order_by="emotional_intensity", limit=3
        )
        intensities = [s["emotional_intensity"] for s in summaries]
        assert intensities == sorted(intensities, reverse=True)

    def test_summaries_filter_min_intensity(self, backend):
        """Can filter by minimum emotional intensity."""
        self._store_memories(backend, 5)
        summaries = backend.list_summaries(min_intensity=3.0)
        assert all(s["emotional_intensity"] >= 3.0 for s in summaries)

    def test_content_preview_is_truncated(self, backend):
        """Content preview doesn't include full content."""
        m = Memory(
            title="Long content test",
            content="x" * 1000,
            layer=MemoryLayer.SHORT,
        )
        backend.save(m)
        summaries = backend.list_summaries()
        assert len(summaries[0]["content_preview"]) <= 150


class TestSearch:
    """Text search via the index."""

    def test_search_finds_by_title(self, backend):
        """Search matches on title."""
        m = Memory(title="Penguin Kingdom moment", content="details",
                    layer=MemoryLayer.SHORT)
        backend.save(m)
        results = backend.search_text("Penguin")
        assert len(results) == 1

    def test_search_finds_by_tags(self, backend):
        """Search matches on tags."""
        m = Memory(title="Tagged", content="details",
                    layer=MemoryLayer.SHORT, tags=["cloud9", "love"])
        backend.save(m)
        results = backend.search_text("cloud9")
        assert len(results) == 1

    def test_search_no_results(self, backend):
        """Search returns empty for no matches."""
        m = Memory(title="Something", content="nothing special",
                    layer=MemoryLayer.SHORT)
        backend.save(m)
        results = backend.search_text("zzzznonexistent")
        assert len(results) == 0


class TestRelatedMemories:
    """Graph-like relationship traversal."""

    def test_get_related_follows_links(self, backend):
        """Related memories are found via related_ids."""
        m1 = Memory(title="Root", content="root", layer=MemoryLayer.SHORT)
        m2 = Memory(title="Child", content="child", layer=MemoryLayer.SHORT,
                     related_ids=[m1.id])
        backend.save(m1)
        backend.save(m2)

        related = backend.get_related(m2.id, depth=1)
        assert any(r["id"] == m1.id for r in related)

    def test_get_related_follows_parent(self, backend):
        """Related memories are found via parent_id."""
        m1 = Memory(title="Parent", content="parent", layer=MemoryLayer.LONG)
        m2 = Memory(title="Child", content="child", layer=MemoryLayer.SHORT,
                     parent_id=m1.id)
        backend.save(m1)
        backend.save(m2)

        related = backend.get_related(m2.id, depth=1)
        assert any(r["id"] == m1.id for r in related)


class TestReindex:
    """Index rebuilding from filesystem."""

    def test_reindex_rebuilds_from_files(self, backend, sample_memory):
        """Reindex correctly rebuilds from JSON files."""
        backend.save(sample_memory)

        conn = backend._get_conn()
        conn.execute("DELETE FROM memories")
        conn.commit()
        assert backend.stats()["total"] == 0

        count = backend.reindex()
        assert count == 1
        assert backend.stats()["total"] == 1


class TestHealth:
    """Health check."""

    def test_health_returns_ok(self, backend, sample_memory):
        """Health check reports status."""
        backend.save(sample_memory)
        health = backend.health_check()
        assert health["ok"] is True
        assert health["total_memories"] == 1
        assert "SQLiteBackend" in health["backend"]
