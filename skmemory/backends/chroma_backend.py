"""
SKChroma — local embedded vector search backend.

Zero-config semantic search powered by ChromaDB. Runs in-process with no
external server required. Each agent gets its own local ChromaDB instance
built from Syncthing-synced flat JSON files.

Replaces Qdrant as the default vector backend for per-agent memory.
Qdrant remains available for shared collections (hammertime, etc.).

Requires:
    pip install skmemory[chroma]
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..models import Memory, MemoryLayer
from ..query_sanitizer import sanitize_query
from .base import BaseBackend
from .sqlite_backend import CONTENT_PREVIEW_LENGTH

logger = logging.getLogger(__name__)

COLLECTION_NAME = "skmemory"
EMBEDDING_MODEL = "mxbai-embed-large"
VECTOR_DIM = 1024
SOVEREIGN_HF_MODEL = "mixedbread-ai/mxbai-embed-large-v1"
PUBLIC_FALLBACK_MODEL = "mixedbread-ai/mxbai-embed-large-v1"

MODEL_DIMENSIONS = {
    "all-MiniLM-L6-v2": 384,
    "mxbai-embed-large": 1024,
    SOVEREIGN_HF_MODEL: 1024,
}

MODEL_ALIASES = {
    "mxbai-embed-large": SOVEREIGN_HF_MODEL,
}


def _resolve_embedding_model_name(model_name: str) -> str:
    """Resolve aliases and sovereign local-path fallbacks."""
    import os

    normalized = MODEL_ALIASES.get(model_name, model_name)

    if normalized in {"mxbai-embed-large", SOVEREIGN_HF_MODEL}:
        hammertime_root = os.environ.get("HAMMERTIME_ROOT")
        if hammertime_root:
            candidate = Path(hammertime_root) / "models" / "mxbai-embed-large"
            if candidate.exists():
                return str(candidate)
        fallback = Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime/models/mxbai-embed-large")
        if fallback.exists():
            return str(fallback)
        return SOVEREIGN_HF_MODEL

    return normalized


class ChromaStateTracker:
    """Tracks which memories have been indexed in ChromaDB.

    Stored at: ~/.skcapstone/agents/{agent}/memory/chroma-state.json
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
                logger.warning("ChromaDB tracker failed to load state from %s: %s", self.path, e)
                self._state = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"memories": self._state}, indent=2))

    def record(self, memory_id: str, content_hash: str):
        self._state[memory_id] = {
            "content_hash": content_hash,
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


class SKChromaBackend(BaseBackend):
    """Embedded vector search backend powered by ChromaDB.

    Zero external dependencies — runs in-process, stores data locally.
    Each agent gets its own persistent ChromaDB directory.

    Args:
        persist_dir: Directory for ChromaDB storage. Defaults to
            ~/.skcapstone/agents/{agent}/memory/chroma/
        collection: Collection name (default: 'skmemory').
        embedding_model: Sentence-transformers model name.
        embed_fn: Optional external embedding function (e.g. Ollama).
        state_path: Optional path to chroma-state.json for sync tracking.
    """

    def __init__(
        self,
        persist_dir: str | Path | None = None,
        collection: str = COLLECTION_NAME,
        embedding_model: str = EMBEDDING_MODEL,
        vector_dim: int | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.persist_dir = str(persist_dir) if persist_dir else None
        self.collection_name = collection
        self.requested_embedding_model = embedding_model
        self.embedding_model_name = _resolve_embedding_model_name(embedding_model)
        self.vector_dim = vector_dim or MODEL_DIMENSIONS.get(embedding_model, VECTOR_DIM)
        self._client = None
        self._collection = None
        self._embedder = None
        self._embed_fn = embed_fn
        self._initialized = False
        self._last_error: str | None = None
        self._tracker: ChromaStateTracker | None = None
        if state_path is not None:
            self._tracker = ChromaStateTracker(state_path)

    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return True

        try:
            import chromadb
        except ImportError:
            logger.warning("chromadb not installed: pip install skmemory[chroma]")
            return False

        if self._embed_fn is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                logger.warning("sentence-transformers not installed: pip install skmemory[chroma]")
                return False

        try:
            if self.persist_dir:
                Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(path=self.persist_dir)
            else:
                self._client = chromadb.Client()

            if self._embed_fn is None:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(self.embedding_model_name)
                get_dim = getattr(
                    self._embedder,
                    "get_embedding_dimension",
                    getattr(self._embedder, "get_sentence_embedding_dimension", None),
                )
                if callable(get_dim):
                    resolved_dim = get_dim()
                    if isinstance(resolved_dim, int) and resolved_dim > 0:
                        self.vector_dim = resolved_dim

            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            self._initialized = True
            return True

        except Exception as e:
            logger.warning("ChromaDB initialization failed: %s", e)
            self._last_error = str(e)
            return False

    def _embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            return self._embed_fn(text)
        if self._embedder is None:
            return []
        return self._embedder.encode(text).tolist()

    def _memory_to_metadata(self, memory: Memory) -> dict:
        """Convert Memory to ChromaDB metadata dict.

        ChromaDB metadata values must be str, int, float, or bool.
        """
        return {
            "title": memory.title,
            "layer": memory.layer.value,
            "tags": ",".join(memory.tags) if memory.tags else "",
            "source": memory.source,
            "created_at": memory.created_at,
            "emotional_intensity": memory.emotional.intensity,
            "emotional_valence": memory.emotional.valence,
            "content_hash": memory.content_hash(),
            "is_chunk": bool(memory.parent_id),
            "parent_id": memory.parent_id or "",
            "authority_tier": memory.metadata.get("authority_tier", "memory"),
            "role": memory.role.value if hasattr(memory.role, "value") else str(memory.role),
        }

    def save(self, memory: Memory) -> str:
        if not self._ensure_initialized():
            return memory.id

        content_hash = memory.content_hash()

        # Dedup: skip if same content already indexed for this ID
        if self._tracker and self._tracker.is_current(memory.id, content_hash):
            return memory.id

        embedding = self._embed(memory.to_embedding_text())
        if not embedding:
            return memory.id

        metadata = self._memory_to_metadata(memory)

        self._collection.upsert(
            ids=[memory.id],
            embeddings=[embedding],
            metadatas=[metadata],
            documents=[memory.model_dump_json()],
        )

        if self._tracker:
            self._tracker.record(memory.id, content_hash)

        return memory.id

    def load(self, memory_id: str) -> Memory | None:
        if not self._ensure_initialized():
            return None

        try:
            result = self._collection.get(ids=[memory_id], include=["documents"])
            if not result["documents"] or not result["documents"][0]:
                return None
            return Memory.model_validate_json(result["documents"][0])
        except Exception as e:
            logger.warning("ChromaDB get failed for memory %s: %s", memory_id, e)
            return None

    def delete(self, memory_id: str) -> bool:
        if not self._ensure_initialized():
            return False

        try:
            existing = self._collection.get(ids=[memory_id], include=[])
            if not existing["ids"]:
                return False
            self._collection.delete(ids=[memory_id])
            if self._tracker:
                self._tracker.remove(memory_id)
            return True
        except Exception as e:
            logger.warning("ChromaDB delete failed for memory %s: %s", memory_id, e)
            return False

    def remove(self, memory_id: str) -> bool:
        """Remove a memory and all its chunks."""
        if not self._ensure_initialized():
            return False

        try:
            self._collection.delete(ids=[memory_id])

            # Delete chunks where parent_id matches
            chunk_results = self._collection.get(
                where={"parent_id": memory_id},
                include=[],
            )
            if chunk_results["ids"]:
                self._collection.delete(ids=chunk_results["ids"])

            if self._tracker:
                self._tracker.remove(memory_id)
            return True
        except Exception as e:
            logger.warning("ChromaDB remove failed: %s", e)
            return False

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        if not self._ensure_initialized():
            return []

        where_filter = None
        conditions = []

        if layer:
            conditions.append({"layer": {"$eq": layer.value}})
        if tags:
            for tag in tags:
                conditions.append({"tags": {"$contains": tag}})

        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}

        try:
            kwargs = {"include": ["documents"], "limit": limit}
            if where_filter:
                kwargs["where"] = where_filter

            result = self._collection.get(**kwargs)

            memories = []
            for doc in result.get("documents") or []:
                if doc:
                    try:
                        memories.append(Memory.model_validate_json(doc))
                    except Exception as e:
                        logger.warning("ChromaDB: skipping malformed memory document: %s", e)
                        continue

            memories.sort(key=lambda m: m.created_at, reverse=True)
            return memories
        except Exception as e:
            logger.warning("ChromaDB list_memories failed: %s", e)
            return []

    def search_text(
        self,
        query: str,
        limit: int = 10,
        layer: str | None = None,
        is_chunk: bool | None = None,
        authority_tier: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> list[Memory]:
        if not self._ensure_initialized():
            return []

        query = sanitize_query(query)
        embedding = self._embed(query)
        if not embedding:
            return []

        where_filter = None
        conditions = []

        if layer is not None:
            conditions.append({"layer": {"$eq": layer}})
        if is_chunk is not None:
            conditions.append({"is_chunk": {"$eq": is_chunk}})
        if authority_tier is not None:
            conditions.append({"authority_tier": {"$eq": authority_tier}})
        if tags:
            for tag in tags:
                conditions.append({"tags": {"$contains": tag}})
        if source is not None:
            conditions.append({"source": {"$eq": source}})

        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}

        try:
            kwargs = {
                "query_embeddings": [embedding],
                "n_results": limit,
                "include": ["documents", "distances"],
            }
            if where_filter:
                kwargs["where"] = where_filter

            results = self._collection.query(**kwargs)

            memories = []
            for doc in results.get("documents", [[]])[0]:
                if doc:
                    try:
                        memories.append(Memory.model_validate_json(doc))
                    except Exception as e:
                        logger.warning("ChromaDB: skipping malformed memory document: %s", e)
                        continue
            return memories
        except Exception as e:
            logger.warning("ChromaDB search failed: %s", e)
            return []

    def find_similar(self, content: str, k: int = 5) -> list[dict]:
        """Find memories with content similar to *content* (advisory dedup check).

        Runs the same ChromaDB nearest-neighbor query as ``search_text()``, but
        keeps the raw distances instead of discarding them, so a caller can
        decide for itself whether a candidate is "close enough" to be a
        near-duplicate. Read-only — never writes, merges, or mutates anything.

        Args:
            content: Text to check for near-duplicates (not yet stored).
            k: Max number of candidates to return.

        Returns:
            list[dict]: Each item is ``{"id", "content_preview", "similarity"}``,
                sorted by similarity descending. Empty list if the backend
                isn't initialized, embedding fails, or the query errors —
                this method never raises.
        """
        if not self._ensure_initialized():
            return []

        embedding = self._embed(sanitize_query(content))
        if not embedding:
            return []

        try:
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=k,
                include=["documents", "distances"],
            )

            ids = (results.get("ids") or [[]])[0]
            documents = (results.get("documents") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]

            matches = []
            for doc_id, doc, distance in zip(ids, documents, distances, strict=False):
                if not doc:
                    continue
                try:
                    preview = Memory.model_validate_json(doc).content[:CONTENT_PREVIEW_LENGTH]
                except Exception:
                    preview = doc[:CONTENT_PREVIEW_LENGTH]

                # ChromaDB's default HNSW space here is "cosine", where the
                # reported "distance" is 1 - cosine_similarity. Clamp to
                # [0, 1] since floating-point drift can push it slightly
                # outside that range.
                similarity = max(0.0, min(1.0, 1.0 - float(distance)))
                matches.append(
                    {
                        "id": doc_id,
                        "content_preview": preview,
                        "similarity": round(similarity, 4),
                    }
                )

            matches.sort(key=lambda m: m["similarity"], reverse=True)
            return matches
        except Exception as e:
            logger.warning("ChromaDB find_similar failed: %s", e)
            return []

    def sync_all(self, flat_files_dir: Path, agent_name: str) -> dict:
        """Incrementally sync all flat-file memories to ChromaDB.

        Only processes memories where content has changed or not yet indexed.
        Removes entries for memories that no longer have flat files.
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "errors": 0}

        if not self._ensure_initialized():
            logger.warning("ChromaDB sync_all: backend not initialized for agent %s", agent_name)
            return stats

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
                    if self._tracker and self._tracker.is_current(memory.id, content_hash):
                        stats["skipped"] += 1
                        continue

                    self.save(memory)
                    stats["indexed"] += 1
                except Exception as e:
                    logger.warning(
                        "ChromaDB sync_all: failed to process %s: %s", json_file.name, e
                    )
                    stats["errors"] += 1

        # Remove stale entries
        if self._tracker:
            stale_ids = self._tracker.all_ids() - flat_memory_ids
            for stale_id in stale_ids:
                try:
                    self.remove(stale_id)
                    stats["removed"] += 1
                except Exception as e:
                    logger.warning("ChromaDB sync_all: failed to remove stale %s: %s", stale_id, e)
                    stats["errors"] += 1

        logger.info(
            "ChromaDB sync_all for '%s': indexed=%d skipped=%d removed=%d errors=%d",
            agent_name,
            stats["indexed"],
            stats["skipped"],
            stats["removed"],
            stats["errors"],
        )
        return stats

    def health_check(self) -> dict:
        if not self._ensure_initialized():
            return {
                "ok": False,
                "backend": "SKChromaBackend",
                "error": self._last_error or "Not initialized",
            }

        try:
            count = self._collection.count()
            return {
                "ok": True,
                "backend": "SKChromaBackend",
                "persist_dir": self.persist_dir,
                "collection": self.collection_name,
                "embedding_model": self.requested_embedding_model,
                "resolved_embedding_model": self.embedding_model_name,
                "vector_dim": self.vector_dim,
                "documents_count": count,
            }
        except Exception as e:
            logger.warning("chroma_backend.py: %s", e)
            return {
                "ok": False,
                "backend": "SKChromaBackend",
                "error": str(e),
            }
