"""
SKVector — semantic vector search backend (Level 1).

Powered by Qdrant. Enables semantic memory recall: instead of exact text
matching, find memories by *meaning*. "That conversation where we felt
connected" finds the right memory even if those exact words aren't in it.

Requires:
    pip install skmemory[skvector]

Qdrant free tier: 1GB storage, 256MB RAM -- enough for thousands of memories.
SaaS endpoint: https://cloud.qdrant.io (free cluster available).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)

COLLECTION_NAME = "skmemory"
EMBEDDING_MODEL = "bge-legal-v1"
VECTOR_DIM = 1024
HAMMERTIME_HF_MODEL = "chefboyrave21/bge-legal-v1"
PUBLIC_FALLBACK_MODEL = "BAAI/bge-large-en-v1.5"

MODEL_DIMENSIONS = {
    "all-MiniLM-L6-v2": 384,
    "bge-legal-v1": 1024,
    HAMMERTIME_HF_MODEL: 1024,
    "BAAI/bge-large-en-v1.5": 1024,
    "bge-large": 1024,
}

MODEL_ALIASES = {
    "bge-large": PUBLIC_FALLBACK_MODEL,
}


def _candidate_local_model_paths(model_name: str) -> list[Path]:
    """Return plausible local model directories for sovereign embeddings."""
    if model_name not in {"bge-legal-v1", HAMMERTIME_HF_MODEL}:
        return []

    candidates: list[Path] = []
    hammertime_root = os.environ.get("HAMMERTIME_ROOT")
    if hammertime_root:
        candidates.append(Path(hammertime_root) / "models" / "bge-legal-v1")
    candidates.append(Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime/models/bge-legal-v1"))
    return candidates


def _resolve_embedding_model_name(model_name: str) -> str:
    """Resolve aliases and sovereign local-path fallbacks for embedding models."""
    normalized = MODEL_ALIASES.get(model_name, model_name)

    for candidate in _candidate_local_model_paths(normalized):
        if candidate.exists():
            return str(candidate)

    if normalized in {"bge-legal-v1", HAMMERTIME_HF_MODEL}:
        if os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN"):
            return HAMMERTIME_HF_MODEL
        return PUBLIC_FALLBACK_MODEL

    return normalized


def _extract_status_code(exc: Exception, unexpected_cls: type | None) -> int | None:
    """Pull an HTTP status code from a qdrant-client exception.

    Works across qdrant-client versions: checks ``status_code`` attr first,
    then falls back to the string representation for patterns like ``401``.
    """
    code = getattr(exc, "status_code", None)
    if code is not None:
        return int(code)
    if unexpected_cls is not None and isinstance(exc, unexpected_cls):
        code = getattr(exc, "status_code", None)
        if code is not None:
            return int(code)
    text = str(exc)
    for candidate in (401, 403):
        if str(candidate) in text:
            return candidate
    return None


class SKVectorBackend(BaseBackend):
    """SKVector — semantic memory search (powered by Qdrant).

    Stores memory embeddings for vector similarity search.
    Falls back gracefully if the vector engine or the embedding model
    is unavailable.

    Args:
        url: SKVector server URL (default: localhost:6333).
        api_key: API key for cloud-hosted SKVector.
        collection: Collection name (default: 'skmemory').
        embedding_model: Sentence-transformers model name.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection: str = COLLECTION_NAME,
        embedding_model: str = EMBEDDING_MODEL,
        vector_dim: int | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.collection = collection
        self.requested_embedding_model = embedding_model
        self.embedding_model_name = _resolve_embedding_model_name(embedding_model)
        self.vector_dim = vector_dim or MODEL_DIMENSIONS.get(embedding_model, VECTOR_DIM)
        self._client = None
        self._embedder = None
        self._initialized = False
        self._last_error: str | None = None

    def _ensure_initialized(self) -> bool:
        """Lazy-initialize Qdrant client and embedding model.

        Returns:
            bool: True if initialization succeeded.
        """
        if self._initialized:
            return True

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError:
            logger.warning("qdrant-client not installed: pip install skmemory[skvector]")
            return False

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning("sentence-transformers not installed: pip install skmemory[skvector]")
            return False

        try:
            from qdrant_client.http.exceptions import (
                UnexpectedResponse,
            )
        except ImportError:
            UnexpectedResponse = None

        try:
            self._client = QdrantClient(url=self.url, api_key=self.api_key)
            self._embedder = SentenceTransformer(self.embedding_model_name)
            get_dim = getattr(self._embedder, "get_sentence_embedding_dimension", None)
            if callable(get_dim):
                resolved_dim = get_dim()
                if isinstance(resolved_dim, int) and resolved_dim > 0:
                    self.vector_dim = resolved_dim
            collections = [c.name for c in self._client.get_collections().collections]

            if self.collection not in collections:
                self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(
                        size=self.vector_dim,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection: %s", self.collection)
            self._initialized = True
            return True

        except Exception as e:
            status = _extract_status_code(e, UnexpectedResponse)
            if status in (401, 403):
                hint = (
                    "SKVector authentication failed (HTTP %d). "
                    "Check your API key:\n"
                    "  - CLI:  --skvector-key YOUR_KEY\n"
                    "  - Env:  SKMEMORY_SKVECTOR_KEY=YOUR_KEY\n"
                    "  - Code: SKVectorBackend(url=..., api_key='YOUR_KEY')"
                )
                logger.error(hint, status)
                self._last_error = hint % status
            else:
                logger.warning("SKVector initialization failed: %s", e)
                self._last_error = str(e)
            return False

    def _embed(self, text: str) -> list[float]:
        """Generate an embedding vector for text.

        Args:
            text: The text to embed.

        Returns:
            list[float]: The embedding vector.
        """
        if self._embedder is None:
            return []
        return self._embedder.encode(text).tolist()

    def _memory_to_payload(self, memory: Memory) -> dict:
        """Convert a Memory to a Qdrant payload dict.

        Args:
            memory: The memory to convert.

        Returns:
            dict: Payload suitable for Qdrant upsert.
        """
        return {
            "memory_json": memory.model_dump_json(),
            "title": memory.title,
            "layer": memory.layer.value,
            "tags": memory.tags,
            "source": memory.source,
            "created_at": memory.created_at,
            "emotional_intensity": memory.emotional.intensity,
            "emotional_valence": memory.emotional.valence,
            "emotional_labels": memory.emotional.labels,
        }

    def save(self, memory: Memory) -> str:
        """Index a memory in Qdrant.

        Args:
            memory: The Memory to index.

        Returns:
            str: The memory ID.
        """
        if not self._ensure_initialized():
            return memory.id

        from qdrant_client.models import PointStruct

        embedding = self._embed(memory.to_embedding_text())
        if not embedding:
            return memory.id

        # Use memory.id hash as Qdrant point ID (not content_hash which
        # would collide if two memories have identical content).
        point_id = int(hashlib.sha256(memory.id.encode()).hexdigest()[:15], 16)
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload=self._memory_to_payload(memory),
        )

        self._client.upsert(
            collection_name=self.collection,
            points=[point],
        )
        return memory.id

    def _id_to_point_id(self, memory_id: str) -> int:
        """Convert a memory ID string to a deterministic Qdrant integer point ID."""
        return int(hashlib.sha256(memory_id.encode()).hexdigest()[:15], 16)

    def load(self, memory_id: str) -> Memory | None:
        """Retrieve a memory by ID from Qdrant.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found.
        """
        if not self._ensure_initialized():
            return None

        try:
            points = self._client.retrieve(
                collection_name=self.collection,
                ids=[self._id_to_point_id(memory_id)],
                with_payload=True,
            )
            if not points:
                return None
            return Memory.model_validate_json(points[0].payload["memory_json"])
        except Exception:
            return None

    def delete(self, memory_id: str) -> bool:
        """Remove a memory from Qdrant by its deterministic point ID.

        Returns False if the memory was not found.

        Args:
            memory_id: The memory identifier.

        Returns:
            bool: True if the memory existed and was deleted, False otherwise.
        """
        if not self._ensure_initialized():
            return False

        try:
            points = self._client.retrieve(
                collection_name=self.collection,
                ids=[self._id_to_point_id(memory_id)],
                with_payload=False,
            )
            if not points:
                return False
            self._client.delete(
                collection_name=self.collection,
                points_selector=[self._id_to_point_id(memory_id)],
            )
            return True
        except Exception:
            return False

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories from Qdrant with filtering.

        Args:
            layer: Filter by layer.
            tags: Filter by tags.
            limit: Max results.

        Returns:
            list[Memory]: Matching memories.
        """
        if not self._ensure_initialized():
            return []

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must_conditions = []
        if layer:
            must_conditions.append(
                FieldCondition(key="layer", match=MatchValue(value=layer.value))
            )
        if tags:
            for tag in tags:
                must_conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag)))

        scroll_filter = Filter(must=must_conditions) if must_conditions else None

        results = self._client.scroll(
            collection_name=self.collection,
            scroll_filter=scroll_filter,
            limit=limit,
        )

        points = results[0] if results else []
        memories = []
        for point in points:
            try:
                mem = Memory.model_validate_json(point.payload["memory_json"])
                memories.append(mem)
            except Exception:
                continue

        memories.sort(key=lambda m: m.created_at, reverse=True)
        return memories

    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        """Semantic search: find memories by meaning, not exact text.

        Args:
            query: Natural language query.
            limit: Max results.

        Returns:
            list[Memory]: Memories ranked by semantic similarity.
        """
        if not self._ensure_initialized():
            return []

        embedding = self._embed(query)
        if not embedding:
            return []

        results = self._client.search(
            collection_name=self.collection,
            query_vector=embedding,
            limit=limit,
        )

        memories = []
        for scored_point in results:
            try:
                mem = Memory.model_validate_json(scored_point.payload["memory_json"])
                memories.append(mem)
            except Exception:
                continue

        return memories

    def health_check(self) -> dict:
        """Check Qdrant backend health.

        Returns:
            dict: Status with connection and collection info.
        """
        if not self._ensure_initialized():
            error_msg = self._last_error or (
                "Not initialized (missing dependencies or connection failed)"
            )
            return {
                "ok": False,
                "backend": "SKVectorBackend",
                "error": error_msg,
            }

        try:
            info = self._client.get_collection(self.collection)
            return {
                "ok": True,
                "backend": "SKVectorBackend",
                "url": self.url,
                "collection": self.collection,
                "requested_embedding_model": self.requested_embedding_model,
                "embedding_model": self.requested_embedding_model,
                "resolved_embedding_model": self.embedding_model_name,
                "vector_dim": self.vector_dim,
                "points_count": getattr(info, "points_count", None),
                "vectors_count": getattr(info, "vectors_count", None),
            }
        except Exception as e:
            return {
                "ok": False,
                "backend": "SKVectorBackend",
                "error": str(e),
            }
