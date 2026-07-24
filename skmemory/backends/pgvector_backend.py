"""
PGVector — semantic + hybrid memory search backend (Postgres + pgvector).

Sovereign, syncable alternative to the Qdrant skvector backend. The embedding
store is a DERIVED, per-node rebuildable cache (same class as the SQLite
index.db), NOT a replicated system of record:
  - Source of truth = the Syncthing-synced flat JSON memory files. `memories`
    rows (content + embedding) are rebuilt from them by skmem_reconcile.py
    (idempotent, agent-scoped). Embeddings are a deterministic function of the
    flat content + the mxbai model, so any node can regenerate them locally.
  - Topology: each node runs its OWN writable skmem-pg on localhost. Agents point
    only at localhost (SKMEMORY_PG_DSN). No streaming/logical replication, no
    remote primary, no failover — a node stays self-sufficient, and DR is covered
    by rebuild-from-flat plus the Syncthing-synced pg dumps.
  - Embedding: HTTP endpoint (default the local Ollama mxbai-embed-large server) so
    the model is served once per host, not loaded in every skmemory process. Inject
    your own via `embed_fn`, or override SKMEMORY_EMBED_URL / SKMEMORY_EMBED_MODEL.

1024-dim vector space (mxbai-embed-large) -> drop-in with the existing schema.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from ..models import Memory, MemoryLayer
from ..query_sanitizer import sanitize_query
from .base import BaseBackend
from .sqlite_backend import CONTENT_PREVIEW_LENGTH

logger = logging.getLogger(__name__)

# skmem-pg is a LOCAL, per-node writable Postgres. The embedding store is a
# derived, rebuildable cache (same class as index.db): `memories` are rebuilt
# from the Syncthing-synced flat JSON via skmem_reconcile.py, `docs` from the
# wiki via skingest. It is NOT streaming-replicated and is NOT a remote primary.
# Default to the node-local writable port; `SKMEMORY_PG_DSN` overrides per node.
# (Was :5433 — the retired standby port — which made an unconfigured node target
# a read-only replica where every save()/delete() raised.)
DEFAULT_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory"
)
DEFAULT_EMBED_URL = os.environ.get("SKMEMORY_EMBED_URL", "http://localhost:11434/api/embed")
DEFAULT_EMBED_MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")
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
        agent: str | None = None,
    ):
        self.dsn = dsn
        self.embed_url = embed_url
        self.embed_model = embed_model
        self.vector_dim = vector_dim
        # Agent isolation: one pg shared across agents, scoped by this column.
        self.agent = (
            agent
            or os.environ.get("SKMEMORY_AGENT")
            or os.environ.get("SKAGENT")
            or os.environ.get("SKCAPSTONE_AGENT")
            or "lumina"
        )
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

        # mxbai-embed-large caps at 512 tokens and this Ollama build 400s on overflow
        # (its `truncate` flag is a no-op). ~1400 chars clears 512 tokens for typical
        # text; for denser text we halve and retry until it fits. The full memory is
        # still stored in content/memory_json and BM25-searchable, so recall is intact.
        text = (text or "")[:1400]
        while text:
            try:
                r = httpx.post(
                    self.embed_url,
                    json={"model": self.embed_model, "input": text, "truncate": True},
                    timeout=60.0,
                )
                r.raise_for_status()
                data = r.json()
                # Ollama: {"embeddings": [[...]]} | OpenAI: {"data":[{"embedding":[...]}]}
                if "embeddings" in data:
                    return data["embeddings"][0]
                if "data" in data:
                    return data["data"][0]["embedding"]
                if "embedding" in data:
                    return data["embedding"]
                return []
            except httpx.HTTPStatusError as e:
                # 400 == over context window: shrink and retry, else give up.
                if e.response.status_code == 400 and len(text) > 200:
                    text = text[: len(text) // 2]
                    continue
                logger.warning("embed failed (%s): %s", self.embed_url, e)
                return []
            except Exception as e:  # noqa: BLE001
                logger.warning("embed failed (%s): %s", self.embed_url, e)
                return []
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
                  (id, agent, layer, role, title, content, summary, tags, source,
                   created_at, updated_at, memory_json, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                  agent=EXCLUDED.agent, layer=EXCLUDED.layer, role=EXCLUDED.role,
                  title=EXCLUDED.title, content=EXCLUDED.content, summary=EXCLUDED.summary,
                  tags=EXCLUDED.tags, source=EXCLUDED.source, updated_at=EXCLUDED.updated_at,
                  memory_json=EXCLUDED.memory_json, embedding=EXCLUDED.embedding
                """,
                (
                    memory.id,
                    self.agent,
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
            cur.execute(
                "SELECT memory_json FROM memories WHERE id=%s AND agent=%s",
                (memory_id, self.agent),
            )
            row = cur.fetchone()
        return Memory.model_validate_json(_as_json_str(row[0])) if row else None

    def delete(self, memory_id: str) -> bool:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id=%s AND agent=%s", (memory_id, self.agent))
            return cur.rowcount > 0

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        clauses, params = ["agent=%s"], [self.agent]
        if layer is not None:
            clauses.append("layer=%s")
            params.append(str(getattr(layer, "value", layer)))
        if tags:
            clauses.append("tags @> %s")
            params.append(list(tags))
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories{where} ORDER BY created_at DESC LIMIT %s",
                params,
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    def search_text(
        self, query: str, limit: int = 10, layer=None, tags=None, source=None
    ) -> list[Memory]:
        """Primary search: mxbai semantic vector (cosine) with optional
        layer/tags/source filters, falling back to BM25 full-text then ILIKE when
        the query can't be embedded or vector returns nothing.

        Named search_text for the MemoryStore contract — store.search() calls this
        with the filter kwargs. (Previously this was BM25-only and rejected the
        kwargs, so every store search silently fell back to text. Now it uses the
        mxbai vectors as intended.)"""
        clauses, params = ["agent=%s"], [self.agent]
        if layer is not None:
            clauses.append("layer=%s")
            params.append(str(getattr(layer, "value", layer)))
        if tags:
            clauses.append("tags @> %s")
            params.append(list(tags))
        if source:
            clauses.append("source=%s")
            params.append(source)
        where = " AND ".join(clauses)
        conn = self._connection()

        # 1) semantic vector (cosine) — the mxbai path
        qvec = self._embed(query)
        if qvec:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT memory_json FROM memories WHERE {where} "
                    f"AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s",
                    params + [qvec, limit],
                )
                rows = cur.fetchall()
            if rows:
                return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]

        # 2) BM25 full-text fallback (same filters)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories WHERE {where} "
                f"AND tsv @@ plainto_tsquery('english', %s) "
                f"ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC LIMIT %s",
                params + [query, query, limit],
            )
            rows = cur.fetchall()
        if rows:
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]

        # 3) case-insensitive substring fallback
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories WHERE {where} AND content ILIKE %s LIMIT %s",
                params + [f"%{query}%", limit],
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
                "SELECT memory_json FROM memories WHERE agent=%s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (self.agent, qvec, limit),
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    # convenience alias
    def search_semantic(self, query: str, limit: int = 10) -> list[Memory]:
        return self.search(query, limit)

    def find_similar(self, content: str, k: int = 5) -> list[dict]:
        """Find memories with content similar to *content* (advisory dedup check).

        Mirrors ``SKChromaBackend.find_similar()`` — same signature and return
        shape — so ``MemoryStore.check_duplicate()`` works identically
        regardless of which vector backend is wired in. Runs the same pgvector
        cosine-distance nearest-neighbor query as ``search()``/``search_text()``,
        scoped to ``self.agent``, but keeps the raw distance (converted to a
        similarity) instead of discarding it, so the caller can decide for
        itself whether a candidate is "close enough" to be a near-duplicate.
        Read-only — never writes, merges, or mutates anything.

        Args:
            content: Text to check for near-duplicates (not yet stored).
            k: Max number of candidates to return.

        Returns:
            list[dict]: Each item is ``{"id", "content_preview", "similarity"}``,
                sorted by similarity descending. Empty list if the query can't
                be embedded, the connection/query fails, or there are no
                candidates — this method never raises.
        """
        try:
            qvec = self._embed(sanitize_query(content))
            if not qvec:
                return []

            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, content, embedding <=> %s::vector AS distance "
                    "FROM memories WHERE agent=%s AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (qvec, self.agent, qvec, k),
                )
                rows = cur.fetchall()

            matches = []
            for row_id, row_content, distance in rows:
                # pgvector cosine (<=>) distance is 1 - cosine_similarity.
                # Clamp to [0, 1] since floating-point drift can push it
                # slightly outside that range.
                similarity = max(0.0, min(1.0, 1.0 - float(distance)))
                preview = (row_content or "")[:CONTENT_PREVIEW_LENGTH]
                matches.append(
                    {
                        "id": row_id,
                        "content_preview": preview,
                        "similarity": round(similarity, 4),
                    }
                )

            matches.sort(key=lambda m: m["similarity"], reverse=True)
            return matches
        except Exception as e:  # noqa: BLE001
            logger.warning("PGVectorBackend find_similar failed: %s", e)
            return []

    def health_check(self) -> dict:
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                # Guard: skmem-pg must be a LOCAL writable primary, never a
                # read-only standby. A replica silently breaks every save()/
                # delete() (and ParadeDB Community cannot serve pg_search reads
                # in recovery), so fail loud rather than look healthy.
                cur.execute("SELECT pg_is_in_recovery()")
                in_recovery = cur.fetchone()[0]
                if in_recovery:
                    return {
                        "ok": False,
                        "backend": "PGVectorBackend",
                        "agent": self.agent,
                        "dsn": self.dsn.split("@")[-1],
                        "error": "pg is in recovery (read-only standby) — skmem-pg "
                        "must be a local writable primary; promote it or fix "
                        "SKMEMORY_PG_DSN to the local node.",
                    }
                cur.execute("SELECT count(*) FROM memories WHERE agent=%s", (self.agent,))
                n = cur.fetchone()[0]
            return {
                "ok": True,
                "backend": "PGVectorBackend",
                "agent": self.agent,
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
