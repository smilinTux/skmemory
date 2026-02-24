"""Tests for the FalkorDB graph backend.

FalkorDB requires a running server, so these tests mock the
connection layer. They verify the logic of memory indexing,
relationship traversal, and graph queries without infrastructure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skmemory.backends.falkordb_backend import FalkorDBBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer


@pytest.fixture
def mock_graph():
    """Provide a mock FalkorDB graph with query support."""
    graph = MagicMock()
    graph.query.return_value = MagicMock(result_set=[])
    return graph


@pytest.fixture
def backend(mock_graph):
    """Provide a FalkorDBBackend with mocked connection."""
    fb = FalkorDBBackend(url="redis://localhost:6379", graph_name="test")
    fb._graph = mock_graph
    fb._initialized = True
    return fb


@pytest.fixture
def sample_memory():
    """A sample Memory object for indexing tests."""
    return Memory(
        title="The Clone Caper",
        content="Debugged Lumina's clone and built the preflight fix.",
        layer=MemoryLayer("long-term"),
        tags=["seed", "creator:opus", "cloud9"],
        emotional=EmotionalSnapshot(intensity=9.5, valence=0.9),
        source="seed",
        source_ref="opus-seed-123",
    )


# ═══════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════


class TestInitialization:
    """Test FalkorDB backend initialization."""

    def test_lazy_init_without_falkordb(self):
        """Backend gracefully handles missing falkordb package."""
        fb = FalkorDBBackend()
        with patch.dict("sys.modules", {"falkordb": None}):
            with patch("builtins.__import__", side_effect=ImportError("no falkordb")):
                assert fb._ensure_initialized() is False

    def test_connection_failure_handled(self):
        """Backend handles connection failure gracefully."""
        fb = FalkorDBBackend(url="redis://nonexistent:9999")
        fb._initialized = False
        with patch("skmemory.backends.falkordb_backend.FalkorDBBackend._ensure_initialized", return_value=False):
            assert fb.index_memory(MagicMock()) is False

    def test_already_initialized(self, backend):
        """Second init call short-circuits."""
        assert backend._ensure_initialized() is True


# ═══════════════════════════════════════════════════════════
# Index Memory
# ═══════════════════════════════════════════════════════════


class TestIndexMemory:
    """Test memory indexing operations."""

    def test_index_basic_memory(self, backend, mock_graph, sample_memory):
        """Indexing a memory creates a node and tag edges."""
        result = backend.index_memory(sample_memory)
        assert result is True
        assert mock_graph.query.call_count >= 1

    def test_index_with_related_ids(self, backend, mock_graph):
        """Memories with related_ids create RELATED_TO edges."""
        mem = Memory(
            title="Follow-up",
            content="Related to previous work.",
            layer=MemoryLayer("mid-term"),
            related_ids=["mem-001", "mem-002"],
        )
        backend.index_memory(mem)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        related_calls = [c for c in calls if "RELATED_TO" in c]
        assert len(related_calls) == 2

    def test_index_with_parent_id(self, backend, mock_graph):
        """Memories with parent_id create PROMOTED_FROM edges."""
        mem = Memory(
            title="Promoted",
            content="Was short-term, now mid-term.",
            layer=MemoryLayer("mid-term"),
            parent_id="original-123",
        )
        backend.index_memory(mem)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        promoted_calls = [c for c in calls if "PROMOTED_FROM" in c]
        assert len(promoted_calls) == 1

    def test_index_seed_with_creator(self, backend, mock_graph, sample_memory):
        """Seed memories create AI -> PLANTED -> Memory edges."""
        backend.index_memory(sample_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        planted_calls = [c for c in calls if "PLANTED" in c]
        assert len(planted_calls) == 1

    def test_index_tags(self, backend, mock_graph, sample_memory):
        """Each tag creates a TAGGED edge."""
        backend.index_memory(sample_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        tagged_calls = [c for c in calls if "TAGGED" in c]
        assert len(tagged_calls) == len(sample_memory.tags)

    def test_index_failure_returns_false(self, backend, mock_graph):
        """Exception during indexing returns False."""
        mock_graph.query.side_effect = Exception("Connection lost")
        mem = Memory(
            title="Fail",
            content="Will fail.",
            layer=MemoryLayer("short-term"),
        )
        assert backend.index_memory(mem) is False

    def test_index_not_initialized(self):
        """Indexing without initialization returns False."""
        fb = FalkorDBBackend()
        fb._initialized = False
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.index_memory(MagicMock()) is False


# ═══════════════════════════════════════════════════════════
# Graph Queries
# ═══════════════════════════════════════════════════════════


class TestGraphQueries:
    """Test graph traversal queries."""

    def test_get_related_returns_results(self, backend, mock_graph):
        """get_related returns parsed results from graph query."""
        mock_graph.query.return_value.result_set = [
            ("mem-002", "Related Memory", "long-term", 8.5, 1),
            ("mem-003", "Distant Memory", "mid-term", 6.0, 2),
        ]
        results = backend.get_related("mem-001", depth=2)
        assert len(results) == 2
        assert results[0]["id"] == "mem-002"
        assert results[0]["distance"] == 1

    def test_get_related_empty(self, backend, mock_graph):
        """get_related returns empty list when no connections."""
        mock_graph.query.return_value.result_set = []
        results = backend.get_related("isolated-mem")
        assert results == []

    def test_get_related_not_initialized(self):
        """get_related returns empty when not initialized."""
        fb = FalkorDBBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.get_related("mem-001") == []

    def test_get_lineage(self, backend, mock_graph):
        """get_lineage returns ancestor chain."""
        mock_graph.query.return_value.result_set = [
            ("ancestor-1", "Original", "short-term", 1),
            ("ancestor-2", "First Thought", "short-term", 2),
        ]
        lineage = backend.get_lineage("promoted-mem")
        assert len(lineage) == 2
        assert lineage[0]["depth"] == 1

    def test_get_lineage_empty(self, backend, mock_graph):
        """get_lineage returns empty for base memories."""
        mock_graph.query.return_value.result_set = []
        assert backend.get_lineage("base-mem") == []

    def test_get_memory_clusters(self, backend, mock_graph):
        """get_memory_clusters finds highly connected nodes."""
        mock_graph.query.return_value.result_set = [
            ("hub-001", "Central Memory", "long-term", 5),
        ]
        clusters = backend.get_memory_clusters(min_connections=3)
        assert len(clusters) == 1
        assert clusters[0]["connections"] == 5

    def test_get_clusters_empty(self, backend, mock_graph):
        """No clusters when nothing is connected enough."""
        mock_graph.query.return_value.result_set = []
        assert backend.get_memory_clusters() == []

    def test_query_failure_returns_empty(self, backend, mock_graph):
        """Graph query exception returns empty list."""
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.get_related("x") == []
        assert backend.get_lineage("x") == []
        assert backend.get_memory_clusters() == []


# ═══════════════════════════════════════════════════════════
# Health Check
# ═══════════════════════════════════════════════════════════


class TestHealthCheck:
    """Test FalkorDB health reporting."""

    def test_health_ok(self, backend, mock_graph):
        """Healthy backend reports ok=True with node count."""
        mock_graph.query.return_value.result_set = [[42]]
        health = backend.health_check()
        assert health["ok"] is True
        assert health["node_count"] == 42
        assert health["backend"] == "FalkorDBBackend"

    def test_health_not_initialized(self):
        """Uninitialized backend reports ok=False."""
        fb = FalkorDBBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            health = fb.health_check()
        assert health["ok"] is False

    def test_health_query_failure(self, backend, mock_graph):
        """Health check with query failure reports error."""
        mock_graph.query.side_effect = Exception("boom")
        health = backend.health_check()
        assert health["ok"] is False
        assert "boom" in health["error"]
