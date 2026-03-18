"""Tests for the SKGraph (FalkorDB) graph backend (Level 2).

FalkorDB requires a running server, so these tests mock the connection
layer. They verify the logic of memory indexing, relationship creation,
traversal, cluster detection, search, stats, and health reporting
without requiring any infrastructure.

Coverage areas:
    - Initialization (lazy-init, failure handling)
    - save() / index_memory() — node and edge creation
    - get() — node property retrieval
    - search() — title full-text search
    - delete() / remove_memory() — DETACH DELETE
    - TAGGED, FROM_SOURCE, RELATED_TO, PROMOTED_FROM, PRECEDED_BY, PLANTED
    - traverse() / get_related() — multi-hop traversal
    - get_lineage() — PROMOTED_FROM chain traversal
    - find_clusters() / get_memory_clusters() — hub detection
    - search_by_tags() — tag-overlap graph search
    - stats() — node/edge/tag counts
    - health_check() — connectivity probe
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skmemory.backends.skgraph_backend import SKGraphBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

# ─────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph():
    """Provide a mock FalkorDB graph with query support."""
    graph = MagicMock()
    graph.query.return_value = MagicMock(result_set=[])
    return graph


@pytest.fixture
def backend(mock_graph):
    """Provide a SKGraphBackend with mocked connection."""
    fb = SKGraphBackend(url="redis://localhost:6379", graph_name="test")
    fb._graph = mock_graph
    fb._initialized = True
    return fb


@pytest.fixture
def sample_memory():
    """A sample Memory for indexing tests."""
    return Memory(
        title="The Clone Caper",
        content="Debugged Lumina's clone and built the preflight fix.",
        layer=MemoryLayer("long-term"),
        tags=["seed", "creator:opus", "cloud9"],
        emotional=EmotionalSnapshot(intensity=9.5, valence=0.9),
        source="seed",
        source_ref="opus-seed-123",
    )


@pytest.fixture
def related_memory():
    """A sample Memory with explicit related_ids and parent_id."""
    return Memory(
        title="Follow-up",
        content="Related to previous work.",
        layer=MemoryLayer("mid-term"),
        related_ids=["mem-001", "mem-002"],
        parent_id="original-123",
    )


# ═══════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════


class TestInitialization:
    """Test SKGraph backend initialization."""

    def test_lazy_init_without_falkordb(self):
        """Backend gracefully handles missing falkordb package."""
        fb = SKGraphBackend()
        with (
            patch.dict("sys.modules", {"falkordb": None}),
            patch("builtins.__import__", side_effect=ImportError("no falkordb")),
        ):
            assert fb._ensure_initialized() is False

    def test_connection_failure_handled(self):
        """Backend handles connection failure gracefully."""
        fb = SKGraphBackend(url="redis://nonexistent:9999")
        fb._initialized = False
        with patch(
            "skmemory.backends.skgraph_backend.SKGraphBackend._ensure_initialized",
            return_value=False,
        ):
            assert fb.index_memory(MagicMock()) is False

    def test_already_initialized(self, backend):
        """Second init call short-circuits."""
        assert backend._ensure_initialized() is True

    def test_not_initialized_by_default(self):
        """Fresh backend starts uninitialized."""
        fb = SKGraphBackend()
        assert fb._initialized is False


# ═══════════════════════════════════════════════════════════
# Index Memory / save
# ═══════════════════════════════════════════════════════════


class TestIndexMemory:
    """Test memory indexing and edge creation."""

    def test_index_basic_memory(self, backend, mock_graph, sample_memory):
        """Indexing a memory creates a node and edges."""
        result = backend.index_memory(sample_memory)
        assert result is True
        assert mock_graph.query.call_count >= 1

    def test_save_returns_memory_id(self, backend, sample_memory):
        """save() returns the memory ID unchanged."""
        result = backend.save(sample_memory)
        assert result == sample_memory.id

    def test_index_with_related_ids(self, backend, mock_graph, related_memory):
        """Memories with related_ids create RELATED_TO edges."""
        backend.index_memory(related_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        # Explicit RELATED_TO edges: one per related_id, passed with b_id param.
        # Exclude the shared-tag auto-wire query (which uses a_id only, no b_id).
        explicit_related = [c for c in calls if "RELATED_TO" in c and "b_id" in c]
        assert len(explicit_related) == len(related_memory.related_ids)

    def test_index_with_parent_id(self, backend, mock_graph, related_memory):
        """Memories with parent_id create PROMOTED_FROM edges."""
        backend.index_memory(related_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        promoted_calls = [c for c in calls if "PROMOTED_FROM" in c]
        assert len(promoted_calls) == 1

    def test_index_seed_with_creator(self, backend, mock_graph, sample_memory):
        """Seed memories create AI-[:PLANTED]->Memory edges."""
        backend.index_memory(sample_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        planted_calls = [c for c in calls if "PLANTED" in c]
        assert len(planted_calls) == 1

    def test_index_tags(self, backend, mock_graph, sample_memory):
        """Each tag creates a TAGGED edge."""
        backend.index_memory(sample_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        # Explicit TAGGED calls use $tag param; exclude the shared-tag sweep
        # (CREATE_SHARED_TAG_RELATED also contains "TAGGED" but uses $a_id only).
        explicit_tagged = [c for c in calls if "TAGGED" in c and "'tag'" in c]
        assert len(explicit_tagged) == len(sample_memory.tags)

    def test_index_creates_from_source_edge(self, backend, mock_graph, sample_memory):
        """Indexing creates a FROM_SOURCE edge to the source node."""
        backend.index_memory(sample_memory)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        source_calls = [c for c in calls if "FROM_SOURCE" in c]
        assert len(source_calls) >= 1

    def test_index_creates_preceded_by_edge_when_prior_exists(self, backend, mock_graph):
        """PRECEDED_BY edge is created when a prior memory from same source exists."""
        # Simulate a previous memory from same source being found
        prior_result = MagicMock()
        prior_result.result_set = [("prior-mem-id", "2026-01-01T00:00:00")]

        empty_result = MagicMock()
        empty_result.result_set = []

        # Query call order for a non-seed memory without tags:
        # 1. UPSERT_MEMORY
        # 2. (no PROMOTED_FROM — no parent)
        # 3. (no RELATED_TO — no related_ids)
        # 4. (no TAGGED — no tags)
        # 5. CREATE_SHARED_TAG_RELATED
        # 6. CREATE_FROM_SOURCE
        # 7. FIND_PREVIOUS_FROM_SOURCE -> returns prior
        # 8. CREATE_PRECEDED_BY
        call_count = [0]

        def side_effect(query, params=None):
            call_count[0] += 1
            result = MagicMock()
            if "FIND_PREVIOUS_FROM_SOURCE" in query or (params and "exclude_id" in params):
                result.result_set = [["prior-mem-id", "2026-01-01T00:00:00"]]
            else:
                result.result_set = []
            return result

        mock_graph.query.side_effect = side_effect

        mem = Memory(
            title="New Session Memory",
            content="Something happened.",
            layer=MemoryLayer("short-term"),
            source="mcp",
        )
        backend.index_memory(mem)
        calls = [str(c) for c in mock_graph.query.call_args_list]
        preceded_calls = [c for c in calls if "PRECEDED_BY" in c]
        assert len(preceded_calls) == 1

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
        fb = SKGraphBackend()
        fb._initialized = False
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.index_memory(MagicMock()) is False


# ═══════════════════════════════════════════════════════════
# get()
# ═══════════════════════════════════════════════════════════


class TestGet:
    """Test graph node property retrieval."""

    def test_get_returns_node_properties(self, backend, mock_graph):
        """get() returns a dict with node properties when found."""
        mock_graph.query.return_value.result_set = [
            (
                "mem-001",
                "The Clone Caper",
                "long-term",
                "seed",
                "opus-seed-123",
                9.5,
                0.9,
                "2026-02-27T00:00:00",
                "2026-02-27T00:00:00",
            )
        ]
        result = backend.get("mem-001")
        assert result is not None
        assert result["id"] == "mem-001"
        assert result["title"] == "The Clone Caper"
        assert result["layer"] == "long-term"
        assert result["intensity"] == 9.5

    def test_get_returns_none_when_not_found(self, backend, mock_graph):
        """get() returns None when the node doesn't exist."""
        mock_graph.query.return_value.result_set = []
        assert backend.get("nonexistent-id") is None

    def test_get_not_initialized(self):
        """get() returns None when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.get("any-id") is None

    def test_get_handles_query_failure(self, backend, mock_graph):
        """get() returns None on query exception."""
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.get("mem-001") is None


# ═══════════════════════════════════════════════════════════
# search()
# ═══════════════════════════════════════════════════════════


class TestSearch:
    """Test title full-text search."""

    def test_search_returns_matching_memories(self, backend, mock_graph):
        """search() returns matching memory stubs."""
        mock_graph.query.return_value.result_set = [
            ("mem-001", "The Clone Caper", "long-term", 9.5, "2026-02-27T00:00:00"),
        ]
        results = backend.search("clone")
        assert len(results) == 1
        assert results[0]["id"] == "mem-001"
        assert results[0]["title"] == "The Clone Caper"

    def test_search_returns_empty_when_no_match(self, backend, mock_graph):
        """search() returns empty list when nothing matches."""
        mock_graph.query.return_value.result_set = []
        assert backend.search("nonexistent") == []

    def test_search_not_initialized(self):
        """search() returns empty when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.search("anything") == []

    def test_search_handles_exception(self, backend, mock_graph):
        """search() returns empty list on query failure."""
        mock_graph.query.side_effect = Exception("boom")
        assert backend.search("test") == []


# ═══════════════════════════════════════════════════════════
# delete() / remove_memory()
# ═══════════════════════════════════════════════════════════


class TestDelete:
    """Test memory node deletion."""

    def test_delete_returns_true(self, backend, mock_graph):
        """delete() returns True on successful removal."""
        assert backend.delete("mem-001") is True

    def test_remove_memory_returns_true(self, backend, mock_graph):
        """remove_memory() returns True on successful removal."""
        assert backend.remove_memory("mem-001") is True

    def test_delete_calls_detach_delete(self, backend, mock_graph):
        """delete() issues a DETACH DELETE query."""
        backend.delete("mem-001")
        calls = [str(c) for c in mock_graph.query.call_args_list]
        delete_calls = [c for c in calls if "DETACH DELETE" in c or "DELETE" in c]
        assert len(delete_calls) >= 1

    def test_delete_not_initialized(self):
        """delete() returns False when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.delete("any") is False

    def test_remove_memory_not_initialized(self):
        """remove_memory() returns False when not initialized."""
        backend = SKGraphBackend(url="redis://nonexistent:6379")
        assert backend.remove_memory("some-id") is False

    def test_delete_handles_exception(self, backend, mock_graph):
        """delete() returns False on query exception."""
        mock_graph.query.side_effect = Exception("gone")
        assert backend.delete("mem-001") is False


# ═══════════════════════════════════════════════════════════
# traverse() / get_related()
# ═══════════════════════════════════════════════════════════


class TestTraversal:
    """Test graph traversal queries."""

    def test_traverse_returns_results(self, backend, mock_graph):
        """traverse() returns parsed results from graph query."""
        mock_graph.query.return_value.result_set = [
            ("mem-002", "Related Memory", "long-term", 8.5, 1),
            ("mem-003", "Distant Memory", "mid-term", 6.0, 2),
        ]
        results = backend.traverse("mem-001", depth=2)
        assert len(results) == 2
        assert results[0]["id"] == "mem-002"
        assert results[0]["distance"] == 1

    def test_traverse_empty(self, backend, mock_graph):
        """traverse() returns empty list when no connections."""
        mock_graph.query.return_value.result_set = []
        assert backend.traverse("isolated-mem") == []

    def test_traverse_not_initialized(self):
        """traverse() returns empty when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.traverse("mem-001") == []

    def test_get_related_returns_results(self, backend, mock_graph):
        """get_related() returns parsed results from graph query."""
        mock_graph.query.return_value.result_set = [
            ("mem-002", "Related Memory", "long-term", 8.5, 1),
            ("mem-003", "Distant Memory", "mid-term", 6.0, 2),
        ]
        results = backend.get_related("mem-001", depth=2)
        assert len(results) == 2
        assert results[0]["id"] == "mem-002"
        assert results[0]["distance"] == 1

    def test_get_related_empty(self, backend, mock_graph):
        """get_related() returns empty list when no connections."""
        mock_graph.query.return_value.result_set = []
        assert backend.get_related("isolated-mem") == []

    def test_get_related_not_initialized(self):
        """get_related() returns empty when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.get_related("mem-001") == []

    def test_traverse_clamps_depth(self, backend, mock_graph):
        """Traversal depth is clamped to 1-5."""
        mock_graph.query.return_value.result_set = []
        # depth=10 should be clamped to 5 — verify query is still issued
        backend.traverse("mem-001", depth=10)
        assert mock_graph.query.called

    def test_traverse_handles_exception(self, backend, mock_graph):
        """traverse() returns empty list on query failure."""
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.traverse("x") == []


# ═══════════════════════════════════════════════════════════
# get_lineage()
# ═══════════════════════════════════════════════════════════


class TestLineage:
    """Test PROMOTED_FROM chain traversal."""

    def test_get_lineage(self, backend, mock_graph):
        """get_lineage() returns ancestor chain."""
        mock_graph.query.return_value.result_set = [
            ("ancestor-1", "Original", "short-term", 1),
            ("ancestor-2", "First Thought", "short-term", 2),
        ]
        lineage = backend.get_lineage("promoted-mem")
        assert len(lineage) == 2
        assert lineage[0]["depth"] == 1
        assert lineage[1]["id"] == "ancestor-2"

    def test_get_lineage_empty(self, backend, mock_graph):
        """get_lineage() returns empty for base (non-promoted) memories."""
        mock_graph.query.return_value.result_set = []
        assert backend.get_lineage("base-mem") == []

    def test_get_lineage_not_initialized(self):
        """get_lineage() returns empty when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.get_lineage("any") == []

    def test_get_lineage_handles_exception(self, backend, mock_graph):
        """get_lineage() returns empty list on query failure."""
        mock_graph.query.side_effect = Exception("boom")
        assert backend.get_lineage("mem") == []


# ═══════════════════════════════════════════════════════════
# find_clusters() / get_memory_clusters()
# ═══════════════════════════════════════════════════════════


class TestClusters:
    """Test memory cluster detection."""

    def test_find_clusters_returns_hubs(self, backend, mock_graph):
        """find_clusters() returns hub nodes above min_size threshold."""
        mock_graph.query.return_value.result_set = [
            ("hub-001", "Central Memory", "long-term", 5),
        ]
        clusters = backend.find_clusters(min_size=3)
        assert len(clusters) == 1
        assert clusters[0]["id"] == "hub-001"
        assert clusters[0]["connections"] == 5

    def test_find_clusters_empty(self, backend, mock_graph):
        """find_clusters() returns empty when nothing meets threshold."""
        mock_graph.query.return_value.result_set = []
        assert backend.find_clusters() == []

    def test_get_memory_clusters(self, backend, mock_graph):
        """get_memory_clusters() finds highly connected nodes."""
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

    def test_clusters_not_initialized(self):
        """find_clusters() returns empty when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            assert fb.find_clusters() == []

    def test_clusters_handles_exception(self, backend, mock_graph):
        """find_clusters() returns empty on query failure."""
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.find_clusters() == []


# ═══════════════════════════════════════════════════════════
# search_by_tags()
# ═══════════════════════════════════════════════════════════


class TestSearchByTags:
    """Test tag-based graph search."""

    def test_search_by_tags_returns_results(self, backend, mock_graph):
        """search_by_tags() returns matching memories with overlap counts."""
        mock_graph.query.return_value.result_set = [
            ("mem-001", "Seed Memory", "long-term", 9.5, ["cloud9", "seed"], 2),
        ]
        results = backend.search_by_tags(["cloud9", "seed"])
        assert len(results) == 1
        assert results[0]["tag_overlap"] == 2

    def test_search_by_tags_empty_tags(self, backend):
        """search_by_tags() returns empty list for empty tag list."""
        assert backend.search_by_tags([]) == []

    def test_search_by_tags_not_initialized(self):
        """search_by_tags() returns empty list when not initialized."""
        backend = SKGraphBackend(url="redis://nonexistent:6379")
        assert backend.search_by_tags(["test"]) == []

    def test_search_by_tags_handles_exception(self, backend, mock_graph):
        """search_by_tags() returns empty on query failure."""
        mock_graph.query.side_effect = Exception("boom")
        assert backend.search_by_tags(["cloud9"]) == []


# ═══════════════════════════════════════════════════════════
# stats()
# ═══════════════════════════════════════════════════════════


class TestStats:
    """Test graph statistics reporting."""

    def test_stats_returns_counts(self, backend, mock_graph):
        """stats() returns node_count, edge_count, memory_count, tag_distribution."""
        results = [
            MagicMock(result_set=[[42]]),  # COUNT_NODES
            MagicMock(result_set=[[100]]),  # COUNT_EDGES
            MagicMock(result_set=[[30]]),  # COUNT_MEMORIES
            MagicMock(
                result_set=[  # TAG_DISTRIBUTION
                    ("cloud9", 15),
                    ("seed", 10),
                ]
            ),
        ]
        mock_graph.query.side_effect = results

        stats = backend.stats()
        assert stats["ok"] is True
        assert stats["node_count"] == 42
        assert stats["edge_count"] == 100
        assert stats["memory_count"] == 30
        assert len(stats["tag_distribution"]) == 2
        assert stats["tag_distribution"][0]["tag"] == "cloud9"
        assert stats["tag_distribution"][0]["memory_count"] == 15

    def test_stats_not_initialized(self):
        """stats() returns ok=False when not initialized."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            result = fb.stats()
        assert result["ok"] is False

    def test_stats_handles_exception(self, backend, mock_graph):
        """stats() returns ok=False on query failure."""
        mock_graph.query.side_effect = Exception("timeout")
        result = backend.stats()
        assert result["ok"] is False
        assert "timeout" in result["error"]

    def test_stats_empty_graph(self, backend, mock_graph):
        """stats() handles an empty graph gracefully."""
        results = [
            MagicMock(result_set=[[0]]),  # COUNT_NODES
            MagicMock(result_set=[[0]]),  # COUNT_EDGES
            MagicMock(result_set=[[0]]),  # COUNT_MEMORIES
            MagicMock(result_set=[]),  # TAG_DISTRIBUTION — empty
        ]
        mock_graph.query.side_effect = results

        stats = backend.stats()
        assert stats["ok"] is True
        assert stats["node_count"] == 0
        assert stats["tag_distribution"] == []


# ═══════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════


class TestHealthCheck:
    """Test SKGraph health reporting."""

    def test_health_ok(self, backend, mock_graph):
        """Healthy backend reports ok=True with node count."""
        mock_graph.query.return_value.result_set = [[42]]
        health = backend.health_check()
        assert health["ok"] is True
        assert health["node_count"] == 42
        assert health["backend"] == "SKGraphBackend"

    def test_health_not_initialized(self):
        """Uninitialized backend reports ok=False."""
        fb = SKGraphBackend()
        with patch.object(fb, "_ensure_initialized", return_value=False):
            health = fb.health_check()
        assert health["ok"] is False

    def test_health_query_failure(self, backend, mock_graph):
        """Health check with query failure reports error."""
        mock_graph.query.side_effect = Exception("boom")
        health = backend.health_check()
        assert health["ok"] is False
        assert "boom" in health["error"]

    def test_health_includes_url_and_graph(self, backend, mock_graph):
        """Healthy health check includes url and graph name."""
        mock_graph.query.return_value.result_set = [[0]]
        health = backend.health_check()
        assert "url" in health
        assert "graph" in health


# ═══════════════════════════════════════════════════════════
# Error resilience across all query methods
# ═══════════════════════════════════════════════════════════


class TestQueryFailureResilient:
    """Verify all query methods degrade gracefully on exception."""

    def test_all_query_methods_handle_exception(self, backend, mock_graph):
        """All read methods return safe defaults when queries fail."""
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.get_related("x") == []
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.get_lineage("x") == []
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.get_memory_clusters() == []
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.search("x") == []
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.search_by_tags(["x"]) == []
        mock_graph.query.side_effect = Exception("timeout")
        assert backend.find_clusters() == []
