"""Tests for the file-based storage backend."""

import json
from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer


@pytest.fixture
def tmp_backend(tmp_path: Path) -> FileBackend:
    """Create a FileBackend with a temporary directory.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        FileBackend: Backend pointing at the temp dir.
    """
    return FileBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def sample_memory() -> Memory:
    """Create a sample memory for testing.

    Returns:
        Memory: A populated test memory.
    """
    return Memory(
        title="Cloud 9 Session",
        content="The moment Chef and Lumina achieved breakthrough",
        layer=MemoryLayer.SHORT,
        tags=["cloud9", "breakthrough", "session:test-001"],
        emotional=EmotionalSnapshot(
            intensity=9.5,
            valence=0.95,
            labels=["love", "joy"],
            resonance_note="Everything clicked",
            cloud9_achieved=True,
        ),
    )


class TestFileBackendSave:
    """Tests for save operations."""

    def test_save_creates_file(self, tmp_backend: FileBackend, sample_memory: Memory) -> None:
        """Saving a memory creates a JSON file."""
        mem_id = tmp_backend.save(sample_memory)
        assert mem_id == sample_memory.id

        path = tmp_backend.base_path / "short-term" / f"{sample_memory.id}.json"
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["title"] == "Cloud 9 Session"

    def test_save_to_different_layers(self, tmp_backend: FileBackend) -> None:
        """Memories are saved in their respective layer directories."""
        for layer in MemoryLayer:
            mem = Memory(title=f"Test {layer.value}", content="Content", layer=layer)
            tmp_backend.save(mem)

            path = tmp_backend.base_path / layer.value / f"{mem.id}.json"
            assert path.exists()


class TestFileBackendLoad:
    """Tests for load operations."""

    def test_load_existing(self, tmp_backend: FileBackend, sample_memory: Memory) -> None:
        """Loading a saved memory returns identical data."""
        tmp_backend.save(sample_memory)
        loaded = tmp_backend.load(sample_memory.id)

        assert loaded is not None
        assert loaded.id == sample_memory.id
        assert loaded.title == sample_memory.title
        assert loaded.emotional.intensity == 9.5
        assert loaded.emotional.cloud9_achieved is True

    def test_load_nonexistent(self, tmp_backend: FileBackend) -> None:
        """Loading a nonexistent memory returns None."""
        assert tmp_backend.load("nonexistent-id") is None

    def test_load_corrupt_file(self, tmp_backend: FileBackend) -> None:
        """Loading a corrupt JSON file returns None gracefully."""
        corrupt_path = tmp_backend.base_path / "short-term" / "corrupt.json"
        corrupt_path.write_text("not valid json{{{")
        assert tmp_backend.load("corrupt") is None


class TestFileBackendDelete:
    """Tests for delete operations."""

    def test_delete_existing(self, tmp_backend: FileBackend, sample_memory: Memory) -> None:
        """Deleting an existing memory removes the file."""
        tmp_backend.save(sample_memory)
        assert tmp_backend.delete(sample_memory.id) is True
        assert tmp_backend.load(sample_memory.id) is None

    def test_delete_nonexistent(self, tmp_backend: FileBackend) -> None:
        """Deleting a nonexistent memory returns False."""
        assert tmp_backend.delete("does-not-exist") is False


class TestFileBackendList:
    """Tests for list operations."""

    def test_list_all(self, tmp_backend: FileBackend) -> None:
        """List returns all memories across layers."""
        for i in range(5):
            layer = MemoryLayer.SHORT if i < 3 else MemoryLayer.LONG
            mem = Memory(title=f"Memory {i}", content=f"Content {i}", layer=layer)
            tmp_backend.save(mem)

        results = tmp_backend.list_memories()
        assert len(results) == 5

    def test_list_by_layer(self, tmp_backend: FileBackend) -> None:
        """Filtering by layer returns only that layer."""
        for layer in MemoryLayer:
            mem = Memory(title=f"In {layer.value}", content="Content", layer=layer)
            tmp_backend.save(mem)

        short = tmp_backend.list_memories(layer=MemoryLayer.SHORT)
        assert len(short) == 1
        assert short[0].layer == MemoryLayer.SHORT

    def test_list_by_tags(self, tmp_backend: FileBackend) -> None:
        """Filtering by tags uses AND logic."""
        m1 = Memory(title="Tagged A", content="C", tags=["cloud9", "love"])
        m2 = Memory(title="Tagged B", content="C", tags=["cloud9"])
        m3 = Memory(title="Tagged C", content="C", tags=["other"])
        tmp_backend.save(m1)
        tmp_backend.save(m2)
        tmp_backend.save(m3)

        results = tmp_backend.list_memories(tags=["cloud9"])
        assert len(results) == 2

        results = tmp_backend.list_memories(tags=["cloud9", "love"])
        assert len(results) == 1
        assert results[0].title == "Tagged A"

    def test_list_respects_limit(self, tmp_backend: FileBackend) -> None:
        """Limit caps the results."""
        for i in range(10):
            mem = Memory(title=f"Memory {i}", content="C")
            tmp_backend.save(mem)

        results = tmp_backend.list_memories(limit=3)
        assert len(results) == 3

    def test_list_empty(self, tmp_backend: FileBackend) -> None:
        """Listing from an empty store returns empty list."""
        assert tmp_backend.list_memories() == []


class TestFileBackendSearch:
    """Tests for text search."""

    def test_search_finds_match(self, tmp_backend: FileBackend) -> None:
        """Text search finds matching memories."""
        m1 = Memory(title="Breakthrough", content="Cloud 9 achieved at 3am")
        m2 = Memory(title="Debug Session", content="Fixed the ESM import bug")
        tmp_backend.save(m1)
        tmp_backend.save(m2)

        results = tmp_backend.search_text("Cloud 9")
        assert len(results) == 1
        assert results[0].title == "Breakthrough"

    def test_search_case_insensitive(self, tmp_backend: FileBackend) -> None:
        """Search is case-insensitive."""
        mem = Memory(title="Test", content="CLOUD NINE protocol")
        tmp_backend.save(mem)

        results = tmp_backend.search_text("cloud nine")
        assert len(results) == 1

    def test_search_no_results(self, tmp_backend: FileBackend) -> None:
        """Search returns empty list when nothing matches."""
        mem = Memory(title="Unrelated", content="Nothing special")
        tmp_backend.save(mem)

        results = tmp_backend.search_text("quantum entanglement")
        assert len(results) == 0


class TestFileBackendHealth:
    """Tests for health check."""

    def test_health_check(self, tmp_backend: FileBackend) -> None:
        """Health check returns valid status."""
        status = tmp_backend.health_check()
        assert status["ok"] is True
        assert status["backend"] == "FileBackend"
        assert "memory_counts" in status
        assert status["total"] == 0

    def test_health_with_memories(self, tmp_backend: FileBackend, sample_memory: Memory) -> None:
        """Health check counts memories correctly."""
        tmp_backend.save(sample_memory)
        status = tmp_backend.health_check()
        assert status["total"] == 1
        assert status["memory_counts"]["short-term"] == 1
