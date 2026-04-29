"""
Lazy Memory Context Loader - Three-Tier Memory Architecture.

Loads memories efficiently based on date tiers to optimize token usage:
- TODAY: Full content (active work)
- YESTERDAY: Summaries only (recent context)
- HISTORICAL: Reference count (deep search available)

Usage:
    loader = LazyMemoryLoader("lumina")
    context = loader.load_active_context()  # Token-optimized

    # Deep search when needed
    results = loader.deep_search("project gentis")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from .agents import get_agent_paths
from .backends.sqlite_backend import SQLiteBackend

logger = logging.getLogger(__name__)


@dataclass
class MemoryContext:
    """Container for loaded memory context."""

    today_memories: list[dict]  # Full memories
    yesterday_summaries: list[dict]  # Summaries only
    historical_count: int  # Reference count only

    def to_context_string(self, max_tokens: int = 3000) -> str:
        """Convert to token-optimized context string."""
        sections = []

        # Today's memories (full content)
        if self.today_memories:
            sections.append(f"## Today's Memories ({len(self.today_memories)})")
            for mem in self.today_memories[:20]:  # Limit to 20
                content = mem.get("content", "")[:200]  # Truncate if needed
                line = f"- {mem.get('title', 'Untitled')}: {content}"
                sections.append(line)
                # Add related context if present
                if mem.get("related_context"):
                    for rel in mem["related_context"]:
                        sections.append(f"  → {rel['edge']}: {rel['title']} [{rel['layer']}]")
                if mem.get("entities"):
                    sections.append(f"  entities: {', '.join(mem['entities'])}")

        # Yesterday's summaries
        if self.yesterday_summaries:
            sections.append(f"\n## Yesterday ({len(self.yesterday_summaries)} memories)")
            for mem in self.yesterday_summaries[:10]:  # Limit to 10
                summary = mem.get("summary", "No summary")[:150]
                sections.append(f"- {mem.get('title', 'Untitled')}: {summary}")

        # Historical reference
        if self.historical_count > 0:
            sections.append("\n## Historical Memory")
            sections.append(f"- {self.historical_count} long-term memories available")
            sections.append("- Use 'search memory [query]' to recall specific details")

        return "\n".join(sections)


def _read_yaml_file(path: Path) -> dict | None:
    """Load a YAML file, return dict or None on missing/error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Could not load %s: %s", path.name, e)
        return None


def _load_skvector_config(config_dir: Path) -> dict | None:
    """Load skvector config: try skvector.yaml first, then skmemory.yaml inline,
    then skmemory.yaml flat keys (legacy migration path)."""
    # 1. Dedicated skvector.yaml
    path = config_dir / "skvector.yaml"
    if path.exists():
        cfg = _read_yaml_file(path)
        if cfg and cfg.get("enabled", False):
            return cfg

    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}

    # 2. Fallback: inline backends.skvector section in skmemory.yaml
    inline = skmem.get("backends", {}).get("skvector", {})
    if inline and inline.get("enabled", False):
        ext_cfg_path = inline.get("config")
        if ext_cfg_path:
            resolved = Path(ext_cfg_path).expanduser()
            if resolved.exists():
                ext = _read_yaml_file(resolved)
                if ext and ext.get("enabled", False):
                    return ext
        if inline.get("host") or inline.get("url"):
            return inline

    # 3. Legacy flat keys: skvector_url / skvector_key / ... in skmemory.yaml
    #    Enabled when backends_enabled list includes 'skvector' (or key is present).
    backends_enabled = skmem.get("backends_enabled", [])
    url = skmem.get("skvector_url")
    if url and ("skvector" in backends_enabled or skmem.get("skvector_key")):
        return {
            "enabled": True,
            "url": url,
            "api_key": skmem.get("skvector_key"),
            "collection": skmem.get("skvector_collection", "skmemory"),
            "embedding": {
                "provider": "sentence_transformers",
                "model": skmem.get("skvector_embedding_model", "all-MiniLM-L6-v2"),
            },
        }

    return None


def _load_skgraph_config(config_dir: Path) -> dict | None:
    """Load skgraph config: try skgraph.yaml first, then skmemory.yaml inline,
    then skmemory.yaml flat keys (legacy migration path)."""
    # 1. Dedicated skgraph.yaml
    path = config_dir / "skgraph.yaml"
    if path.exists():
        cfg = _read_yaml_file(path)
        if cfg and cfg.get("enabled", False):
            return cfg

    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}

    # 2. Fallback: inline backends.skgraph section in skmemory.yaml
    inline = skmem.get("backends", {}).get("skgraph", {})
    if inline and inline.get("enabled", False):
        ext_cfg_path = inline.get("config")
        if ext_cfg_path:
            resolved = Path(ext_cfg_path).expanduser()
            if resolved.exists():
                ext = _read_yaml_file(resolved)
                if ext and ext.get("enabled", False):
                    return ext
        if inline.get("host") or inline.get("url"):
            return inline

    # 3. Legacy flat keys: skgraph_url / skgraph_graph_name / ... in skmemory.yaml
    backends_enabled = skmem.get("backends_enabled", [])
    url = skmem.get("skgraph_url")
    if url and ("skgraph" in backends_enabled or skmem.get("skgraph_graph_name")):
        return {
            "enabled": True,
            "url": url,
            "graph_name": skmem.get("skgraph_graph_name", "skmemory"),
        }

    return None


def _load_recall_collections(config_dir: Path) -> list[str]:
    """Return recall_collections list from skmemory.yaml (for cross-index search)."""
    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}
    return skmem.get("recall_collections", [])


def _make_ollama_embed_fn(model: str, base_url: str):
    """Return an embedding function that calls the Ollama /api/embeddings endpoint."""
    import urllib.request

    def embed(text: str) -> list[float]:
        body = json.dumps({"model": model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return data.get("embedding", [])
        except Exception as e:
            logger.warning("Ollama embedding failed: %s", e)
            return []

    return embed


def _skvector_url(cfg: dict) -> str:
    """Resolve the Qdrant URL from config dict.  Accepts either a top-level
    ``url`` field or the ``host`` / ``port`` / ``https`` combination."""
    if "url" in cfg:
        return cfg["url"]
    host = cfg.get("host", "localhost")
    port = cfg.get("port", 6333)
    scheme = "https" if cfg.get("https", False) else "http"
    return f"{scheme}://{host}:{port}"


def _skgraph_url(cfg: dict) -> str:
    """Resolve the FalkorDB/Redis URL from config dict.

    Accepts:
    - Top-level ``url`` field (used as-is)
    - ``host`` / ``port`` / ``password`` combination (password is
      URL-decoded so YAML authors don't need to double-encode)
    """
    if "url" in cfg:
        return cfg["url"]
    host = cfg.get("host", "localhost")
    port = cfg.get("port", 6379)
    raw_password = cfg.get("password") or cfg.get("passwd")
    if raw_password:
        # Decode URL-encoded chars (e.g. %2B → +, %2F → /, %3D → =)
        password = unquote(str(raw_password))
        # Re-encode only the characters that break URL parsing
        safe_password = password.replace("@", "%40").replace(":", "%3A")
        return f"redis://:{safe_password}@{host}:{port}"
    return f"redis://{host}:{port}"


def _build_skvector_backend(skvector_cfg: dict) -> Any | None:
    """Instantiate SKVectorBackend from config dict.

    Accepts both ``url`` and ``host``/``port``/``https`` styles.
    Embedding provider ``ollama`` injects an Ollama embed_fn so
    sentence-transformers is not required.
    """
    try:
        from .backends.skvector_backend import SKVectorBackend

        url = _skvector_url(skvector_cfg)
        api_key = (
            skvector_cfg.get("api_key")
            or skvector_cfg.get("api-key")
            or skvector_cfg.get("apiKey")
        )
        collection = skvector_cfg.get("collection_name") or skvector_cfg.get("collection", "skmemory")
        embed_cfg = skvector_cfg.get("embedding", {})
        provider = embed_cfg.get("provider", "sentence_transformers")
        model = embed_cfg.get("model", "all-MiniLM-L6-v2")
        embed_fn = None

        if provider == "ollama":
            ollama_url = embed_cfg.get("url", "http://localhost:11434")
            embed_fn = _make_ollama_embed_fn(model, ollama_url)

        return SKVectorBackend(
            url=url,
            api_key=api_key,
            collection=collection,
            embed_fn=embed_fn,
        )
    except Exception as e:
        logger.warning("Could not build SKVectorBackend: %s", e)
        return None


def _build_skgraph_backend(skgraph_cfg: dict) -> Any | None:
    """Instantiate SKGraphBackend from config dict.

    Accepts both ``url`` and ``host``/``port``/``password`` styles.
    URL-encoded passwords (e.g. ``eiCn%2BMz0%3D``) are decoded before
    being embedded in the connection URL.
    """
    try:
        from .backends.skgraph_backend import SKGraphBackend

        url = _skgraph_url(skgraph_cfg)
        graph_name = skgraph_cfg.get("graph_name") or skgraph_cfg.get("graph", "skmemory")
        return SKGraphBackend(url=url, graph_name=graph_name)
    except Exception as e:
        logger.warning("Could not build SKGraphBackend: %s", e)
        return None


class LazyMemoryLoader:
    """Efficiently loads memories based on date tiers."""

    def __init__(self, agent_name: str | None = None):
        self.agent_name = agent_name
        self.paths = get_agent_paths(agent_name)
        self.today = datetime.now().date()
        self.db = SQLiteBackend(str(self.paths["base"] / "memory"))
        self._vector_backend = None
        self._recall_qdrant_backend = None
        self._graph_backend = None
        self._recall_collections: list[str] = []
        self._backends_loaded = False

    def load_active_context(self) -> MemoryContext:
        """Load token-optimized context for current session.

        Returns:
            MemoryContext with today (full), yesterday (summaries), historical (count)
        """
        today = self._load_today()
        self._ensure_backends()
        today = self._enrich_with_graph_context(today)
        return MemoryContext(
            today_memories=today,
            yesterday_summaries=self._load_yesterday_summaries(),
            historical_count=self._count_historical(),
        )

    def _load_today(self) -> list[dict]:
        """Load today's memories with full content."""
        today_str = self.today.isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content, tags, emotional_signature
                FROM memories
                WHERE DATE(created_at) = ?
                  AND layer = 'short'
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (today_str,),
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "tags": json.loads(row[3]) if row[3] else [],
                    "emotional": json.loads(row[4]) if row[4] else {},
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logger.error(f"Failed to load today's memories: {e}")
            return []

    def _load_yesterday_summaries(self) -> list[dict]:
        """Load yesterday's memories as summaries only."""
        yesterday = (self.today - timedelta(days=1)).isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, summary, tags
                FROM memories
                WHERE DATE(created_at) = ?
                  AND layer IN ('short', 'medium')
                ORDER BY importance DESC
                LIMIT 20
                """,
                (yesterday,),
            )
            memories = []
            for row in cursor.fetchall():
                mem = {
                    "id": row[0],
                    "title": row[1],
                    "summary": row[2] or self._generate_summary(row[1]),
                    "tags": json.loads(row[3]) if row[3] else [],
                }
                memories.append(mem)
            return memories
        except Exception as e:
            logger.error(f"Failed to load yesterday's summaries: {e}")
            return []

    def _count_historical(self) -> int:
        """Count older memories (not loaded into context)."""
        yesterday = (self.today - timedelta(days=1)).isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT COUNT(*) FROM memories
                WHERE DATE(created_at) < ?
                """,
                (yesterday,),
            )
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to count historical memories: {e}")
            return 0

    def _generate_summary(self, content: str, sentences: int = 2) -> str:
        """Generate a brief summary (fallback if no summary stored)."""
        # Simple truncation-based summary
        words = content.split()[:30]  # First 30 words
        return " ".join(words) + "..." if len(words) >= 30 else content

    def _ensure_backends(self) -> None:
        """Lazy-load vector and graph backends from agent config (once).

        Prefers ChromaDB (local, embedded) over Qdrant. Falls back to
        Qdrant if ChromaDB is unavailable or not installed.
        When ChromaDB is primary, Qdrant is loaded separately as
        _recall_qdrant_backend for shared recall_collections queries.
        """
        if self._backends_loaded:
            return
        self._backends_loaded = True
        config_dir = self.paths["config"]

        # Always load skvector config — needed for recall_collections even when Chroma is primary
        skvector_cfg = _load_skvector_config(config_dir)

        # Try ChromaDB first (local, zero-config)
        chroma_ok = False
        try:
            from .backends.chroma_backend import SKChromaBackend
            persist_dir = str(self.paths["base"] / "memory" / "chroma")
            state_path = self.paths["base"] / "memory" / "chroma-state.json"
            self._vector_backend = SKChromaBackend(
                persist_dir=persist_dir,
                state_path=state_path,
            )
            chroma_ok = True
        except Exception as e:
            logger.warning("context_loader.py: %s", e)
            pass

        if not chroma_ok:
            # Fall back to Qdrant as primary vector backend
            if skvector_cfg:
                self._vector_backend = _build_skvector_backend(skvector_cfg)
        elif skvector_cfg:
            # ChromaDB is primary — but load Qdrant separately for recall_collections
            self._recall_qdrant_backend = _build_skvector_backend(skvector_cfg)

        skgraph_cfg = _load_skgraph_config(config_dir)
        if skgraph_cfg:
            self._graph_backend = _build_skgraph_backend(skgraph_cfg)

        self._recall_collections = _load_recall_collections(config_dir)

        # Resolve recall_collections through env aliasing
        env = (skvector_cfg or {}).get("env", "prod")
        if env != "prod":
            self._recall_collections = [
                f"{col}-{env}" if not col.endswith(f"-{env}") else col
                for col in self._recall_collections
            ]

    def deep_search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search ALL memory tiers including vector and graph backends.

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            List of full memory details
        """
        self._ensure_backends()
        seen_ids: set[str] = set()
        results = []

        # 1. SQLite full-text search (always available)
        for r in self._search_sqlite(query):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                r.setdefault("source_backend", "sqlite")
                results.append(r)

        # 2. SKVector semantic search (primary collection + recall_collections)
        if self._vector_backend is not None:
            try:
                vector_hits = self._vector_backend.search_text(query, limit=max_results)
                for mem in vector_hits:
                    if mem.id not in seen_ids:
                        seen_ids.add(mem.id)
                        results.append({
                            "id": mem.id,
                            "title": mem.title,
                            "content": mem.content,
                            "summary": getattr(mem, "summary", None),
                            "tags": mem.tags,
                            "layer": mem.layer.value if hasattr(mem.layer, "value") else str(mem.layer),
                            "created_at": mem.created_at,
                            "source_backend": "skvector",
                        })
            except Exception as e:
                logger.warning("SKVector deep_search failed: %s", e)

            # Also search recall_collections (cross-agent/cross-project indexes)
            # Uses the Qdrant backend (shared collections like hammertime-v3, opus-memory).
            # When ChromaDB is primary, _recall_qdrant_backend holds the Qdrant client.
            _recall_backend = self._recall_qdrant_backend or (
                self._vector_backend
                if self._vector_backend is not None and hasattr(self._vector_backend, "_client")
                and hasattr(getattr(self._vector_backend, "_client", None), "query_points")
                else None
            )
            if self._recall_collections and _recall_backend is not None and _recall_backend._ensure_initialized():
                embedding = _recall_backend._embed(query)
                if embedding:
                    for recall_col in self._recall_collections:
                        try:
                            scored_points = _recall_backend._client.query_points(
                                collection_name=recall_col,
                                query=embedding,
                                limit=max_results,
                            ).points
                            for sp in scored_points:
                                payload = sp.payload or {}
                                raw = payload.get("memory_json")
                                if raw:
                                    try:
                                        from .models import Memory
                                        mem = Memory.model_validate_json(raw)
                                        if mem.id not in seen_ids:
                                            seen_ids.add(mem.id)
                                            results.append({
                                                "id": mem.id,
                                                "title": mem.title,
                                                "content": mem.content,
                                                "summary": getattr(mem, "summary", None),
                                                "tags": mem.tags,
                                                "layer": mem.layer.value if hasattr(mem.layer, "value") else str(mem.layer),
                                                "created_at": mem.created_at,
                                                "source_backend": f"skvector:{recall_col}",
                                            })
                                    except Exception as e:
                                        logger.warning("context_loader.py: %s", e)
                                        # Payload from foreign collection may not be a Memory
                                        # Fall back to raw payload fields
                                        mem_id = payload.get("id", str(sp.id))
                                        if mem_id not in seen_ids:
                                            seen_ids.add(mem_id)
                                            results.append({
                                                "id": mem_id,
                                                "title": payload.get("title", ""),
                                                "content": payload.get("content", payload.get("text", "")),
                                                "summary": payload.get("summary"),
                                                "tags": payload.get("tags", []),
                                                "layer": payload.get("layer", "unknown"),
                                                "created_at": payload.get("created_at", ""),
                                                "source_backend": f"skvector:{recall_col}",
                                            })
                        except Exception as e:
                            logger.warning("SKVector recall_collection '%s' failed: %s", recall_col, e)

        # 3. SKGraph title + tag search (if configured)
        if self._graph_backend is not None:
            try:
                graph_hits = self._graph_backend.search(query, limit=max_results)
                for hit in graph_hits:
                    if hit["id"] not in seen_ids:
                        seen_ids.add(hit["id"])
                        hit["source_backend"] = "skgraph"
                        results.append(hit)
                # Also search by tags (split query into words)
                tags = [w for w in query.split() if len(w) > 2]
                if tags:
                    tag_hits = self._graph_backend.search_by_tags(tags, limit=max_results)
                    for hit in tag_hits:
                        if hit["id"] not in seen_ids:
                            seen_ids.add(hit["id"])
                            hit["source_backend"] = "skgraph_tags"
                            results.append(hit)
            except Exception as e:
                logger.warning("SKGraph deep_search failed: %s", e)

        # Compute fusion scores and sort
        query_terms = [w.lower() for w in query.split() if len(w) > 2]
        for r in results:
            r["_fusion_score"] = self._compute_fusion_score(r, query, query_terms)

        # Sort by fusion score descending
        results = sorted(results, key=lambda x: x.get("_fusion_score", 0), reverse=True)
        return results[:max_results]

    def _compute_fusion_score(self, result: dict, query: str, query_terms: list[str]) -> float:
        """Compute hybrid fusion score combining text match, authority, and recency."""
        import math
        from datetime import datetime, timezone

        # 1. Text overlap score (BM25-ish: title > content)
        title = result.get("title", "").lower()
        content = (result.get("content", "") or result.get("content_preview", "")).lower()
        title_hits = sum(1 for t in query_terms if t in title)
        content_hits = sum(1 for t in query_terms if t in content)
        text_score = min(1.0, (title_hits * 0.4 + content_hits * 0.1) / max(1, len(query_terms)))

        # 2. Authority weight
        from .retrieval import AUTHORITY_WEIGHTS
        tier = result.get("authority_tier", "memory")
        authority_score = AUTHORITY_WEIGHTS.get(tier, 0.35)

        # 3. Time decay (half-life: 7d short, 30d mid, 365d long)
        half_life = {
            "short-term": 7, "short": 7,
            "mid-term": 30, "mid": 30,
            "long-term": 365, "long": 365,
        }.get(result.get("layer", "short-term"), 30)
        try:
            created = result.get("created_at", "")
            if created:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - dt).days
                decay = math.pow(0.5, age_days / half_life)
            else:
                decay = 1.0
        except Exception as e:
            logger.warning("context_loader.py: %s", e)
            decay = 1.0

        # 4. Backend bonus (vector results carry semantic signal)
        backend = result.get("source_backend", "sqlite")
        backend_bonus = {"skvector": 0.15, "skgraph": 0.05, "sqlite": 0.0}.get(
            backend.split(":")[0], 0.0
        )

        # Weighted fusion
        return (0.35 * text_score + 0.30 * authority_score + 0.20 * decay + 0.15) + backend_bonus

    def _enrich_with_graph_context(self, memories: list[dict]) -> list[dict]:
        """Add graph neighbourhood to top memories for richer context."""
        if self._graph_backend is None:
            return memories
        for mem in memories[:5]:  # only top 5 to avoid token bloat
            try:
                graph_ctx = self._graph_backend.get_context_graph(mem["id"], depth=1)
                if graph_ctx.get("related"):
                    mem["related_context"] = [
                        {"title": r["title"], "layer": r["layer"], "edge": r["edge_type"]}
                        for r in graph_ctx["related"][:3]
                    ]
                if graph_ctx.get("entities"):
                    mem["entities"] = graph_ctx["entities"][:5]
            except Exception as e:
                logger.warning("context_loader.py: %s", e)
                pass
        return memories

    def sync_backends(self) -> dict:
        """Sync all flat-file memories to vector and graph backends.

        Returns dict with stats: indexed, skipped, removed, errors per backend.
        """
        self._ensure_backends()
        stats = {}

        if self._vector_backend is not None:
            mem_dir = self.paths["base"] / "memory"
            result = self._vector_backend.sync_all(mem_dir, self.agent_name or "default")
            stats["skvector"] = result

        return stats

    def _search_sqlite(self, query: str) -> list[dict]:
        """Search SQLite for memories matching query."""
        try:
            pattern = f"%{query}%"
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content_preview, summary, tags, layer, created_at
                FROM memories
                WHERE title LIKE ? OR content_preview LIKE ? OR tags LIKE ?
                ORDER BY
                    CASE
                        WHEN title LIKE ? THEN 3
                        WHEN content_preview LIKE ? THEN 2
                        ELSE 1
                    END DESC,
                    created_at DESC
                LIMIT 50
                """,
                (pattern, pattern, pattern, pattern, pattern),
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "summary": row[3],
                    "tags": (json.loads(row[4]) if row[4] and row[4].startswith("[") else []),
                    "layer": row[5],
                    "created_at": row[6],
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logger.error(f"Failed to search SQLite: {e}")
            return []

    def get_memory_by_id(self, memory_id: str) -> dict | None:
        """Load full memory details by ID (for deep recall).

        Args:
            memory_id: UUID of the memory

        Returns:
            Full memory dict or None
        """
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content, summary, tags,
                       emotional_signature, layer, created_at
                FROM memories
                WHERE id = ?
                """,
                (memory_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "summary": row[3],
                    "tags": json.loads(row[4]) if row[4] else [],
                    "emotional": json.loads(row[5]) if row[5] else {},
                    "layer": row[6],
                    "created_at": row[7],
                }
        except Exception as e:
            logger.error(f"Failed to get memory {memory_id}: {e}")
        return None

    def promote_memory(self, memory_id: str, to_layer: str) -> bool:
        """Promote memory to different tier and generate summary.

        Args:
            memory_id: Memory to promote
            to_layer: Target layer ('short', 'medium', 'long')

        Returns:
            True if successful
        """
        try:
            # Get memory content
            memory = self.get_memory_by_id(memory_id)
            if not memory:
                return False

            # Generate summary if promoting to medium/long
            if to_layer in ("medium", "long") and not memory.get("summary"):
                summary = self._generate_summary(memory["content"], 2)

                # Update in database
                self.db._conn.execute(
                    """
                    UPDATE memories
                    SET layer = ?, summary = ?
                    WHERE id = ?
                    """,
                    (to_layer, summary, memory_id),
                )
                self.db._conn.commit()

                # Also move flat file
                self._move_flat_file(memory_id, to_layer)

                logger.info(f"Promoted memory {memory_id} to {to_layer}")
                return True

        except Exception as e:
            logger.error(f"Failed to promote memory {memory_id}: {e}")

        return False

    def _move_flat_file(self, memory_id: str, to_layer: str):
        """Move memory flat file to appropriate tier directory."""
        # Find current location
        for layer in ["short", "medium", "long"]:
            src = self.paths["memory_" + layer] / f"{memory_id}.json"
            if src.exists():
                dst = self.paths["memory_" + to_layer] / f"{memory_id}.json"
                src.rename(dst)
                logger.debug(f"Moved {src} -> {dst}")
                break


def get_context_for_session(agent_name: str | None = None) -> str:
    """Convenience function: get token-optimized context.

    Usage:
        context = get_context_for_session("lumina")
        # Returns formatted string with today's + yesterday's summaries
    """
    loader = LazyMemoryLoader(agent_name)
    context = loader.load_active_context()
    return context.to_context_string()
