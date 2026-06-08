"""
PGVector — semantic + hybrid memory search backend (Postgres + pgvector).

Sovereign, syncable alternative to the Qdrant skvector backend: stores memory
embeddings in a local/central Postgres so the DB can be replicated across hosts
(streaming/logical replication) instead of snapshot-shipped.

Architecture:
  - Storage: Postgres + pgvector on .158 (SKMEMORY_PG_DSN).
  - Embedding: REMOTE HTTP endpoint (default the .100 bge-legal-v2 server) so the
    2.3GB model is not loaded in every skmemory process. Inject your own via
    `embed_fn`, or override SKMEMORY_EMBED_URL / SKMEMORY_EMBED_MODEL.

Same 1024-dim vector space as bge-legal-v2 -> drop-in with the existing schema.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)

DEFAULT_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@192.168.0.158:5432/skmemory"
)
DEFAULT_EMBED_URL = os.environ.get(
    "SKMEMORY_EMBED_URL", "http://192.168.0.100:11435/api/embed"
)
DEFAULT_EMBED_MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "bge-legal-v2")
VECTOR_DIM = 1024


class PGVectorBackend(BaseBackend):
    """Postgres + pgvector storage with hybrid (vector + BM25) search."""

    def __init__(
        self,
        dsn: str = DEFAULT_DSN,
        embed_fn: Callable[[str], list[float]] | None = None,
        embed_url: str = DEFAULT_EMBED_URL,
        embed_model: str = DEFAULT_EMBED_MODEL,
        vector_dim: int = VECTOR_DIM,
    ):
        self.dsn = dsn
        self.embed_url = embed_url
        self.embed_model = embed_model
        self.vector_dim = vector_dim
        self._embed_fn = embed_fn
        self._conn = None

    # --- infra ---------------------------------------------------------------
    def _connection(self):
        import psycopg
        from pgvector.psycopg import register_vector

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            register_vector(self._conn)
        return self._conn

    def _embed(self, text: str) -> list[float]:
        """Embed text. Uses injected embed_fn, else the remote HTTP endpoint."""
        if self._embed_fn is not None:
            return self._embed_fn(text)
        import httpx

        text = (text or "")[:8000]  # bge-m3 ctx headroom
        try:
            r = httpx.post(
                self.embed_url,
                json={"model": self.embed_model, "input": text},
                timeout=60.0,
            )
            r.raise_for_status()
            data = r.json()
            # Ollama: {"embeddings": [[...]]}  | OpenAI: {"data":[{"embedding":[...]}]}
            if "embeddings" in data:
                return data["embeddings"][0]
            if "data" in data:
                return data["data"][0]["embedding"]
            if "embedding" in data:
                return data["embedding"]
        except Exception as e:  # noqa: BLE001
            logger.warning("embed failed (%s): %s", self.embed_url, e)
        return []

    @staticmethod
    def _searchable(memory: Memory) -> str:
        return f"{memory.title}\n{memory.content}\n{memory.summary or ''}".strip()

    # --- BaseBackend ---------------------------------------------------------
    def save(self, memory: Memory) -> str:
        emb = self._embed(self._searchable(memory)) or None
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories
                  (id, layer, role, title, content, summary, tags, source,
                   created_at, updated_at, memory_json, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                  layer=EXCLUDED.layer, role=EXCLUDED.role, title=EXCLUDED.title,
                  content=EXCLUDED.content, summary=EXCLUDED.summary, tags=EXCLUDED.tags,
                  source=EXCLUDED.source, updated_at=EXCLUDED.updated_at,
                  memory_json=EXCLUDED.memory_json, embedding=EXCLUDED.embedding
                """,
                (
                    memory.id,
                    str(getattr(memory.layer, "value", memory.layer)),
                    str(getattr(memory.role, "value", memory.role)),
                    memory.title,
                    memory.content,
                    memory.summary or "",
                    list(memory.tags or []),
                    memory.source,
                    memory.created_at,
                    memory.updated_at,
                    memory.model_dump_json(),
                    emb,
                ),
            )
        return memory.id

    def load(self, memory_id: str) -> Memory | None:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("SELECT memory_json FROM memories WHERE id=%s", (memory_id,))
            row = cur.fetchone()
        return Memory.model_validate_json(_as_json_str(row[0])) if row else None

    def delete(self, memory_id: str) -> bool:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id=%s", (memory_id,))
            return cur.rowcount > 0

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        clauses, params = [], []
        if layer is not None:
            clauses.append("layer=%s")
            params.append(str(getattr(layer, "value", layer)))
        if tags:
            clauses.append("tags @> %s")
            params.append(list(tags))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories{where} ORDER BY created_at DESC LIMIT %s",
                params,
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        """Full-text (BM25) search over title/content/summary."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_json FROM memories
                WHERE tsv @@ plainto_tsquery('english', %s)
                ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC
                LIMIT %s
                """,
                (query, query, limit),
            )
            rows = cur.fetchall()
        if rows:
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]
        # fallback: case-insensitive substring
        with conn.cursor() as cur:
            cur.execute(
                "SELECT memory_json FROM memories WHERE content ILIKE %s LIMIT %s",
                (f"%{query}%", limit),
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    def search(self, query: str, limit: int = 10) -> list[Memory]:
        """Semantic vector search (cosine). Falls back to text search if no embedding."""
        qvec = self._embed(query)
        if not qvec:
            return self.search_text(query, limit)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT memory_json FROM memories WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (qvec, limit),
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    # convenience alias
    def search_semantic(self, query: str, limit: int = 10) -> list[Memory]:
        return self.search(query, limit)

    def health_check(self) -> dict:
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM memories")
                n = cur.fetchone()[0]
            return {
                "ok": True,
                "backend": "PGVectorBackend",
                "dsn": self.dsn.split("@")[-1],
                "memories": n,
                "embed_url": self.embed_url,
                "vector_dim": self.vector_dim,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "PGVectorBackend", "error": str(e)}


def _as_json_str(value) -> str:
    """psycopg returns JSONB as dict; Memory.model_validate_json needs a str."""
    return value if isinstance(value, str) else json.dumps(value)
