"""Tests for the SKVector (Qdrant) vector search backend.

Mocks the Qdrant client and sentence-transformers to test
logic without requiring infrastructure. Verifies save, search,
list, delete, and health check operations.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skmemory.backends.skvector_backend import (
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    MODEL_DIMENSIONS,
    VECTOR_DIM,
    SKVectorBackend,
    VectorStateTracker,
    _extract_status_code,
)
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

try:
    import qdrant_client  # noqa: F401

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not QDRANT_AVAILABLE,
    reason="qdrant-client not installed",
)


@pytest.fixture
def mock_qdrant_client():
    """Mocked Qdrant client with collection support."""
    client = MagicMock()
    collection_mock = MagicMock()
    collection_mock.name = COLLECTION_NAME
    client.get_collections.return_value.collections = [collection_mock]
    client.scroll.return_value = ([], None)
    client.search.return_value = []
    return client


@pytest.fixture
def mock_embedder():
    """Mocked sentence-transformers model."""
    import numpy as np

    embedder = MagicMock()
    embedder.encode.return_value = np.random.rand(VECTOR_DIM).astype("float32")
    return embedder


@pytest.fixture
def backend(mock_qdrant_client, mock_embedder):
    """Provide a SKVectorBackend with mocked dependencies."""
    qb = SKVectorBackend(url="http://mock:6333")
    qb._client = mock_qdrant_client
    qb._embedder = mock_embedder
    qb._initialized = True
    return qb


@pytest.fixture
def sample_memory():
    """A sample memory for testing."""
    return Memory(
        title="The Secret Recipe",
        content="Chef figured it out -- projection creates reality.",
        layer=MemoryLayer("long-term"),
        tags=["cloud9", "philosophy"],
        emotional=EmotionalSnapshot(intensity=10.0, valence=0.9),
        source="cli",
    )


# ═══════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════


class TestInitialization:
    """Test lazy initialization."""

    def test_not_initialized_by_default(self):
        """Backend starts uninitialized."""
        qb = SKVectorBackend()
        assert qb._initialized is False
        assert qb.requested_embedding_model == EMBEDDING_MODEL
        assert qb.vector_dim == VECTOR_DIM

    def test_init_fails_without_qdrant(self):
        """Fails gracefully without qdrant-client."""
        qb = SKVectorBackend()
        with patch("builtins.__import__", side_effect=ImportError):
            assert qb._ensure_initialized() is False

    def test_already_initialized_shortcuts(self, backend):
        """Second init call returns immediately."""
        assert backend._ensure_initialized() is True

    def test_model_alias_sets_expected_vector_dim(self):
        """Known model aliases should default to the right dimension."""
        qb = SKVectorBackend(embedding_model="mxbai-embed-large")
        assert qb.embedding_model_name == "mixedbread-ai/mxbai-embed-large-v1"
        assert qb.vector_dim == MODEL_DIMENSIONS["mxbai-embed-large"]

    def test_default_model_prefers_local_hammertime_path(self, tmp_path, monkeypatch):
        """The default sovereign model should resolve to the local HammerTime path when present."""
        # Create a fake hammerTime model dir so the path-resolution logic can find it
        fake_model = tmp_path / "models" / "mxbai-embed-large"
        fake_model.mkdir(parents=True)
        monkeypatch.setenv("HAMMERTIME_ROOT", str(tmp_path))
        qb = SKVectorBackend()
        assert qb.requested_embedding_model == "mxbai-embed-large"
        # Resolves to a local path containing the model directory
        assert (
            "models" in qb.embedding_model_name and "mxbai-embed-large" in qb.embedding_model_name
        )


# ═══════════════════════════════════════════════════════════
# Save
# ═══════════════════════════════════════════════════════════


class TestSave:
    """Test memory indexing in Qdrant."""

    def test_save_calls_upsert(self, backend, mock_qdrant_client, sample_memory):
        """save() creates a point and upserts it."""
        # scroll returns empty so dedup guard passes
        mock_qdrant_client.scroll.return_value = ([], None)
        result = backend.save(sample_memory)
        assert result == sample_memory.id
        mock_qdrant_client.upsert.assert_called_once()

    def test_save_generates_embedding(self, backend, mock_embedder, sample_memory):
        """save() generates an embedding from the memory text."""
        mock_qdrant_client_local = backend._client
        mock_qdrant_client_local.scroll.return_value = ([], None)
        backend.save(sample_memory)
        mock_embedder.encode.assert_called_once()

    def test_save_not_initialized(self, sample_memory):
        """save() returns id gracefully when not initialized."""
        qb = SKVectorBackend()
        result = qb.save(sample_memory)
        assert result == sample_memory.id

    def test_save_skips_duplicate_content(self, backend, mock_qdrant_client, sample_memory):
        """save() skips re-embedding when identical content already exists."""
        # Simulate a duplicate: scroll returns a point with same content_hash but different id
        dup_point = MagicMock()
        existing_memory = Memory(
            title="Existing",
            content=sample_memory.content,  # same content
        )
        dup_point.payload = {"memory_json": existing_memory.model_dump_json()}
        mock_qdrant_client.scroll.return_value = ([dup_point], None)

        result = backend.save(sample_memory)
        assert result == sample_memory.id
        mock_qdrant_client.upsert.assert_not_called()

    def test_save_does_not_skip_same_id_resave(self, backend, mock_qdrant_client, sample_memory):
        """save() re-embeds when the same memory id is saved again (update)."""
        # scroll returns same memory_id => not a duplicate, allow upsert
        dup_point = MagicMock()
        dup_point.payload = {"memory_json": sample_memory.model_dump_json()}
        mock_qdrant_client.scroll.return_value = ([dup_point], None)

        result = backend.save(sample_memory)
        assert result == sample_memory.id
        mock_qdrant_client.upsert.assert_called_once()

    def test_memory_to_payload_exposes_recall_source_fields(self, backend):
        mem = Memory(
            title="Shared corpus doc",
            content="Reference content for a shared corpus.",
            layer=MemoryLayer("long-term"),
            source="shared-corpus:nyc-docs",
            source_ref="cover-letters/README.md",
            metadata={
                "file_path": "cover-letters/README.md",
                "filename": "README.md",
                "type": "process",
                "category": "workflow",
                "parent_doc": "cover-letters/README.md",
                "decomposition": {
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "section_title": "Overview",
                },
            },
        )
        payload = backend._memory_to_payload(mem)
        assert payload["file_path"] == "cover-letters/README.md"
        assert payload["filename"] == "README.md"
        assert payload["type"] == "process"
        assert payload["category"] == "workflow"
        assert payload["parent_doc"] == "cover-letters/README.md"


# ═══════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════


class TestSearch:
    """Test semantic search."""

    def test_search_text_generates_embedding(self, backend, mock_embedder):
        """search_text embeds the query before searching."""
        backend.search_text("moments of connection")
        mock_embedder.encode.assert_called_once_with("moments of connection")

    def test_search_text_calls_qdrant_query_points(self, backend, mock_qdrant_client):
        """search_text uses Qdrant's query_points endpoint."""
        backend.search_text("test query", limit=5)
        mock_qdrant_client.query_points.assert_called_once()

    def test_search_text_returns_memories(self, backend, mock_qdrant_client, sample_memory):
        """search_text parses results into Memory objects."""
        scored_point = MagicMock()
        scored_point.payload = {"memory_json": sample_memory.model_dump_json()}
        mock_qdrant_client.query_points.return_value.points = [scored_point]

        results = backend.search_text("secret recipe")
        assert len(results) == 1
        assert results[0].title == "The Secret Recipe"

    def test_search_text_handles_legacy_payloads(self, backend, mock_qdrant_client):
        scored_point = MagicMock()
        scored_point.payload = {
            "file_path": "reference/postal/usps-imm-2025.md",
            "filename": "usps-imm-2025.md",
            "type": "document",
            "category": "document",
            "is_chunk": True,
            "chunk_index": 3602,
            "total_chunks": 3605,
            "parent_doc": "reference/postal/usps-imm-2025.md",
        }
        mock_qdrant_client.query_points.return_value.points = [scored_point]
        results = backend.search_text("postal")
        assert len(results) == 1
        assert results[0].id
        assert results[0].title == "usps-imm-2025.md"

    def test_search_text_empty_results(self, backend, mock_qdrant_client):
        """search_text returns empty list when nothing matches."""
        mock_qdrant_client.query_points.return_value.points = []
        assert backend.search_text("nonexistent") == []

    def test_search_text_with_layer_filter(self, backend, mock_qdrant_client):
        """search_text passes layer filter to Qdrant when specified."""
        mock_qdrant_client.query_points.return_value.points = []
        backend.search_text("query", layer="long-term")
        call_kwargs = mock_qdrant_client.query_points.call_args
        assert call_kwargs is not None
        # query_filter should be set when layer is provided
        assert "query_filter" in call_kwargs.kwargs or call_kwargs.args

    def test_search_text_with_tags_and_source_filters(self, backend, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value.points = []
        backend.search_text("query", tags=["cloud9", "philosophy"], source="cli")
        call_kwargs = mock_qdrant_client.query_points.call_args
        assert call_kwargs is not None
        query_filter = call_kwargs.kwargs.get("query_filter")
        assert query_filter is not None
        values = [condition.match.value for condition in query_filter.must]
        assert "cloud9" in values
        assert "philosophy" in values
        assert "cli" in values

    def test_search_text_no_filter_when_none_params(self, backend, mock_qdrant_client):
        """search_text passes no filter when all filter params are None."""
        mock_qdrant_client.query_points.return_value.points = []
        backend.search_text("query")
        call_kwargs = mock_qdrant_client.query_points.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs.get("query_filter") is None

    def test_search_not_initialized(self):
        """search_text returns empty when not initialized."""
        qb = SKVectorBackend()
        assert qb.search_text("anything") == []


# ═══════════════════════════════════════════════════════════
# List
# ═══════════════════════════════════════════════════════════


class TestList:
    """Test memory listing with filters."""

    def test_list_memories_calls_scroll(self, backend, mock_qdrant_client):
        """list_memories uses Qdrant scroll API."""
        backend.list_memories()
        mock_qdrant_client.scroll.assert_called_once()

    def test_list_memories_with_layer_filter(self, backend, mock_qdrant_client):
        """list_memories passes layer filter to Qdrant."""
        backend.list_memories(layer=MemoryLayer("long-term"))
        call_kwargs = mock_qdrant_client.scroll.call_args
        assert call_kwargs is not None

    def test_list_not_initialized(self):
        """list_memories returns empty when not initialized."""
        qb = SKVectorBackend()
        assert qb.list_memories() == []


# ═══════════════════════════════════════════════════════════
# Delete
# ═══════════════════════════════════════════════════════════


class TestDelete:
    """Test memory deletion."""

    def test_delete_not_found(self, backend, mock_qdrant_client):
        """delete returns False when memory not in Qdrant."""
        mock_qdrant_client.retrieve.return_value = []
        assert backend.delete("nonexistent") is False

    def test_delete_not_initialized(self):
        """delete returns False when not initialized."""
        qb = SKVectorBackend()
        assert qb.delete("any") is False


# ═══════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════


class TestHealth:
    """Test health check reporting."""

    def test_health_ok(self, backend, mock_qdrant_client):
        """Healthy backend returns ok=True with collection stats."""
        collection_info = MagicMock()
        collection_info.points_count = 42
        collection_info.vectors_count = 42
        mock_qdrant_client.get_collection.return_value = collection_info

        health = backend.health_check()
        assert health["ok"] is True
        assert health["points_count"] == 42
        assert health["vectors_count"] == 42
        assert health["embedding_model"] == EMBEDDING_MODEL
        assert "resolved_embedding_model" in health
        assert health["vector_dim"] == VECTOR_DIM

    def test_health_ok_without_vectors_count(self, backend, mock_qdrant_client):
        """Health check tolerates older collection info without vectors_count."""
        collection_info = MagicMock()
        collection_info.points_count = 42
        del collection_info.vectors_count
        mock_qdrant_client.get_collection.return_value = collection_info

        health = backend.health_check()
        assert health["ok"] is True
        assert health["points_count"] == 42
        assert health["vectors_count"] is None

    def test_health_not_initialized(self):
        """Uninitialized backend returns ok=False."""
        qb = SKVectorBackend()
        health = qb.health_check()
        assert health["ok"] is False

    def test_health_query_failure(self, backend, mock_qdrant_client):
        """Health check with error returns ok=False."""
        mock_qdrant_client.get_collection.side_effect = Exception("timeout")
        health = backend.health_check()
        assert health["ok"] is False
        assert "timeout" in health["error"]

    def test_health_surfaces_auth_error(self):
        """Health check surfaces 401 auth error with actionable hint."""
        qb = SKVectorBackend(url="https://cloud.qdrant.io", api_key="bad-key")
        qb._last_error = (
            "SKVector authentication failed (HTTP 401). "
            "Check your API key:\n"
            "  - CLI:  --skvector-key YOUR_KEY\n"
            "  - Env:  SKMEMORY_SKVECTOR_KEY=YOUR_KEY\n"
            "  - Code: SKVectorBackend(url=..., api_key='YOUR_KEY')"
        )
        with patch.object(qb, "_ensure_initialized", return_value=False):
            health = qb.health_check()
        assert health["ok"] is False
        assert "401" in health["error"]
        assert "API key" in health["error"]

    def test_health_generic_error_without_last_error(self):
        """Health check falls back to generic message when no _last_error."""
        qb = SKVectorBackend()
        with patch.object(qb, "_ensure_initialized", return_value=False):
            health = qb.health_check()
        assert health["ok"] is False
        assert "Not initialized" in health["error"]


# ═══════════════════════════════════════════════════════════
# Auth / Status Code Extraction
# ═══════════════════════════════════════════════════════════


class TestExtractStatusCode:
    """Test HTTP status code extraction from exceptions."""

    def test_status_code_attribute(self):
        exc = Exception("Unauthorized")
        exc.status_code = 401
        assert _extract_status_code(exc, None) == 401

    def test_status_code_from_string(self):
        exc = Exception("Unexpected Response: 401 (Unauthorized)")
        assert _extract_status_code(exc, None) == 401

    def test_forbidden_from_string(self):
        exc = Exception("HTTP 403 Forbidden")
        assert _extract_status_code(exc, None) == 403

    def test_no_status_code(self):
        exc = Exception("Connection refused")
        assert _extract_status_code(exc, None) is None

    def test_other_status_code_not_matched(self):
        exc = Exception("HTTP 500 Internal Server Error")
        assert _extract_status_code(exc, None) is None


# ═══════════════════════════════════════════════════════════
# Embedding
# ═══════════════════════════════════════════════════════════


class TestEmbedding:
    """Test the embedding generation."""

    def test_embed_returns_vector(self, backend, mock_embedder):
        """_embed returns a float list of correct dimension."""
        vector = backend._embed("test text")
        assert len(vector) == VECTOR_DIM
        assert all(isinstance(v, float) for v in vector)

    def test_embed_without_embedder(self):
        """_embed returns empty when no embedder available."""
        qb = SKVectorBackend()
        qb._embedder = None
        assert qb._embed("test") == []

    def test_memory_to_payload(self, backend, sample_memory):
        """_memory_to_payload creates correct Qdrant payload."""
        payload = backend._memory_to_payload(sample_memory)
        assert payload["title"] == "The Secret Recipe"
        assert payload["layer"] == "long-term"
        assert "cloud9" in payload["tags"]
        assert payload["emotional_intensity"] == 10.0
        # Change 1: verify new top-level fields
        assert "content_preview" in payload
        assert payload["content_preview"] == sample_memory.content[:500]
        assert "content_hash" in payload
        assert payload["content_hash"] == sample_memory.content_hash()
        assert "is_chunk" in payload
        assert payload["is_chunk"] is False  # no parent_id
        assert "chunk_index" in payload
        assert "total_chunks" in payload
        assert "parent_id" in payload
        assert payload["parent_id"] == ""
        assert "section_title" in payload
        assert "authority_tier" in payload
        assert payload["authority_tier"] == "memory"
        assert "role" in payload
        assert payload["role"] == "general"


# ═══════════════════════════════════════════════════════════
# VectorStateTracker
# ═══════════════════════════════════════════════════════════


class TestVectorStateTracker:
    """Test the VectorStateTracker state persistence."""

    def test_record_and_is_current(self, tmp_path):
        """record() then is_current() returns True for matching hash."""
        tracker = VectorStateTracker(tmp_path / "vector-state.json")
        tracker.record("mem-1", "abc123", 99)
        assert tracker.is_current("mem-1", "abc123") is True

    def test_is_current_wrong_hash(self, tmp_path):
        """is_current() returns False when hash changed."""
        tracker = VectorStateTracker(tmp_path / "vector-state.json")
        tracker.record("mem-1", "abc123", 99)
        assert tracker.is_current("mem-1", "differenthash") is False

    def test_is_current_unknown_id(self, tmp_path):
        """is_current() returns False for unseen memory_id."""
        tracker = VectorStateTracker(tmp_path / "vector-state.json")
        assert tracker.is_current("unknown", "abc") is False

    def test_remove_deletes_entry(self, tmp_path):
        """remove() deletes the tracked entry."""
        tracker = VectorStateTracker(tmp_path / "vector-state.json")
        tracker.record("mem-1", "abc123", 99)
        tracker.remove("mem-1")
        assert tracker.is_current("mem-1", "abc123") is False
        assert "mem-1" not in tracker.all_ids()

    def test_persists_across_instances(self, tmp_path):
        """State survives re-instantiation (disk persistence)."""
        state_file = tmp_path / "vector-state.json"
        t1 = VectorStateTracker(state_file)
        t1.record("mem-A", "hash1", 42)

        t2 = VectorStateTracker(state_file)
        assert t2.is_current("mem-A", "hash1") is True

    def test_all_ids(self, tmp_path):
        """all_ids() returns the set of tracked IDs."""
        tracker = VectorStateTracker(tmp_path / "vector-state.json")
        tracker.record("m1", "h1", 1)
        tracker.record("m2", "h2", 2)
        assert tracker.all_ids() == {"m1", "m2"}

    def test_corrupted_state_recovers(self, tmp_path):
        """Corrupted state file falls back to empty state."""
        state_file = tmp_path / "vector-state.json"
        state_file.write_text("not valid json")
        tracker = VectorStateTracker(state_file)
        assert tracker.all_ids() == set()


# ═══════════════════════════════════════════════════════════
# Remove (cascade delete)
# ═══════════════════════════════════════════════════════════


class TestRemove:
    """Test remove() with chunk cascade delete."""

    def test_remove_deletes_main_and_chunks(self, backend, mock_qdrant_client):
        """remove() calls delete twice: main point + chunk filter."""
        result = backend.remove("test-memory-id")
        assert result is True
        assert mock_qdrant_client.delete.call_count == 2

    def test_remove_not_initialized(self):
        """remove() returns False when not initialized."""
        qb = SKVectorBackend()
        assert qb.remove("any-id") is False

    def test_remove_updates_tracker(self, tmp_path, mock_qdrant_client, mock_embedder):
        """remove() updates state tracker when configured."""
        state_file = tmp_path / "vector-state.json"
        backend = SKVectorBackend(url="http://mock:6333", state_path=state_file)
        backend._client = mock_qdrant_client
        backend._embedder = mock_embedder
        backend._initialized = True

        tracker = backend._tracker
        tracker.record("mem-X", "hashX", 123)
        assert tracker.is_current("mem-X", "hashX") is True

        backend.remove("mem-X")
        assert tracker.is_current("mem-X", "hashX") is False


# ═══════════════════════════════════════════════════════════
# is_indexed()
# ═══════════════════════════════════════════════════════════


class TestIsIndexed:
    """Test the is_indexed() helper."""

    def test_is_indexed_without_tracker(self, backend):
        """is_indexed returns False when no tracker configured."""
        assert backend.is_indexed("any", "hash") is False

    def test_is_indexed_with_tracker(self, tmp_path, mock_qdrant_client, mock_embedder):
        """is_indexed returns True when tracker has matching entry."""
        state_file = tmp_path / "vector-state.json"
        b = SKVectorBackend(url="http://mock:6333", state_path=state_file)
        b._client = mock_qdrant_client
        b._embedder = mock_embedder
        b._initialized = True
        b._tracker.record("mem-Z", "hashZ", 99)
        assert b.is_indexed("mem-Z", "hashZ") is True
        assert b.is_indexed("mem-Z", "differenthash") is False


# ═══════════════════════════════════════════════════════════
# sync_all()
# ═══════════════════════════════════════════════════════════


class TestSyncAll:
    """Test the incremental sync_all() method."""

    def _write_memory_file(self, directory: Path, memory: Memory):
        """Helper: write a memory to a tier directory."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{memory.id}.json").write_text(memory.model_dump_json())

    def test_sync_all_indexes_new_memories(self, tmp_path, mock_qdrant_client, mock_embedder):
        """sync_all() indexes memories not yet in the tracker."""
        state_file = tmp_path / "vector-state.json"
        b = SKVectorBackend(url="http://mock:6333", state_path=state_file)
        b._client = mock_qdrant_client
        b._embedder = mock_embedder
        b._initialized = True
        mock_qdrant_client.scroll.return_value = ([], None)

        mem = Memory(title="Sync test", content="Content to sync")
        self._write_memory_file(tmp_path / "short-term", mem)

        stats = b.sync_all(tmp_path, "test-agent")
        assert stats["indexed"] == 1
        assert stats["skipped"] == 0
        assert stats["errors"] == 0

    def test_sync_all_skips_current_memories(self, tmp_path, mock_qdrant_client, mock_embedder):
        """sync_all() skips memories where content_hash matches tracker."""
        state_file = tmp_path / "vector-state.json"
        b = SKVectorBackend(url="http://mock:6333", state_path=state_file)
        b._client = mock_qdrant_client
        b._embedder = mock_embedder
        b._initialized = True

        mem = Memory(title="Already indexed", content="Same content")
        self._write_memory_file(tmp_path / "short-term", mem)
        # Pre-populate tracker with matching hash
        b._tracker.record(mem.id, mem.content_hash(), 1)

        stats = b.sync_all(tmp_path, "test-agent")
        assert stats["skipped"] == 1
        assert stats["indexed"] == 0

    def test_sync_all_removes_stale_entries(self, tmp_path, mock_qdrant_client, mock_embedder):
        """sync_all() removes tracker entries with no matching flat file."""
        state_file = tmp_path / "vector-state.json"
        b = SKVectorBackend(url="http://mock:6333", state_path=state_file)
        b._client = mock_qdrant_client
        b._embedder = mock_embedder
        b._initialized = True

        # Tracker has an entry for a memory that no longer exists on disk
        b._tracker.record("deleted-mem-id", "stale-hash", 99)

        stats = b.sync_all(tmp_path, "test-agent")
        assert stats["removed"] == 1
        assert b._tracker.get("deleted-mem-id") is None
