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
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)

COLLECTION_NAME = "skmemory"
EMBEDDING_MODEL = "bge-legal-v2"
VECTOR_DIM = 1024
HAMMERTIME_HF_MODEL = "chefboyrave21/bge-legal-v2"
PUBLIC_FALLBACK_MODEL = "BAAI/bge-large-en-v1.5"

MODEL_DIMENSIONS = {
    "all-MiniLM-L6-v2": 384,
    "bge-legal-v2": 1024,
    HAMMERTIME_HF_MODEL: 1024,
    "BAAI/bge-large-en-v1.5": 1024,
    "bge-large": 1024,
}

MODEL_ALIASES = {
    "bge-large": PUBLIC_FALLBACK_MODEL,
}


def _legacy_payload_memory_id(payload: dict) -> str:
    """Derive a deterministic memory id from a legacy payload."""
    for key in ("id", "memory_id"):
        value = payload.get(key)
        if value:
            return str(value)

    basis = [
        payload.get("file_path"),
        payload.get("parent_doc"),
        payload.get("filename"),
        payload.get("section_title"),
        payload.get("title"),
        payload.get("content_preview"),
        payload.get("summary"),
        payload.get("source"),
        payload.get("chunk_index"),
    ]
    sep = chr(124)
    stable = sep.join("" if value is None else str(value) for value in basis)
    if not stable.strip(sep):
        stable = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def _candidate_local_model_paths(model_name: str) -> list[Path]:
    """Return plausible local model directories for sovereign embeddings."""
    if model_name not in {"bge-legal-v2", HAMMERTIME_HF_MODEL}:
        return []

    candidates: list[Path] = []
    hammertime_root = os.environ.get("HAMMERTIME_ROOT")
    if hammertime_root:
        candidates.append(Path(hammertime_root) / "models" / "bge-legal-v2")
    candidates.append(Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime/models/bge-legal-v2"))
    return candidates


def _resolve_embedding_model_name(model_name: str) -> str:
    """Resolve aliases and sovereign local-path fallbacks for embedding models."""
    normalized = MODEL_ALIASES.get(model_name, model_name)

    for candidate in _candidate_local_model_paths(normalized):
        if candidate.exists():
            return str(candidate)

    if normalized in {"bge-legal-v2", HAMMERTIME_HF_MODEL}:
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


class VectorStateTracker:
    """Tracks which memories have been vector-indexed and their content hashes.
    Stored at: ~/.skcapstone/agents/{agent}/memory/vector-state.json
    """

    def __init__(self, state_path: Path):
        self.path = state_path
        self._state: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text()).get("memories", {})
            except Exception as e:
                logger.warning("skvector_backend.py: %s", e)
                self._state = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"memories": self._state}, indent=2))

    def record(self, memory_id: str, content_hash: str, point_id: int, chunk_count: int = 1):
        self._state[memory_id] = {
            "content_hash": content_hash,
            "point_id": point_id,
            "chunk_count": chunk_count,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def remove(self, memory_id: str):
        self._state.pop(memory_id, None)
        self._save()

    def get(self, memory_id: str) -> dict | None:
        return self._state.get(memory_id)

    def is_current(self, memory_id: str, content_hash: str) -> bool:
        entry = self._state.get(memory_id)
        return entry is not None and entry.get("content_hash") == content_hash

    def all_ids(self) -> set[str]:
        return set(self._state.keys())


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
        state_path: Optional path to vector-state.json for state tracking.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection: str = COLLECTION_NAME,
        embedding_model: str = EMBEDDING_MODEL,
        vector_dim: int | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.collection = collection
        self.requested_embedding_model = embedding_model
        self.embedding_model_name = _resolve_embedding_model_name(embedding_model)
        self.vector_dim = vector_dim or MODEL_DIMENSIONS.get(embedding_model, VECTOR_DIM)
        self._client = None
        self._embedder = None
        self._embed_fn = embed_fn  # optional external embedding function (e.g. Ollama)
        self._initialized = False
        self._last_error: str | None = None
        self._tracker: VectorStateTracker | None = None
        if state_path is not None:
            self._tracker = VectorStateTracker(state_path)

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
            from qdrant_client.http.exceptions import (
                UnexpectedResponse,
            )
        except ImportError:
            UnexpectedResponse = None

        # If an external embed_fn is provided, skip SentenceTransformer loading.
        if self._embed_fn is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                logger.warning(
                    "sentence-transformers not installed: pip install skmemory[skvector]"
                )
                return False

        try:
            self._client = QdrantClient(url=self.url, api_key=self.api_key)

            if self._embed_fn is None:
                self._embedder = SentenceTransformer(self.embedding_model_name)
                # get_embedding_dimension (new API) or get_sentence_embedding_dimension (legacy)
                get_dim = getattr(self._embedder, "get_embedding_dimension",
                          getattr(self._embedder, "get_sentence_embedding_dimension", None))
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
            logger.warning("skvector_backend.py: %s", e)
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

        Uses the injected embed_fn if provided (e.g. Ollama), otherwise
        falls back to the sentence-transformers model.

        Args:
            text: The text to embed.

        Returns:
            list[float]: The embedding vector.
        """
        if self._embed_fn is not None:
            return self._embed_fn(text)
        if self._embedder is None:
            return []
        return self._embedder.encode(text).tolist()

    def _memory_to_payload(self, memory: Memory) -> dict:
        """Convert a Memory to a Qdrant payload dict.

        Includes top-level filterable fields so Qdrant queries can filter
        without parsing memory_json.

        Args:
            memory: The memory to convert.

        Returns:
            dict: Payload suitable for Qdrant upsert.
        """
        metadata = memory.metadata or {}
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
            # Additional top-level filterable fields for recall corpora and legacy-compatible scans
            "content_preview": memory.content[:500],
            "content_hash": memory.content_hash(),
            "is_chunk": bool(memory.parent_id),
            "chunk_index": metadata.get("decomposition", {}).get("chunk_index", 0),
            "total_chunks": metadata.get("decomposition", {}).get("total_chunks", 1),
            "parent_id": memory.parent_id or "",
            "section_title": metadata.get("decomposition", {}).get("section_title", ""),
            "authority_tier": metadata.get("authority_tier", "memory"),
            "role": memory.role.value if hasattr(memory.role, "value") else str(memory.role),
            "file_path": metadata.get("file_path", ""),
            "filename": metadata.get("filename", ""),
            "type": metadata.get("type", ""),
            "category": metadata.get("category", ""),
            "parent_doc": metadata.get("parent_doc", ""),
        }

    def _memory_from_payload(self, payload: dict) -> Memory:
        """Build a Memory from either current or legacy Qdrant payloads."""
        raw = payload.get("memory_json")
        if raw:
            return Memory.model_validate_json(raw)

        layer_raw = payload.get("layer") or payload.get("tier") or MemoryLayer.SHORT.value
        try:
            layer = MemoryLayer(layer_raw)
        except Exception:
            layer = MemoryLayer.LONG if str(layer_raw).startswith("long") else MemoryLayer.SHORT

        emotions = payload.get("emotional_labels") or payload.get("emotions") or []
        if isinstance(emotions, str):
            emotions = [emotions]
        elif not isinstance(emotions, list):
            emotions = []

        tags = payload.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]

        title = (
            payload.get("title")
            or payload.get("filename")
            or payload.get("file_path")
            or payload.get("source")
            or "Legacy vector memory"
        )
        content = (
            payload.get("content")
            or payload.get("content_preview")
            or payload.get("summary")
            or payload.get("file_path")
            or payload.get("section_title")
            or title
        )

        known_keys = {
            "memory_json", "title", "layer", "tier", "tags", "source", "created_at",
            "emotional_intensity", "intensity", "emotional_valence", "emotional_labels",
            "emotions", "content", "content_preview", "summary", "role", "file_path",
            "filename", "type", "category", "is_chunk", "chunk_index", "total_chunks",
            "section_title", "parent_id", "parent_doc", "authority_tier",
        }
        metadata = {k: v for k, v in payload.items() if k not in known_keys}
        for key in ("file_path", "filename", "type", "category", "parent_doc", "authority_tier"):
            if payload.get(key) is not None:
                metadata.setdefault(key, payload.get(key))


        from ..retrieval import prepare_metadata
        source_ref = payload.get("parent_doc") or payload.get("file_path") or payload.get("filename") or ""
        metadata = prepare_metadata(
            title=title,
            source=payload.get("source") or payload.get("type") or "skvector",
            source_ref=str(source_ref),
            tags=tags,
            metadata=metadata,
        )
        return Memory(
            id=_legacy_payload_memory_id(payload),
            title=title,
            content=content,
            layer=layer,
            source_ref=str(source_ref),
            parent_id=payload.get("parent_id") or payload.get("parent_doc"),
            source=payload.get("source") or payload.get("type") or "skvector",
            created_at=payload.get("created_at") or datetime.now(timezone.utc).isoformat(),
            emotional={
                "intensity": payload.get("emotional_intensity", payload.get("intensity", 0.0)),
                "valence": payload.get("emotional_valence", 0.0),
                "labels": emotions,
            },
            metadata=metadata,
        )

    def _check_duplicate(self, content_hash: str) -> str | None:
        """Return existing point's memory_id if duplicate content exists.

        Args:
            content_hash: SHA-256 hash prefix of the content.

        Returns:
            str | None: The memory ID of the existing duplicate, or None.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            results, _ = self._client.scroll(
                collection_name=self.collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="content_hash", match=MatchValue(value=content_hash))]
                ),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if results:
                raw = results[0].payload.get("memory_json")
                if raw:
                    return json.loads(raw).get("id")
        except Exception as e:
            logger.warning("Duplicate check failed: %s", e)
        return None

    def save(self, memory: Memory) -> str:
        """Index a memory in Qdrant.

        Skips re-embedding if identical content already exists (dedup guard).

        Args:
            memory: The Memory to index.

        Returns:
            str: The memory ID.
        """
        if not self._ensure_initialized():
            return memory.id

        # Change 2: content-hash dedup guard
        content_hash = memory.content_hash()
        existing_id = self._check_duplicate(content_hash)
        if existing_id and existing_id != memory.id:
            logger.info(
                "SKVector: duplicate content detected for '%s', skipping re-embed", memory.title
            )
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

        # Change 4: record in state tracker if configured
        if self._tracker is not None:
            self._tracker.record(memory.id, content_hash, point_id)

        return memory.id

    def _id_to_point_id(self, memory_id: str) -> int:
        """Convert a memory ID string to a deterministic Qdrant integer point ID."""
        return int(hashlib.sha256(memory_id.encode()).hexdigest()[:15], 16)

    # Alias for spec compatibility
    _memory_id_to_point_id = _id_to_point_id

    def is_indexed(self, memory_id: str, content_hash: str) -> bool:
        """Check if a memory is currently indexed with the given content hash.

        Uses the state tracker if available; otherwise returns False.

        Args:
            memory_id: The memory identifier.
            content_hash: Expected content hash.

        Returns:
            bool: True if indexed and up-to-date.
        """
        if self._tracker is None:
            return False
        return self._tracker.is_current(memory_id, content_hash)

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
            return self._memory_from_payload(points[0].payload)
        except Exception as e:
            logger.warning("skvector_backend.py: %s", e)
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
        except Exception as e:
            logger.warning("skvector_backend.py: %s", e)
            return False

    def remove(self, memory_id: str) -> bool:
        """Remove a memory and all its chunks from Qdrant.

        Deletes the main point by ID and all chunk points where
        parent_id matches memory_id (cascade delete).

        Args:
            memory_id: The memory identifier.

        Returns:
            bool: True if successful.
        """
        if not self._ensure_initialized():
            return False

        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            # Delete the main point
            point_id = self._id_to_point_id(memory_id)
            self._client.delete(
                collection_name=self.collection, points_selector=[point_id]
            )
            # Change 3: delete all chunk points where parent_id matches
            self._client.delete(
                collection_name=self.collection,
                points_selector=Filter(
                    must=[FieldCondition(key="parent_id", match=MatchValue(value=memory_id))]
                ),
            )

            # Change 4: remove from state tracker if configured
            if self._tracker is not None:
                self._tracker.remove(memory_id)

            return True
        except Exception as e:
            logger.warning("SKVector remove failed: %s", e)
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
                mem = self._memory_from_payload(point.payload)
                memories.append(mem)
            except Exception as e:
                logger.warning("skvector_backend.py: %s", e)
                continue

        memories.sort(key=lambda m: m.created_at, reverse=True)
        return memories

    def search_text(
        self,
        query: str,
        limit: int = 10,
        layer: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        is_chunk: bool | None = None,
        authority_tier: str | None = None,
    ) -> list[Memory]:
        """Semantic search: find memories by meaning, not exact text.

        Change 6: optional filter params to narrow results by layer,
        tags, source, chunk status, or authority tier.

        Args:
            query: Natural language query.
            limit: Max results.
            layer: Filter by layer value (e.g. 'short-term').
            tags: Filter by tags (AND logic).
            source: Filter by source value.
            is_chunk: If True, return only chunks; if False, exclude chunks.
            authority_tier: Filter by authority_tier payload field.

        Returns:
            list[Memory]: Memories ranked by semantic similarity.
        """
        if not self._ensure_initialized():
            return []

        embedding = self._embed(query)
        if not embedding:
            return []

        query_filter = None
        must_conditions = []

        from qdrant_client.models import FieldCondition, Filter, MatchValue
        if layer is not None:
            layer_value = layer.value if hasattr(layer, "value") else layer
            must_conditions.append(
                FieldCondition(key="layer", match=MatchValue(value=layer_value))
            )
        if tags:
            for tag in tags:
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )
        if source is not None:
            must_conditions.append(
                FieldCondition(key="source", match=MatchValue(value=source))
            )
        if is_chunk is not None:
            must_conditions.append(
                FieldCondition(key="is_chunk", match=MatchValue(value=is_chunk))
            )
        if authority_tier is not None:
            must_conditions.append(
                FieldCondition(key="authority_tier", match=MatchValue(value=authority_tier))
            )

        if must_conditions:
            query_filter = Filter(must=must_conditions)

        results = self._client.query_points(
            collection_name=self.collection,
            query=embedding,
            limit=limit,
            query_filter=query_filter,
        ).points

        memories = []
        for scored_point in results:
            try:
                mem = self._memory_from_payload(scored_point.payload)
                memories.append(mem)
            except Exception as e:
                logger.warning("skvector_backend.py: %s", e)
                continue

        return memories

    def sync_all(self, flat_files_dir: Path, agent_name: str) -> dict:
        """Incrementally sync all flat-file memories to vector index.

        Only processes memories where content has changed or not yet indexed.
        Removes vector entries for memories that no longer have flat files.

        Args:
            flat_files_dir: Root memory directory containing short-term/,
                            mid-term/, long-term/ subdirs.
            agent_name: Agent name (used for logging).

        Returns:
            dict: {"indexed": N, "skipped": N, "removed": N, "errors": N}
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "errors": 0}

        if not self._ensure_initialized():
            logger.warning("SKVector sync_all: backend not initialized for agent %s", agent_name)
            return stats

        # Collect all flat-file memory IDs
        flat_memory_ids: set[str] = set()
        subdirs = ["short-term", "mid-term", "long-term"]

        for subdir in subdirs:
            tier_dir = flat_files_dir / subdir
            if not tier_dir.exists():
                continue
            for json_file in tier_dir.glob("*.json"):
                try:
                    raw = json_file.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    memory = Memory.model_validate(data)
                    flat_memory_ids.add(memory.id)

                    content_hash = memory.content_hash()
                    if self._tracker is not None and self._tracker.is_current(
                        memory.id, content_hash
                    ):
                        stats["skipped"] += 1
                        continue

                    self.save(memory)
                    stats["indexed"] += 1
                except Exception as e:
                    logger.warning(
                        "SKVector sync_all: failed to process %s: %s", json_file.name, e
                    )
                    stats["errors"] += 1

        # Remove stale tracker entries (flat file gone)
        if self._tracker is not None:
            stale_ids = self._tracker.all_ids() - flat_memory_ids
            for stale_id in stale_ids:
                try:
                    self.remove(stale_id)
                    stats["removed"] += 1
                except Exception as e:
                    logger.warning(
                        "SKVector sync_all: failed to remove stale %s: %s", stale_id, e
                    )
                    stats["errors"] += 1

        logger.info(
            "SKVector sync_all for '%s': indexed=%d skipped=%d removed=%d errors=%d",
            agent_name,
            stats["indexed"],
            stats["skipped"],
            stats["removed"],
            stats["errors"],
        )
        return stats

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
            logger.warning("skvector_backend.py: %s", e)
            return {
                "ok": False,
                "backend": "SKVectorBackend",
                "error": str(e),
            }
