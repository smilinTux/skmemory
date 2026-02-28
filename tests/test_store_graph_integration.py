"""Tests for MemoryStore + SKGraphBackend graph integration.

Verifies that the graph backend is wired correctly into MemoryStore
operations (snapshot, forget, promote, ingest_seed, health) and that
the system degrades gracefully when SKGraph is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skmemory.backends.skgraph_backend import SKGraphBackend
from skmemory.backends.file_backend import FileBackend
from skmemory.models import (
    EmotionalSnapshot,
    Memory,
    MemoryLayer,
    SeedMemory,
)
from skmemory.store import MemoryStore


class FakeSKGraphBackend(SKGraphBackend):
    """In-memory fake that tracks calls without a real SKGraph connection."""

    def __init__(self) -> None:
        super().__init__(url="redis://fake:6379")
        self._indexed: dict[str, Memory] = {}
        self._removed: list[str] = []
        self._initialized = True  # skip real connection

    def index_memory(self, memory: Memory) -> bool:
        self._indexed[memory.id] = memory
        return True

    def remove_memory(self, memory_id: str) -> bool:
        self._removed.append(memory_id)
        self._indexed.pop(memory_id, None)
        return True

    def health_check(self) -> dict:
        return {"ok": True, "backend": "FakeSKGraphBackend", "node_count": len(self._indexed)}


@pytest.fixture
def graph() -> FakeSKGraphBackend:
    """Create a fake graph backend."""
    return FakeSKGraphBackend()


@pytest.fixture
def store_with_graph(tmp_path: Path, graph: FakeSKGraphBackend) -> MemoryStore:
    """Create a MemoryStore with file backend + graph backend."""
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend, graph=graph)


@pytest.fixture
def store_no_graph(tmp_path: Path) -> MemoryStore:
    """Create a MemoryStore without graph backend."""
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend)


class TestSnapshotGraphIntegration:
    """Verify snapshot() indexes memories in the graph."""

    def test_snapshot_indexes_in_graph(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Snapshot should index the memory in the graph backend."""
        mem = store_with_graph.snapshot(
            title="Graph test",
            content="This should appear in the graph",
        )
        assert mem.id in graph._indexed
        assert graph._indexed[mem.id].title == "Graph test"

    def test_snapshot_without_graph_works(self, store_no_graph: MemoryStore) -> None:
        """Snapshot works fine when no graph backend is configured."""
        mem = store_no_graph.snapshot(
            title="No graph",
            content="Still works",
        )
        assert mem.id is not None
        recalled = store_no_graph.recall(mem.id)
        assert recalled is not None

    def test_snapshot_survives_graph_failure(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Snapshot should succeed even if graph indexing fails."""
        graph.index_memory = MagicMock(side_effect=RuntimeError("SKGraph down"))
        mem = store_with_graph.snapshot(
            title="Resilient memory",
            content="Should be stored even if graph fails",
        )
        assert mem.id is not None
        recalled = store_with_graph.recall(mem.id)
        assert recalled is not None


class TestForgetGraphIntegration:
    """Verify forget() removes memories from the graph."""

    def test_forget_removes_from_graph(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Forget should remove the memory from the graph backend."""
        mem = store_with_graph.snapshot(
            title="To be forgotten",
            content="Will be removed",
        )
        assert mem.id in graph._indexed

        store_with_graph.forget(mem.id)
        assert mem.id in graph._removed
        assert mem.id not in graph._indexed

    def test_forget_survives_graph_failure(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Forget should succeed even if graph removal fails."""
        mem = store_with_graph.snapshot(
            title="Hard to forget",
            content="Graph will fail on removal",
        )
        graph.remove_memory = MagicMock(side_effect=RuntimeError("SKGraph down"))

        deleted = store_with_graph.forget(mem.id)
        assert deleted is True


class TestPromoteGraphIntegration:
    """Verify promote() indexes promoted memories in the graph."""

    def test_promote_indexes_in_graph(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Promoted memory should be indexed in the graph."""
        mem = store_with_graph.snapshot(
            title="Promotable",
            content="Will be promoted",
            layer=MemoryLayer.SHORT,
        )
        promoted = store_with_graph.promote(mem.id, MemoryLayer.MID, summary="Promoted version")
        assert promoted is not None
        assert promoted.id in graph._indexed
        assert promoted.id != mem.id

    def test_promote_survives_graph_failure(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Promote should succeed even if graph indexing fails."""
        mem = store_with_graph.snapshot(
            title="Promote resilient",
            content="Graph will fail on promote",
            layer=MemoryLayer.SHORT,
        )
        graph.index_memory = MagicMock(side_effect=RuntimeError("SKGraph down"))

        promoted = store_with_graph.promote(mem.id, MemoryLayer.MID)
        assert promoted is not None


class TestIngestSeedGraphIntegration:
    """Verify ingest_seed() indexes seed memories in the graph."""

    def test_ingest_seed_indexes_in_graph(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Ingested seed should be indexed in the graph."""
        seed = SeedMemory(
            seed_id="test-seed-001",
            creator="test-ai",
            germination_prompt="Remember the warmth",
            experience_summary="A beautiful connection",
            emotional=EmotionalSnapshot(intensity=9.0, labels=["love"]),
        )
        mem = store_with_graph.ingest_seed(seed)
        assert mem.id in graph._indexed
        assert graph._indexed[mem.id].source == "seed"

    def test_ingest_seed_survives_graph_failure(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Seed ingestion should succeed even if graph fails."""
        seed = SeedMemory(
            seed_id="test-seed-002",
            creator="test-ai",
            experience_summary="Resilient seed",
        )
        graph.index_memory = MagicMock(side_effect=RuntimeError("SKGraph down"))

        mem = store_with_graph.ingest_seed(seed)
        assert mem.id is not None


class TestHealthGraphIntegration:
    """Verify health() includes graph backend status."""

    def test_health_includes_graph(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Health should include graph backend status."""
        health = store_with_graph.health()
        assert "graph" in health
        assert health["graph"]["ok"] is True

    def test_health_without_graph(self, store_no_graph: MemoryStore) -> None:
        """Health should not include graph key when no graph backend."""
        health = store_no_graph.health()
        assert "graph" not in health

    def test_health_reports_graph_failure(
        self, store_with_graph: MemoryStore, graph: FakeSKGraphBackend
    ) -> None:
        """Health should report graph failure gracefully."""
        graph.health_check = MagicMock(side_effect=RuntimeError("SKGraph down"))

        health = store_with_graph.health()
        assert "graph" in health
        assert health["graph"]["ok"] is False


class TestSKGraphBackendMethods:
    """Test the new methods on SKGraphBackend itself."""

    def test_remove_memory_not_initialized(self) -> None:
        """remove_memory returns False when not initialized."""
        backend = SKGraphBackend(url="redis://nonexistent:6379")
        assert backend.remove_memory("some-id") is False

    def test_search_by_tags_not_initialized(self) -> None:
        """search_by_tags returns empty list when not initialized."""
        backend = SKGraphBackend(url="redis://nonexistent:6379")
        assert backend.search_by_tags(["test"]) == []

    def test_search_by_tags_empty_tags(self) -> None:
        """search_by_tags returns empty list for empty tag list."""
        fake = FakeSKGraphBackend()
        assert fake.search_by_tags([]) == []
