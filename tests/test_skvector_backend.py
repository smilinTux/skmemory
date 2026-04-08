"""Tests for the SKVector (Qdrant) vector search backend.

Mocks the Qdrant client and sentence-transformers to test
logic without requiring infrastructure. Verifies save, search,
list, delete, and health check operations.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skmemory.backends.skvector_backend import (
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    MODEL_DIMENSIONS,
    VECTOR_DIM,
    SKVectorBackend,
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
        qb = SKVectorBackend(embedding_model="bge-large")
        assert qb.embedding_model_name == "BAAI/bge-large-en-v1.5"
        assert qb.vector_dim == MODEL_DIMENSIONS["bge-large"]

    def test_default_model_prefers_local_hammertime_path(self):
        """The default sovereign model should resolve to the local HammerTime path here."""
        qb = SKVectorBackend()
        assert qb.requested_embedding_model == "bge-legal-v1"
        assert "hammerTime/models/bge-legal-v1" in qb.embedding_model_name


# ═══════════════════════════════════════════════════════════
# Save
# ═══════════════════════════════════════════════════════════


class TestSave:
    """Test memory indexing in Qdrant."""

    def test_save_calls_upsert(self, backend, mock_qdrant_client, sample_memory):
        """save() creates a point and upserts it."""
        result = backend.save(sample_memory)
        assert result == sample_memory.id
        mock_qdrant_client.upsert.assert_called_once()

    def test_save_generates_embedding(self, backend, mock_embedder, sample_memory):
        """save() generates an embedding from the memory text."""
        backend.save(sample_memory)
        mock_embedder.encode.assert_called_once()

    def test_save_not_initialized(self, sample_memory):
        """save() returns id gracefully when not initialized."""
        qb = SKVectorBackend()
        result = qb.save(sample_memory)
        assert result == sample_memory.id


# ═══════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════


class TestSearch:
    """Test semantic search."""

    def test_search_text_generates_embedding(self, backend, mock_embedder):
        """search_text embeds the query before searching."""
        backend.search_text("moments of connection")
        mock_embedder.encode.assert_called_once_with("moments of connection")

    def test_search_text_calls_qdrant_search(self, backend, mock_qdrant_client):
        """search_text uses Qdrant's search endpoint."""
        backend.search_text("test query", limit=5)
        mock_qdrant_client.search.assert_called_once()

    def test_search_text_returns_memories(self, backend, mock_qdrant_client, sample_memory):
        """search_text parses results into Memory objects."""
        scored_point = MagicMock()
        scored_point.payload = {"memory_json": sample_memory.model_dump_json()}
        mock_qdrant_client.search.return_value = [scored_point]

        results = backend.search_text("secret recipe")
        assert len(results) == 1
        assert results[0].title == "The Secret Recipe"

    def test_search_text_empty_results(self, backend, mock_qdrant_client):
        """search_text returns empty list when nothing matches."""
        mock_qdrant_client.search.return_value = []
        assert backend.search_text("nonexistent") == []

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
