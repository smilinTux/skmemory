"""
Integration test fixtures for SKMemory live backends.

Fixtures skip automatically when SKGraph or SKVector are unreachable.
Set env vars to point at non-default endpoints:

    SKMEMORY_SKGRAPH_URL=redis://localhost:6379
    SKMEMORY_SKVECTOR_URL=http://localhost:6333

A dedicated test graph and collection are used so production data is
never touched. Both are torn down after the test session.
"""

from __future__ import annotations

import os
import uuid

import pytest

# ─────────────────────────────────────────────────────────
# Connection constants
# ─────────────────────────────────────────────────────────

SKGRAPH_URL = os.environ.get("SKMEMORY_SKGRAPH_URL", "redis://localhost:6379")
SKVECTOR_URL = os.environ.get("SKMEMORY_SKVECTOR_URL", "http://localhost:6333")
SKVECTOR_KEY = os.environ.get("SKMEMORY_SKVECTOR_KEY")

# Isolated names so tests never collide with production data
TEST_GRAPH_NAME = "skmemory_integration_test"
TEST_COLLECTION_NAME = "skmemory_integration_test"


# ─────────────────────────────────────────────────────────
# Availability checks
# ─────────────────────────────────────────────────────────


def _skgraph_available() -> bool:
    """Return True if a SKGraph (FalkorDB/Redis) server is reachable."""
    try:
        from falkordb import FalkorDB  # type: ignore[import]
    except ImportError:
        return False

    try:
        db = FalkorDB.from_url(SKGRAPH_URL)
        g = db.select_graph("__ping__")
        g.query("RETURN 1")
        return True
    except Exception:
        return False


def _skvector_available() -> bool:
    """Return True if SKVector (Qdrant) is reachable and qdrant-client is installed."""
    try:
        from qdrant_client import QdrantClient  # type: ignore[import]
    except ImportError:
        return False

    try:
        client = QdrantClient(url=SKVECTOR_URL, api_key=SKVECTOR_KEY)
        client.get_collections()
        return True
    except Exception:
        return False


def _sentence_transformers_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401  # type: ignore[import]
        return True
    except ImportError:
        return False


SKGRAPH_AVAILABLE = _skgraph_available()
SKVECTOR_AVAILABLE = _skvector_available()
SENTENCE_TRANSFORMERS_AVAILABLE = _sentence_transformers_available()

# Composite flag: SKVector tests also require the embedding model
SKVECTOR_FULL_AVAILABLE = SKVECTOR_AVAILABLE and SENTENCE_TRANSFORMERS_AVAILABLE

requires_skgraph = pytest.mark.skipif(
    not SKGRAPH_AVAILABLE,
    reason="SKGraph unreachable (set SKMEMORY_SKGRAPH_URL or start Redis+FalkorDB)",
)

requires_skvector = pytest.mark.skipif(
    not SKVECTOR_FULL_AVAILABLE,
    reason=(
        "SKVector unreachable or sentence-transformers not installed "
        "(set SKMEMORY_SKVECTOR_URL or pip install qdrant-client sentence-transformers)"
    ),
)

requires_both = pytest.mark.skipif(
    not (SKGRAPH_AVAILABLE and SKVECTOR_FULL_AVAILABLE),
    reason="Both SKGraph and SKVector must be reachable for cross-backend tests",
)


# ─────────────────────────────────────────────────────────
# SKGraph fixtures
# ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def falkordb_backend():
    """Live SKGraphBackend pointed at the test graph.

    The test graph is deleted at session teardown.
    """
    pytest.importorskip("falkordb", reason="falkordb not installed")
    if not SKGRAPH_AVAILABLE:
        pytest.skip("SKGraph unreachable")

    from skmemory.backends.skgraph_backend import SKGraphBackend

    backend = SKGraphBackend(url=SKGRAPH_URL, graph_name=TEST_GRAPH_NAME)
    assert backend._ensure_initialized(), "SKGraph backend failed to initialize"

    yield backend

    # Teardown: drop the test graph
    try:
        from falkordb import FalkorDB  # type: ignore[import]
        db = FalkorDB.from_url(SKGRAPH_URL)
        db.select_graph(TEST_GRAPH_NAME).delete()
    except Exception:
        pass


@pytest.fixture
def falkordb_clean(falkordb_backend):
    """SKGraphBackend with test graph wiped before each test."""
    # Clear all nodes so tests are independent
    try:
        falkordb_backend._graph.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass
    return falkordb_backend


# ─────────────────────────────────────────────────────────
# SKVector fixtures
# ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def qdrant_backend():
    """Live SKVectorBackend pointed at the test collection.

    The test collection is deleted at session teardown.
    """
    pytest.importorskip("qdrant_client", reason="qdrant-client not installed")
    pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")
    if not SKVECTOR_AVAILABLE:
        pytest.skip("SKVector unreachable")

    from skmemory.backends.skvector_backend import SKVectorBackend

    backend = SKVectorBackend(
        url=SKVECTOR_URL,
        api_key=SKVECTOR_KEY,
        collection=TEST_COLLECTION_NAME,
    )
    assert backend._ensure_initialized(), "SKVector backend failed to initialize"

    yield backend

    # Teardown: delete test collection
    try:
        backend._client.delete_collection(TEST_COLLECTION_NAME)
    except Exception:
        pass


@pytest.fixture
def qdrant_clean(qdrant_backend):
    """SKVectorBackend with collection wiped before each test."""
    try:
        from qdrant_client.models import Distance, VectorParams
        from skmemory.backends.skvector_backend import VECTOR_DIM

        qdrant_backend._client.delete_collection(TEST_COLLECTION_NAME)
        qdrant_backend._client.create_collection(
            collection_name=TEST_COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
    except Exception:
        pass
    return qdrant_backend


# ─────────────────────────────────────────────────────────
# Shared memory factory
# ─────────────────────────────────────────────────────────


def make_memory(
    title: str = "Test Memory",
    content: str = "Integration test content.",
    tags: list[str] | None = None,
    source: str = "integration-test",
    layer: str = "short-term",
    intensity: float = 5.0,
    valence: float = 0.5,
    emotional_labels: list[str] | None = None,
    parent_id: str | None = None,
    related_ids: list[str] | None = None,
):
    """Factory for Memory objects in integration tests."""
    from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

    layer_enum = MemoryLayer(layer)
    emotional = EmotionalSnapshot(
        intensity=intensity,
        valence=valence,
        labels=emotional_labels or [],
    )
    return Memory(
        id=str(uuid.uuid4()),
        title=title,
        content=content,
        tags=tags or [],
        source=source,
        layer=layer_enum,
        emotional=emotional,
        parent_id=parent_id,
        related_ids=related_ids or [],
    )
