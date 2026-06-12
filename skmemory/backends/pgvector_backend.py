"""
PGVector — semantic + hybrid memory search backend (Postgres + pgvector).

Sovereign, syncable alternative to the Qdrant skvector backend: stores memory
embeddings in a local/central Postgres so the DB can be replicated across hosts
(streaming/logical replication) instead of snapshot-shipped.

Architecture:
  - Storage: Postgres + pgvector on .158 (SKMEMORY_PG_DSN).
  - Embedding: REMOTE HTTP endpoint (default the .100 mxbai-embed-large server,
    Ollama :11434) so the model is not loaded in every skmemory process. Inject
    your own via `embed_fn`, or override SKMEMORY_EMBED_URL / SKMEMORY_EMBED_MODEL.

Same 1024-dim vector space as mxbai-embed-large -> drop-in with the existing schema.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)

# Recency boost for hybrid ranking — so "latest" asks surface today's work
# instead of keyword-dense older content. Added to the RRF score as
# boost*exp(-age_days/halflife) (recent ≈ +boost). Tunable via env; set
# SKMEMORY_RECENCY_BOOST=0 to disable.
_RECENCY_BOOST = float(os.environ.get("SKMEMORY_RECENCY_BOOST", "0.03"))
_RECENCY_HALFLIFE_DAYS = float(os.environ.get("SKMEMORY_RECENCY_HALFLIFE_DAYS", "21"))

# Tantivy/pg_search query-string operators. ParadeDB's `content @@@ 'str'`
# parses the string as a query expression, so raw punctuation/URLs
# ("biolabs!", "https://…") throw "could not parse query string". Strip them to
# plain terms for the BM25 leg; the vector leg still embeds the original query.
_BM25_SPECIAL = str.maketrans({c: " " for c in '+-&|!(){}[]^"~*?:\\/<>='})


def _bm25_terms(query: str) -> str:
    """Sanitize free text into safe BM25 terms (no Tantivy operators)."""
    return " ".join((query or "").translate(_BM25_SPECIAL).split()) or "_nomatch_"


DEFAULT_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@192.168.0.158:5432/skmemory"
)
DEFAULT_EMBED_URL = os.environ.get(
    "SKMEMORY_EMBED_URL", "http://192.168.0.100:11434/api/embed"  # mxbai (Ollama); was :11435 bge
)
DEFAULT_EMBED_MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")  # was bge-legal-v2
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

        text = (text or "")[:1100]  # mxbai-embed-large 512-tok ctx safe (was 8000 for bge)
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
            cur.execute(
                "DELETE FROM memories WHERE id=%s AND agent=%s", (memory_id, self.agent)
            )
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
        self,
        query: str,
        limit: int = 10,
        *,
        layer: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> list[Memory]:
        """Hybrid (vector + pg_search BM25) search over memories, with optional filters.

        Despite the name (kept for the store's call signature), this is the primary
        search: it fuses mxbai vector + BM25 via RRF. Degrades gracefully to pure
        BM25 -> tsv FTS -> ILIKE if embeddings/pg_search are unavailable.
        """
        def _filt():
            cl = ["agent=%s"]; ps = [self.agent]
            if layer:  cl.append("layer=%s");   ps.append(layer)
            if source: cl.append("source=%s");  ps.append(source)
            if tags:   cl.append("tags && %s");  ps.append(list(tags))
            return " AND ".join(cl), ps

        where, fp = _filt()
        conn = self._connection()
        qvec = self._embed(query)
        bm_query = _bm25_terms(query)  # sanitized for ParadeDB @@@ (no operators)
        # recency term: boost*exp(-age_days/halflife), added to the RRF score.
        rec = (f"+ {_RECENCY_BOOST}*exp(-(extract(epoch from now()-m.created_at)"
               f"/86400.0)/{_RECENCY_HALFLIFE_DAYS})") if _RECENCY_BOOST > 0 else ""
        # 1) hybrid: vector (mxbai) + BM25 (pg_search), RRF (vector weighted 2x)
        if qvec:
            vlit = "[" + ",".join(map(str, qvec)) + "]"
            sql = f"""
                WITH vec AS (
                  SELECT id, row_number() OVER (ORDER BY embedding <=> %s::vector) r
                  FROM memories WHERE {where} AND embedding IS NOT NULL
                  ORDER BY embedding <=> %s::vector LIMIT 100),
                bm AS (
                  SELECT id, row_number() OVER (ORDER BY paradedb.score(id) DESC) r
                  FROM memories WHERE {where} AND content @@@ %s
                  ORDER BY paradedb.score(id) DESC LIMIT 100)
                SELECT m.memory_json FROM memories m
                  LEFT JOIN vec ON vec.id=m.id LEFT JOIN bm ON bm.id=m.id
                WHERE (vec.id IS NOT NULL OR bm.id IS NOT NULL) AND {where}
                ORDER BY (2.0*COALESCE(1.0/(60+vec.r),0)+COALESCE(1.0/(60+bm.r),0) {rec}) DESC
                LIMIT %s"""
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, [vlit, *fp, vlit, *fp, bm_query, *fp, limit])
                    rows = cur.fetchall()
                if rows:
                    return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]
            except Exception as exc:  # noqa: BLE001
                logger.warning("hybrid search failed, falling back to BM25: %s", exc)
                conn.rollback()
        # 2) pure BM25 (pg_search)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT memory_json FROM memories WHERE {where} AND content @@@ %s "
                    f"ORDER BY paradedb.score(id) DESC LIMIT %s", [*fp, bm_query, limit])
                rows = cur.fetchall()
            if rows:
                return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]
        except Exception:  # noqa: BLE001
            conn.rollback()
        # 3) tsv FTS  4) ILIKE
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories WHERE {where} "
                f"AND tsv @@ plainto_tsquery('english',%s) "
                f"ORDER BY ts_rank(tsv,plainto_tsquery('english',%s)) DESC LIMIT %s",
                [*fp, query, query, limit])
            rows = cur.fetchall()
        if rows:
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in rows]
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT memory_json FROM memories WHERE {where} AND content ILIKE %s LIMIT %s",
                [*fp, f"%{query}%", limit])
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    def search(self, query: str, limit: int = 10) -> list[Memory]:
        """Semantic vector search (cosine). Falls back to text search if no embedding."""
        qvec = self._embed(query)
        if not qvec:
            return self.search_text(query, limit)
        vlit = "[" + ",".join(map(str, qvec)) + "]"
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT memory_json FROM memories WHERE agent=%s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (self.agent, vlit, limit),
            )
            return [Memory.model_validate_json(_as_json_str(r[0])) for r in cur.fetchall()]

    # convenience alias
    def search_semantic(self, query: str, limit: int = 10) -> list[Memory]:
        return self.search(query, limit)

    def health_check(self) -> dict:
        try:
            conn = self._connection()
            with conn.cursor() as cur:
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
