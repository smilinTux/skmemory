"""
Qdrant vector search backend (Level 2).

Enables semantic memory recall: instead of exact text matching,
find memories by *meaning*. "That conversation where we felt connected"
finds the right memory even if those exact words aren't in it.

Requires:
    pip install qdrant-client sentence-transformers

Qdrant free tier: 1GB storage, 256MB RAM -- enough for thousands of memories.
SaaS endpoint: https://cloud.qdrant.io (free cluster available).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)

COLLECTION_NAME = "skmemory"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384


class QdrantBackend(BaseBackend):
    """Qdrant-powered semantic memory search.

    Stores memory embeddings in Qdrant for vector similarity search.
    Falls back gracefully if Qdrant or the embedding model is unavailable.

    Args:
        url: Qdrant server URL (default: localhost:6333).
        api_key: API key for Qdrant Cloud.
        collection: Collection name (default: 'skmemory').
        embedding_model: Sentence-transformers model name.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: Optional[str] = None,
        collection: str = COLLECTION_NAME,
        embedding_model: str = EMBEDDING_MODEL,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.collection = collection
        self.embedding_model_name = embedding_model
        self._client = None
        self._embedder = None
        self._initialized = False

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
            logger.warning("qdrant-client not installed: pip install qdrant-client")
            return False

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning(
                "sentence-transformers not installed: "
                "pip install sentence-transformers"
            )
            return False

        try:
            self._client = QdrantClient(url=self.url, api_key=self.api_key)
            collections = [c.name for c in self._client.get_collections().collections]

            if self.collection not in collections:
                self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(
                        size=VECTOR_DIM,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection: %s", self.collection)

            self._embedder = SentenceTransformer(self.embedding_model_name)
            self._initialized = True
            return True

        except Exception as e:
            logger.warning("Qdrant initialization failed: %s", e)
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

        point = PointStruct(
            id=memory.content_hash(),
            vector=embedding,
            payload=self._memory_to_payload(memory),
        )

        self._client.upsert(
            collection_name=self.collection,
            points=[point],
        )
        return memory.id

    def load(self, memory_id: str) -> Optional[Memory]:
        """Retrieve a memory by ID from Qdrant.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found.

        Note:
            Qdrant uses content hashes as point IDs, so this does
            a scroll+filter. For direct ID lookup, use the file backend.
        """
        if not self._ensure_initialized():
            return None

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        results = self._client.scroll(
            collection_name=self.collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="memory_json",
                        match=MatchValue(value=memory_id),
                    )
                ]
            ),
            limit=1,
        )

        points = results[0] if results else []
        if not points:
            return None

        try:
            return Memory.model_validate_json(points[0].payload["memory_json"])
        except Exception:
            return None

    def delete(self, memory_id: str) -> bool:
        """Remove a memory from Qdrant by scrolling for it.

        Args:
            memory_id: The memory identifier.

        Returns:
            bool: True if something was deleted.
        """
        if not self._ensure_initialized():
            return False

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        # Reason: Qdrant doesn't support delete-by-payload natively,
        # so we scroll to find the point ID then delete by point ID.
        results = self._client.scroll(
            collection_name=self.collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="memory_json",
                        match=MatchValue(value=memory_id),
                    )
                ]
            ),
            limit=1,
        )

        points = results[0] if results else []
        if not points:
            return False

        self._client.delete(
            collection_name=self.collection,
            points_selector=[points[0].id],
        )
        return True

    def list_memories(
        self,
        layer: Optional[MemoryLayer] = None,
        tags: Optional[list[str]] = None,
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
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )

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
                mem = Memory.model_validate_json(
                    scored_point.payload["memory_json"]
                )
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
            return {
                "ok": False,
                "backend": "QdrantBackend",
                "error": "Not initialized (missing dependencies or connection failed)",
            }

        try:
            info = self._client.get_collection(self.collection)
            return {
                "ok": True,
                "backend": "QdrantBackend",
                "url": self.url,
                "collection": self.collection,
                "points_count": info.points_count,
                "vectors_count": info.vectors_count,
            }
        except Exception as e:
            return {
                "ok": False,
                "backend": "QdrantBackend",
                "error": str(e),
            }
