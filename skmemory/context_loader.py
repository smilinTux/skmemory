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
                sections.append(f"- {mem.get('title', 'Untitled')}: {content}")

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


def _load_skvector_config(config_dir: Path) -> dict | None:
    """Load agent's skvector.yaml if it exists and is enabled."""
    path = config_dir / "skvector.yaml"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        return cfg
    except Exception as e:
        logger.warning("Could not load skvector.yaml: %s", e)
        return None


def _load_skgraph_config(config_dir: Path) -> dict | None:
    """Load agent's skgraph.yaml if it exists and is enabled."""
    path = config_dir / "skgraph.yaml"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        return cfg
    except Exception as e:
        logger.warning("Could not load skgraph.yaml: %s", e)
        return None


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


def _build_skvector_backend(skvector_cfg: dict) -> Any | None:
    """Instantiate SKVectorBackend from an agent skvector.yaml dict."""
    try:
        from .backends.skvector_backend import SKVectorBackend

        host = skvector_cfg.get("host", "localhost")
        port = skvector_cfg.get("port", 6333)
        use_https = skvector_cfg.get("https", False)
        scheme = "https" if use_https else "http"
        url = f"{scheme}://{host}:{port}"
        api_key = skvector_cfg.get("api_key") or skvector_cfg.get("api-key")
        collection = skvector_cfg.get("collection_name", "skmemory")
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
    """Instantiate SKGraphBackend from an agent skgraph.yaml dict."""
    try:
        from .backends.skgraph_backend import SKGraphBackend

        host = skgraph_cfg.get("host", "localhost")
        port = skgraph_cfg.get("port", 6379)
        password = skgraph_cfg.get("password")
        graph_name = skgraph_cfg.get("graph_name", "skmemory")

        if password:
            url = f"redis://:{password}@{host}:{port}"
        else:
            url = f"redis://{host}:{port}"

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
        self._graph_backend = None
        self._backends_loaded = False

    def load_active_context(self) -> MemoryContext:
        """Load token-optimized context for current session.

        Returns:
            MemoryContext with today (full), yesterday (summaries), historical (count)
        """
        return MemoryContext(
            today_memories=self._load_today(),
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
        """Lazy-load vector and graph backends from agent config (once)."""
        if self._backends_loaded:
            return
        self._backends_loaded = True
        config_dir = self.paths["config"]

        skvector_cfg = _load_skvector_config(config_dir)
        if skvector_cfg:
            self._vector_backend = _build_skvector_backend(skvector_cfg)

        skgraph_cfg = _load_skgraph_config(config_dir)
        if skgraph_cfg:
            self._graph_backend = _build_skgraph_backend(skgraph_cfg)

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

        # 2. SKVector semantic search (if configured)
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

        # Sort by relevance across all backends
        results = sorted(
            results,
            key=lambda x: (
                x.get("content", "").lower().count(query.lower()),
                x.get("title", "").lower().count(query.lower()),
            ),
            reverse=True,
        )

        return results[:max_results]

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
