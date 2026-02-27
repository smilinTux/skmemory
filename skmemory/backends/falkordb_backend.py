"""
FalkorDB graph backend (Level 2 — relationships).

Enables graph-based memory traversal: "What memories are connected
to this person?" or "Show me the seed lineage chain." Uses the
Cypher query language over a Redis-compatible protocol.

Requires:
    pip install falkordb

FalkorDB is the successor to RedisGraph. Run locally via Docker
or point to an external instance.

This backend is SUPPLEMENTARY — it indexes relationships alongside
the primary backend (SQLite or file). It does not store full memory
content, only the graph edges and key metadata for traversal.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger(__name__)


class FalkorDBBackend:
    """FalkorDB graph backend for memory relationship traversal.

    Not a full BaseBackend — this is a supplementary index for
    graph queries. The primary backend handles CRUD.

    Args:
        url: FalkorDB/Redis URL (default: localhost:6379).
        graph_name: Name of the graph (default: 'skmemory').
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        graph_name: str = "skmemory",
    ) -> None:
        self.url = url
        self.graph_name = graph_name
        self._db = None
        self._graph = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        """Lazy-initialize the FalkorDB connection.

        Returns:
            bool: True if connection succeeded.
        """
        if self._initialized:
            return True

        try:
            from falkordb import FalkorDB
        except ImportError:
            logger.warning("falkordb not installed: pip install falkordb")
            return False

        try:
            self._db = FalkorDB.from_url(self.url)
            self._graph = self._db.select_graph(self.graph_name)
            self._initialized = True
            return True
        except Exception as e:
            logger.warning("FalkorDB connection failed: %s", e)
            return False

    def index_memory(self, memory: Memory) -> bool:
        """Add a memory node and its relationships to the graph.

        Args:
            memory: The memory to index.

        Returns:
            bool: True if indexed successfully.
        """
        if not self._ensure_initialized():
            return False

        try:
            self._graph.query(
                """
                MERGE (m:Memory {id: $id})
                SET m.title = $title,
                    m.layer = $layer,
                    m.source = $source,
                    m.intensity = $intensity,
                    m.created_at = $created_at
                """,
                {
                    "id": memory.id,
                    "title": memory.title,
                    "layer": memory.layer.value,
                    "source": memory.source,
                    "intensity": memory.emotional.intensity,
                    "created_at": memory.created_at,
                },
            )

            if memory.parent_id:
                self._graph.query(
                    """
                    MATCH (child:Memory {id: $child_id})
                    MERGE (parent:Memory {id: $parent_id})
                    MERGE (child)-[:PROMOTED_FROM]->(parent)
                    """,
                    {"child_id": memory.id, "parent_id": memory.parent_id},
                )

            for related_id in memory.related_ids:
                self._graph.query(
                    """
                    MATCH (a:Memory {id: $a_id})
                    MERGE (b:Memory {id: $b_id})
                    MERGE (a)-[:RELATED_TO]->(b)
                    """,
                    {"a_id": memory.id, "b_id": related_id},
                )

            for tag in memory.tags:
                self._graph.query(
                    """
                    MATCH (m:Memory {id: $mem_id})
                    MERGE (t:Tag {name: $tag})
                    MERGE (m)-[:TAGGED]->(t)
                    """,
                    {"mem_id": memory.id, "tag": tag},
                )

            if memory.source == "seed":
                creator = next(
                    (t.split(":", 1)[1] for t in memory.tags if t.startswith("creator:")),
                    None,
                )
                if creator:
                    self._graph.query(
                        """
                        MATCH (m:Memory {id: $mem_id})
                        MERGE (a:AI {name: $creator})
                        MERGE (a)-[:PLANTED]->(m)
                        """,
                        {"mem_id": memory.id, "creator": creator},
                    )

            return True
        except Exception as e:
            logger.warning("FalkorDB index failed: %s", e)
            return False

    def get_related(self, memory_id: str, depth: int = 2) -> list[dict]:
        """Traverse the graph to find related memories.

        Args:
            memory_id: Starting memory ID.
            depth: How many hops to traverse (1-5).

        Returns:
            list[dict]: Related memory nodes with relationship info.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                f"""
                MATCH (start:Memory {{id: $id}})
                MATCH path = (start)-[*1..{min(depth, 5)}]-(related:Memory)
                WHERE related.id <> $id
                RETURN DISTINCT related.id AS id,
                       related.title AS title,
                       related.layer AS layer,
                       related.intensity AS intensity,
                       length(path) AS distance
                ORDER BY distance ASC, related.intensity DESC
                LIMIT 20
                """,
                {"id": memory_id},
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "intensity": row[3],
                    "distance": row[4],
                }
                for row in result.result_set
            ]
        except Exception as e:
            logger.warning("FalkorDB query failed: %s", e)
            return []

    def get_lineage(self, memory_id: str) -> list[dict]:
        """Get the promotion/seed lineage chain for a memory.

        Args:
            memory_id: Starting memory ID.

        Returns:
            list[dict]: Chain of ancestor memories.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                """
                MATCH (start:Memory {id: $id})
                MATCH path = (start)-[:PROMOTED_FROM*1..10]->(ancestor:Memory)
                RETURN ancestor.id AS id,
                       ancestor.title AS title,
                       ancestor.layer AS layer,
                       length(path) AS depth
                ORDER BY depth ASC
                """,
                {"id": memory_id},
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "depth": row[3],
                }
                for row in result.result_set
            ]
        except Exception as e:
            logger.warning("FalkorDB lineage query failed: %s", e)
            return []

    def get_memory_clusters(self, min_connections: int = 2) -> list[dict]:
        """Find clusters of highly connected memories.

        Args:
            min_connections: Minimum edges to be considered a cluster center.

        Returns:
            list[dict]: Cluster centers with connection counts.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                """
                MATCH (m:Memory)-[r]-(connected:Memory)
                WITH m, count(DISTINCT connected) AS connections
                WHERE connections >= $min
                RETURN m.id AS id,
                       m.title AS title,
                       m.layer AS layer,
                       connections
                ORDER BY connections DESC
                LIMIT 20
                """,
                {"min": min_connections},
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "connections": row[3],
                }
                for row in result.result_set
            ]
        except Exception as e:
            logger.warning("FalkorDB cluster query failed: %s", e)
            return []

    def remove_memory(self, memory_id: str) -> bool:
        """Remove a memory node and all its relationships from the graph.

        Args:
            memory_id: The memory ID to remove.

        Returns:
            bool: True if removed successfully.
        """
        if not self._ensure_initialized():
            return False

        try:
            self._graph.query(
                "MATCH (m:Memory {id: $id}) DETACH DELETE m",
                {"id": memory_id},
            )
            return True
        except Exception as e:
            logger.warning("FalkorDB remove failed: %s", e)
            return False

    def search_by_tags(self, tags: list[str], limit: int = 20) -> list[dict]:
        """Find memories that share the given tags via graph edges.

        Args:
            tags: Tag names to search for (OR logic — any match).
            limit: Maximum results.

        Returns:
            list[dict]: Matching memory nodes with tag overlap count.
        """
        if not self._ensure_initialized():
            return []

        if not tags:
            return []

        try:
            result = self._graph.query(
                """
                MATCH (m:Memory)-[:TAGGED]->(t:Tag)
                WHERE t.name IN $tags
                WITH m, collect(DISTINCT t.name) AS matched_tags
                RETURN m.id AS id,
                       m.title AS title,
                       m.layer AS layer,
                       m.intensity AS intensity,
                       matched_tags,
                       size(matched_tags) AS tag_overlap
                ORDER BY tag_overlap DESC, m.intensity DESC
                LIMIT $limit
                """,
                {"tags": tags, "limit": limit},
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "intensity": row[3],
                    "matched_tags": row[4],
                    "tag_overlap": row[5],
                }
                for row in result.result_set
            ]
        except Exception as e:
            logger.warning("FalkorDB tag search failed: %s", e)
            return []

    def health_check(self) -> dict:
        """Check FalkorDB backend health.

        Returns:
            dict: Status with connection and graph info.
        """
        if not self._ensure_initialized():
            return {
                "ok": False,
                "backend": "FalkorDBBackend",
                "error": "Not initialized",
            }

        try:
            result = self._graph.query(
                "MATCH (n) RETURN count(n) AS nodes"
            )
            node_count = result.result_set[0][0] if result.result_set else 0
            return {
                "ok": True,
                "backend": "FalkorDBBackend",
                "url": self.url,
                "graph": self.graph_name,
                "node_count": node_count,
            }
        except Exception as e:
            return {
                "ok": False,
                "backend": "FalkorDBBackend",
                "error": str(e),
            }
