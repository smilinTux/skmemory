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
    The embed server runs on a GPU box whose VRAM can flap, so a comma-separated
    SKMEMORY_EMBED_URLS lets a node fail over to a secondary endpoint. Each endpoint
    call is bounded by a short, configurable timeout (SKMEMORY_EMBED_TIMEOUT, with a
    tighter SKMEMORY_EMBED_CONNECT_TIMEOUT) so a wedged backend is abandoned in
    seconds and failover proceeds, instead of hanging a save for timeout * N-endpoints
    during a GPU/driver outage. Whatever the endpoint, the write path NEVER stores a
    NULL/empty/zero vector: if every endpoint is down it raises EmbeddingUnavailable
    rather than silently poisoning recall with an unsearchable row.

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


def _parse_float_env(name: str, default: float) -> float:
    """Read a positive float env var, falling back to ``default`` on missing/garbage.

    A bad value (empty, non-numeric, <= 0) must never silently become 0 (which
    httpx treats as "fail immediately") nor crash import; we log and use the
    default so a typo in one node's env can't wedge memory writes fleet-wide.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s", name, raw, default)
        return default
    if val <= 0:
        logger.warning("%s=%r must be > 0; using default %s", name, raw, default)
        return default
    return val


# Per-endpoint embed timeout. The embed server sits on a GPU box whose driver/VRAM
# can flap (NVML "Driver/library version mismatch" wedges the process), so a
# hardcoded long timeout meant a save could hang timeout * N-endpoints -> minutes
# during a GPU outage. A short, configurable timeout caps that: a wedged endpoint
# is abandoned fast and failover moves to the next URL. The connect phase gets an
# even shorter cap so an unroutable/half-open host fails almost immediately.
# Overridable via SKMEMORY_EMBED_TIMEOUT / SKMEMORY_EMBED_CONNECT_TIMEOUT (seconds).
DEFAULT_EMBED_TIMEOUT = _parse_float_env("SKMEMORY_EMBED_TIMEOUT", 15.0)
DEFAULT_EMBED_CONNECT_TIMEOUT = _parse_float_env(
    "SKMEMORY_EMBED_CONNECT_TIMEOUT", min(5.0, DEFAULT_EMBED_TIMEOUT)
)


def _parse_embed_urls(raw: str | None, primary: str) -> list[str]:
    """Build the ordered embed-endpoint list from a comma-separated string.

    The embed server sits on a GPU box with documented VRAM flapping, so a node
    can list a secondary (or more) to fail over to. Blanks are dropped and order
    is preserved; if nothing usable is given we fall back to the single
    ``primary`` URL so unconfigured nodes keep today's behavior. Duplicates are
    collapsed (first-wins) so a repeated URL is not retried twice.
    """
    urls = [u.strip() for u in (raw or "").split(",") if u.strip()]
    if not urls:
        urls = [primary]
    return list(dict.fromkeys(u for u in urls if u))


# Endpoint failover: SKMEMORY_EMBED_URLS = "primary,secondary,..." (comma-separated).
# Defaults to the single SKMEMORY_EMBED_URL so an unconfigured node is unchanged.
DEFAULT_EMBED_URLS = _parse_embed_urls(os.environ.get("SKMEMORY_EMBED_URLS"), DEFAULT_EMBED_URL)

# Fleet standard is mxbai-embed-large everywhere. If an endpoint silently serves a
# DIFFERENT model (or a redeploy swaps it), stored vectors become incompatible with
# query vectors and recall is corrupted with no error. We pin the expected model +
# dimension and verify each embedding at runtime so a mismatch fails LOUDLY instead
# of silently poisoning the store. Ops escape hatch: SKMEMORY_EMBED_VERIFY=0.
DEFAULT_EMBED_VERIFY = os.environ.get("SKMEMORY_EMBED_VERIFY", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}


class EmbeddingModelMismatch(RuntimeError):
    """Raised when the embedding endpoint's output does not match the pinned model.

    Signals that the served model identity (vector dimension and/or model name)
    diverged from what was configured, so writing the returned vector would poison
    the store. Failing here is deliberate: a loud error beats silent corruption.
    """


class EmbeddingUnavailable(RuntimeError):
    """Raised on the WRITE path when no embed endpoint yields a usable vector.

    Every configured endpoint was down / timed out / returned a non-200 or an
    empty/all-zero embedding. Storing that as a NULL/zero vector would silently
    poison recall (the row can never match a semantic query), so save() fails
    loudly instead. Query paths still degrade gracefully to BM25/text search.
    """


def _normalize_model_name(name: str) -> str:
    """Reduce a model id to a comparable base name.

    Strips an Ollama tag (``mxbai-embed-large:latest`` -> ``mxbai-embed-large``)
    and any org/path prefix (``mixedbread-ai/mxbai-embed-large-v1`` ->
    ``mxbai-embed-large-v1``), lowercased. Used for tolerant identity matching.
    """
    base = (name or "").strip().lower()
    base = base.split(":", 1)[0]  # drop Ollama tag
    base = base.rsplit("/", 1)[-1]  # drop org/path prefix
    return base


def _model_names_match(expected: str, actual: str) -> bool:
    """True when two model ids plausibly name the same model.

    Tolerant of tags, org prefixes, and version suffixes (``mxbai-embed-large``
    vs ``mxbai-embed-large-v1``) by treating the shorter normalized name being a
    substring of the longer as a match. A genuinely different model
    (``nomic-embed-text``) is not contained and so is rejected.
    """
    exp = _normalize_model_name(expected)
    act = _normalize_model_name(actual)
    if not exp or not act:
        return True  # nothing to compare against -> do not block
    short, long = (exp, act) if len(exp) <= len(act) else (act, exp)
    return short in long


class PGVectorBackend(BaseBackend):
    """Postgres + pgvector storage with hybrid (vector + BM25) search."""

    def __init__(
        self,
        dsn: str = DEFAULT_DSN,
        embed_fn: Callable[[str], list[float]] | None = None,
        embed_url: str = DEFAULT_EMBED_URL,
        embed_urls: list[str] | None = None,
        embed_model: str = DEFAULT_EMBED_MODEL,
        vector_dim: int = VECTOR_DIM,
        agent: str | None = None,
        verify_embedding: bool = DEFAULT_EMBED_VERIFY,
        embed_timeout: float = DEFAULT_EMBED_TIMEOUT,
        embed_connect_timeout: float = DEFAULT_EMBED_CONNECT_TIMEOUT,
    ):
        self.dsn = dsn
        # Ordered embed endpoints for failover. Precedence:
        #   explicit embed_urls -> explicit single embed_url -> SKMEMORY_EMBED_URLS
        #   env (which itself defaults to the single SKMEMORY_EMBED_URL).
        # self.embed_url stays the PRIMARY so existing callers/error text/to_dict
        # keep working; failover walks self.embed_urls in order.
        if embed_urls is not None:
            self.embed_urls = list(dict.fromkeys(u for u in embed_urls if u))
        elif embed_url != DEFAULT_EMBED_URL:
            self.embed_urls = [embed_url]
        else:
            self.embed_urls = list(DEFAULT_EMBED_URLS)
        if not self.embed_urls:
            self.embed_urls = [embed_url]
        self.embed_url = self.embed_urls[0]
        self.embed_model = embed_model
        self.vector_dim = vector_dim
        # Short, per-endpoint timeouts so a wedged embed backend (GPU outage) is
        # abandoned quickly and failover proceeds, instead of hanging the whole
        # memory op for timeout * N-endpoints. Connect is capped separately so an
        # unroutable host fails even faster. Both stay > 0 (0 == httpx "fail now").
        self.embed_timeout = embed_timeout if embed_timeout and embed_timeout > 0 else 15.0
        self.embed_connect_timeout = (
            embed_connect_timeout
            if embed_connect_timeout and embed_connect_timeout > 0
            else min(5.0, self.embed_timeout)
        )
        # Pin + verify the embedding model identity (dimension, and model name when
        # the endpoint reports it) on every embed. Off via SKMEMORY_EMBED_VERIFY=0.
        self.verify_embedding = verify_embedding
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

    def _verify_embedding(self, vec: list[float], served_model: str | None = None) -> list[float]:
        """Verify a vector matches the pinned model identity, else raise.

        An empty vector means the embed call failed upstream; that is handled
        gracefully elsewhere (returns []), so we pass it through unchecked. A
        non-empty vector whose dimension differs from the pinned ``vector_dim``,
        or a ``served_model`` name that names a different model, raises
        ``EmbeddingModelMismatch`` naming expected-vs-actual.
        """
        if not self.verify_embedding or not vec:
            return vec
        if len(vec) != self.vector_dim:
            raise EmbeddingModelMismatch(
                "embedding dimension mismatch: expected "
                f"{self.vector_dim} (model {self.embed_model!r}) but endpoint "
                f"{self.embed_url!r} returned {len(vec)}. The served embedding model "
                "does not match the pinned one; refusing to store an incompatible "
                "vector. Set SKMEMORY_EMBED_VERIFY=0 to override."
            )
        if served_model and not _model_names_match(self.embed_model, served_model):
            raise EmbeddingModelMismatch(
                f"embedding model mismatch: expected {self.embed_model!r} but endpoint "
                f"{self.embed_url!r} reported serving {served_model!r}. Refusing to store "
                "a vector from an unexpected model. Set SKMEMORY_EMBED_VERIFY=0 to override."
            )
        return vec

    def _finalize_embedding(self, vec: list[float], required: bool) -> list[float]:
        """Gate the final vector for the caller's tolerance.

        Query paths pass ``required=False`` and tolerate an empty ``[]`` (they
        degrade to BM25/text). The WRITE path passes ``required=True``: an
        empty, or all-zero, vector would be stored as a NULL/unsearchable row
        that silently poisons recall, so we raise ``EmbeddingUnavailable``
        instead. (A wrong-dimension vector already raised in ``_verify_embedding``.)
        """
        if not required:
            return vec
        if not vec or not any(vec):
            raise EmbeddingUnavailable(
                "embedding unavailable: every configured embed endpoint "
                f"({', '.join(self.embed_urls)}) was down, timed out, or returned an "
                "empty/all-zero vector. Refusing to store a NULL embedding that would "
                "silently break recall for this memory. Bring an embed server back up, "
                "or configure a fallback via SKMEMORY_EMBED_URLS."
            )
        return vec

    def _embed(self, text: str, *, required: bool = False) -> list[float]:
        """Embed text, with endpoint failover.

        Uses the injected ``embed_fn`` when present, else walks ``embed_urls`` in
        order, returning the first usable vector. On the WRITE path pass
        ``required=True`` so a total failure raises ``EmbeddingUnavailable``
        rather than yielding an empty vector that would be stored as NULL. A
        pinned-model mismatch (``EmbeddingModelMismatch``) always propagates.
        """
        if self._embed_fn is not None:
            vec = self._verify_embedding(self._embed_fn(text))
            return self._finalize_embedding(vec, required)

        # mxbai-embed-large caps at 512 tokens and this Ollama build 400s on overflow
        # (its `truncate` flag is a no-op). ~1400 chars clears 512 tokens for typical
        # text; for denser text we halve and retry until it fits. The full memory is
        # still stored in content/memory_json and BM25-searchable, so recall is intact.
        text = (text or "")[:1400]
        vec: list[float] = []
        for url in self.embed_urls:
            vec = self._embed_one(url, text)
            if vec:
                break  # first endpoint that yields a vector wins
        return self._finalize_embedding(vec, required)

    def _embed_one(self, url: str, text: str) -> list[float]:
        """Embed via a SINGLE endpoint. Returns ``[]`` on transient failure
        (connection/timeout/non-200) so the caller can fail over to the next URL;
        raises ``EmbeddingModelMismatch`` on a pinned-model mismatch (a config
        error, identical on every endpoint, so failover cannot help). A 400
        (over-context) shrinks the text and retries the same endpoint."""
        import httpx

        # Bound each phase: read/write/pool at embed_timeout, connect even shorter,
        # so a wedged or unroutable endpoint is dropped fast and failover proceeds.
        timeout = httpx.Timeout(self.embed_timeout, connect=self.embed_connect_timeout)
        while text:
            try:
                r = httpx.post(
                    url,
                    json={"model": self.embed_model, "input": text, "truncate": True},
                    timeout=timeout,
                )
                r.raise_for_status()
                data = r.json()
                # Ollama and OpenAI-compatible endpoints echo the served model name;
                # verify it (and the dimension) so a swapped model fails loudly.
                served_model = data.get("model") if isinstance(data, dict) else None
                # Ollama: {"embeddings": [[...]]} | OpenAI: {"data":[{"embedding":[...]}]}
                if "embeddings" in data:
                    return self._verify_embedding(data["embeddings"][0], served_model)
                if "data" in data:
                    return self._verify_embedding(data["data"][0]["embedding"], served_model)
                if "embedding" in data:
                    return self._verify_embedding(data["embedding"], served_model)
                return []
            except httpx.HTTPStatusError as e:
                # 400 == over context window: shrink and retry, else give up on this url.
                if e.response.status_code == 400 and len(text) > 200:
                    text = text[: len(text) // 2]
                    continue
                logger.warning("embed failed (%s): %s", url, e)
                return []
            except EmbeddingModelMismatch:
                # A pinned-model mismatch is not a transient embed failure: it must
                # surface loudly, not be swallowed into an empty vector or a retry.
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("embed failed (%s): %s", url, e)
                return []
        return []

    @staticmethod
    def _searchable(memory: Memory) -> str:
        return f"{memory.title}\n{memory.content}\n{memory.summary or ''}".strip()

    # --- BaseBackend ---------------------------------------------------------
    def save(self, memory: Memory) -> str:
        # required=True: never store a NULL/empty/zero embedding (would poison
        # recall). If every endpoint is down this raises EmbeddingUnavailable.
        emb = self._embed(self._searchable(memory), required=True)
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

    def remove(self, memory_id: str) -> bool:
        """Remove a memory AND its child/chunk rows (cascade).

        This mirrors the chunk-cascade ``remove()`` of the other vector
        backends (``SKChromaBackend``, ``SKVectorBackend``): delete the
        memory's own row and any child rows that reference it via
        ``parent_id``. Those backends delete chunk *points* keyed by a
        ``parent_id`` payload; the pgvector analogue is a child ``Memory``
        row whose ``memory_json.parent_id`` points at this id.

        ``MemoryStore.forget()`` calls ``self.vector.remove(memory_id)``.
        Before this method existed, ``PGVectorBackend`` exposed only
        ``delete()``, so on the default (pgvector) deployment forget() raised
        ``AttributeError`` — swallowed at ``store.py`` — and the pg row was
        NOT deleted at forget time; it lingered until the daily reconcile
        prune (Gap A, card 23a722ca). Now a forget actually forgets
        immediately.

        Scoped to ``self.agent``. Returns True if the main row or any child
        row was deleted.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE id=%s AND agent=%s",
                (memory_id, self.agent),
            )
            removed = cur.rowcount
            # Cascade: any child/chunk memory whose memory_json.parent_id
            # references this id (the pgvector analogue of Chroma/Qdrant
            # deleting chunk points by parent_id).
            cur.execute(
                "DELETE FROM memories WHERE memory_json->>'parent_id'=%s AND agent=%s",
                (memory_id, self.agent),
            )
            removed += cur.rowcount
        return removed > 0

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
                "embed_urls": list(self.embed_urls),
                "embed_timeout": self.embed_timeout,
                "embed_connect_timeout": self.embed_connect_timeout,
                "vector_dim": self.vector_dim,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "PGVectorBackend", "error": str(e)}


def _as_json_str(value) -> str:
    """psycopg returns JSONB as dict; Memory.model_validate_json needs a str."""
    return value if isinstance(value, str) else json.dumps(value)
