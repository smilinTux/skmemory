"""
SKGraph — graph relationship backend (Level 2).

Powered by FalkorDB. Enables graph-based memory traversal: "What memories
are connected to this moment?" or "Show me the seed lineage chain." Uses
the Cypher query language over a Redis-compatible protocol.

Requires:
    pip install skmemory[skgraph]

FalkorDB is the successor to RedisGraph. Run locally via Docker
or point to an external instance. Connection URL is read from the
``SKMEMORY_SKGRAPH_URL`` environment variable, defaulting to
``redis://localhost:6379``.

This backend is SUPPLEMENTARY — it indexes relationships alongside
the primary backend (SQLite or file). It stores key metadata and
graph edges for traversal, not full memory content. For CRUD,
always use the primary backend. For relationship traversal and
cluster discovery, use this one.

Graph schema:

    (:Memory)  — core node, keyed by memory id
    (:Tag)     — tag node, keyed by name
    (:Source)  — source node (mcp, cli, seed, session, …)
    (:AI)      — AI creator node for seed memories

    (:Memory)-[:TAGGED]->(:Tag)
    (:Memory)-[:FROM_SOURCE]->(:Source)
    (:Memory)-[:RELATED_TO]->(:Memory)
    (:Memory)-[:PROMOTED_FROM]->(:Memory)
    (:Memory)-[:PRECEDED_BY]->(:Memory)
    (:AI)-[:PLANTED]->(:Memory)
    (:Memory)-[:MENTIONS]->(:Entity)
    (:Memory)-[:CITES]->(:Citation)
    (:Memory)-[:ASSERTS]->(:Claim)
    (:Memory)-[:IN_SECTION]->(:Section)
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from .. import graph_queries as Q
from ..models import Memory

logger = logging.getLogger(__name__)

DEFAULT_URL = os.environ.get("SKMEMORY_SKGRAPH_URL", "redis://localhost:6379")


class SKGraphBackend:
    """SKGraph — graph backend for memory relationship indexing and traversal.

    Powered by FalkorDB. Not a full ``BaseBackend`` — this is a supplementary
    graph index. The primary backend (SQLite / file) handles CRUD. This
    backend adds graph edges so you can ask questions like:
    "Which memories are most connected to this session?" or
    "What did Opus plant before this seed?"

    Args:
        url: SKGraph connection URL.  Reads ``SKMEMORY_SKGRAPH_URL``
            env var by default, falling back to ``redis://localhost:6379``.
        graph_name: Name of the graph (default: ``'skmemory'``).
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        graph_name: str = "skmemory",
    ) -> None:
        self.url = url
        self.graph_name = graph_name
        self._db = None
        self._graph = None
        self._initialized = False

    # ─────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────

    def _ensure_initialized(self) -> bool:
        """Lazy-initialise the FalkorDB connection.

        Returns:
            bool: True if the connection is ready, False otherwise.
        """
        if self._initialized:
            return True

        try:
            from falkordb import FalkorDB  # type: ignore[import]
        except ImportError:
            logger.warning("falkordb not installed: pip install skmemory[skgraph]")
            return False

        try:
            self._db = FalkorDB.from_url(self.url)
            self._graph = self._db.select_graph(self.graph_name)
            self._initialized = True
            logger.debug("SKGraph connected: %s / %s", self.url, self.graph_name)
            return True
        except Exception as exc:
            logger.warning("SKGraph connection failed: %s", exc)
            return False

    # ─────────────────────────────────────────────────────────
    # Write operations
    # ─────────────────────────────────────────────────────────

    def save(self, memory: Memory) -> str:
        """Store a memory node with properties in the graph.

        Creates or updates the Memory node and its edges: TAGGED,
        FROM_SOURCE, RELATED_TO, PROMOTED_FROM, PRECEDED_BY, and PLANTED
        for seed memories created by AI instances.

        This is a thin wrapper around :meth:`index_memory` that also
        returns the memory ID, matching the convention used by other
        backends.

        Args:
            memory: The Memory object to store as a graph node.

        Returns:
            str: The memory ID (unchanged).
        """
        self.index_memory(memory)
        return memory.id

    def index_memory(self, memory: Memory) -> bool:
        """Add a memory node and all its relationships to the graph.

        Graph edges created:

        * ``(Memory)-[:TAGGED]->(Tag)`` — one per tag
        * ``(Memory)-[:FROM_SOURCE]->(Source)`` — the origin system
        * ``(Memory)-[:RELATED_TO]->(Memory)`` — explicit related_ids
        * ``(Memory)-[:PROMOTED_FROM]->(Memory)`` — if parent_id is set
        * ``(Memory)-[:PRECEDED_BY]->(Memory)`` — the previous memory
          from the same source (temporal chain)
        * ``(AI)-[:PLANTED]->(Memory)`` — for seed memories with a
          ``creator:<name>`` tag

        After creating tag edges, any existing memories with 2+ shared
        tags are automatically linked via ``RELATED_TO``.

        Args:
            memory: The memory to index.

        Returns:
            bool: True if indexed successfully, False on failure.
        """
        if not self._ensure_initialized():
            return False

        try:
            # Core Memory node upsert
            self._graph.query(
                Q.UPSERT_MEMORY,
                {
                    "id": memory.id,
                    "title": memory.title,
                    "layer": memory.layer.value,
                    "source": memory.source,
                    "source_ref": memory.source_ref,
                    "intensity": memory.emotional.intensity,
                    "valence": memory.emotional.valence,
                    "created_at": memory.created_at,
                    "updated_at": memory.updated_at,
                },
            )

            # PROMOTED_FROM edge (promotion lineage)
            if memory.parent_id:
                self._graph.query(
                    Q.CREATE_PROMOTED_FROM,
                    {"child_id": memory.id, "parent_id": memory.parent_id},
                )

            # RELATED_TO edges (explicit relationships)
            for related_id in memory.related_ids:
                self._graph.query(
                    Q.CREATE_RELATED_TO,
                    {"a_id": memory.id, "b_id": related_id},
                )

            # TAGGED edges (one per tag)
            for tag in memory.tags:
                self._graph.query(
                    Q.CREATE_TAGGED,
                    {"mem_id": memory.id, "tag": tag},
                )

            # Auto-wire shared-tag neighbours (overlap >= 2)
            self._graph.query(
                Q.CREATE_SHARED_TAG_RELATED,
                {"a_id": memory.id},
            )

            # FROM_SOURCE edge
            self._graph.query(
                Q.CREATE_FROM_SOURCE,
                {"mem_id": memory.id, "source": memory.source},
            )

            # PRECEDED_BY temporal edge — link to the most recent prior
            # memory from the same source so sessions form a chain.
            prev_result = self._graph.query(
                Q.FIND_PREVIOUS_FROM_SOURCE,
                {"source": memory.source, "exclude_id": memory.id},
            )
            if prev_result.result_set:
                prev_id = prev_result.result_set[0][0]
                self._graph.query(
                    Q.CREATE_PRECEDED_BY,
                    {"later_id": memory.id, "earlier_id": prev_id},
                )

            # PLANTED edge for AI seed memories
            if memory.source == "seed":
                creator = next(
                    (t.split(":", 1)[1] for t in memory.tags if t.startswith("creator:")),
                    None,
                )
                if creator:
                    self._graph.query(
                        Q.CREATE_PLANTED,
                        {"mem_id": memory.id, "creator": creator},
                    )

            # Decomposition-derived structure edges
            decomposition = memory.metadata.get("decomposition", {})
            section_title = decomposition.get("section_title")
            if section_title:
                self._graph.query(
                    Q.CREATE_IN_SECTION,
                    {"mem_id": memory.id, "section": section_title},
                )
            for section in decomposition.get("section_titles", []):
                self._graph.query(
                    Q.CREATE_IN_SECTION,
                    {"mem_id": memory.id, "section": section},
                )

            for entity in decomposition.get("entities", []):
                self._graph.query(
                    Q.CREATE_MENTIONS_ENTITY,
                    {"mem_id": memory.id, "entity": entity},
                )

            for citation in decomposition.get("citations", []):
                self._graph.query(
                    Q.CREATE_CITES,
                    {"mem_id": memory.id, "citation": citation},
                )

            for claim in decomposition.get("claims", []):
                self._graph.query(
                    Q.CREATE_ASSERTS,
                    {"mem_id": memory.id, "claim": claim},
                )

            return True
        except Exception as exc:
            logger.warning("SKGraph index failed: %s", exc)
            return False

    # ─────────────────────────────────────────────────────────
    # Read operations
    # ─────────────────────────────────────────────────────────

    def get(self, memory_id: str) -> dict | None:
        """Retrieve the graph node properties for a memory by ID.

        Returns only the properties stored in the graph (no full content).
        For the full Memory object use the primary backend.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[dict]: Node properties if found, None otherwise.
        """
        if not self._ensure_initialized():
            return None

        try:
            result = self._graph.query(
                Q.GET_MEMORY_BY_ID,
                {"id": memory_id},
            )
            if not result.result_set:
                return None
            row = result.result_set[0]
            return {
                "id": row[0],
                "title": row[1],
                "layer": row[2],
                "source": row[3],
                "source_ref": row[4],
                "intensity": row[5],
                "valence": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
        except Exception as exc:
            logger.warning("SKGraph get failed: %s", exc)
            return None

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search on memory titles stored in the graph.

        Performs a case-insensitive substring match against the ``title``
        property of all Memory nodes. For full-content search use the
        primary backend or the Qdrant vector backend.

        Args:
            query: Search string (case-insensitive substring match).
            limit: Maximum number of results to return.

        Returns:
            list[dict]: Matching memory node stubs, sorted by
                emotional intensity descending.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                Q.SEARCH_BY_TITLE,
                {"query": query, "limit": limit},
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "intensity": row[3],
                    "created_at": row[4],
                }
                for row in result.result_set
            ]
        except Exception as exc:
            logger.warning("SKGraph search failed: %s", exc)
            return []

    def search_by_tags(self, tags: list[str], limit: int = 20) -> list[dict]:
        """Find memories sharing any of the given tags via graph edges.

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
                Q.SEARCH_BY_TAGS,
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
        except Exception as exc:
            logger.warning("SKGraph tag search failed: %s", exc)
            return []

    def search_by_entity(self, query: str, limit: int = 20) -> list[dict]:
        """Find memories mentioning entities extracted during decomposition."""
        return self._search_by_structure(Q.SEARCH_BY_ENTITY, query, limit)

    def search_by_citation(self, query: str, limit: int = 20) -> list[dict]:
        """Find memories citing a legal or document reference."""
        return self._search_by_structure(Q.SEARCH_BY_CITATION, query, limit)

    def search_by_claim(self, query: str, limit: int = 20) -> list[dict]:
        """Find memories asserting a claim matching the query."""
        return self._search_by_structure(Q.SEARCH_BY_CLAIM, query, limit)

    def search_by_section(self, query: str, limit: int = 20) -> list[dict]:
        """Find memories associated with a decomposed section title."""
        return self._search_by_structure(Q.SEARCH_BY_SECTION, query, limit)

    def related_claims_by_entity(self, query: str, limit: int = 20) -> list[dict]:
        """Find claims supported by memories mentioning an entity."""
        return self._search_related_claims(Q.RELATED_CLAIMS_BY_ENTITY, query, limit)

    def related_claims_by_citation(self, query: str, limit: int = 20) -> list[dict]:
        """Find claims supported by memories citing a citation."""
        return self._search_related_claims(Q.RELATED_CLAIMS_BY_CITATION, query, limit)

    def _search_by_structure(self, query_template: str, query: str, limit: int) -> list[dict]:
        if not self._ensure_initialized():
            return []
        if not query.strip():
            return []
        try:
            result = self._graph.query(
                query_template,
                {"query": query, "limit": limit},
            )
            rows = [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "intensity": row[3],
                    "matched_value": row[4],
                    "canonical_id": row[5],
                    "canonical_title": row[6],
                    "canonical_layer": row[7],
                    "canonical_intensity": row[8],
                    "is_chunk": row[9],
                }
                for row in result.result_set
            ]
            return self._collapse_canonical_results(rows)
        except Exception as exc:
            logger.warning("SKGraph structured search failed: %s", exc)
            return []

    def _search_related_claims(self, query_template: str, query: str, limit: int) -> list[dict]:
        if not self._ensure_initialized():
            return []
        if not query.strip():
            return []
        try:
            result = self._graph.query(
                query_template,
                {"query": query, "limit": limit},
            )
            return [
                {
                    "claim": row[0],
                    "matched_value": row[1],
                    "support_count": row[2],
                    "memory_ids": row[3],
                    "memory_titles": row[4],
                }
                for row in result.result_set
            ]
        except Exception as exc:
            logger.warning("SKGraph related-claims search failed: %s", exc)
            return []

    def _collapse_canonical_results(self, results: list[dict]) -> list[dict]:
        """Collapse chunk-level matches into parent-level result groups."""
        grouped: OrderedDict[str, dict] = OrderedDict()
        for result in results:
            canonical_id = result.get("canonical_id") or result["id"]
            matched_value = result.get("matched_value")
            existing = grouped.get(canonical_id)
            if existing is None:
                collapsed = {
                    "id": canonical_id,
                    "title": result.get("canonical_title") or result["title"],
                    "layer": result.get("canonical_layer") or result["layer"],
                    "intensity": result.get("canonical_intensity") or result["intensity"],
                    "source_memory_ids": [result["id"]],
                    "match_count": 1,
                    "chunk_match_count": 1 if result.get("is_chunk") else 0,
                }
                if matched_value is not None:
                    collapsed["matched_values"] = [matched_value]
                grouped[canonical_id] = collapsed
                continue

            if matched_value is not None:
                existing.setdefault("matched_values", [])
                if matched_value not in existing["matched_values"]:
                    existing["matched_values"].append(matched_value)
            if result["id"] not in existing["source_memory_ids"]:
                existing["source_memory_ids"].append(result["id"])
            existing["match_count"] = len(existing["source_memory_ids"])
            if result.get("is_chunk"):
                existing["chunk_match_count"] += 1
            existing["intensity"] = max(existing["intensity"], result.get("canonical_intensity") or result["intensity"])

        collapsed = list(grouped.values())
        collapsed.sort(key=lambda row: (-row["intensity"], -row["match_count"], row["title"]))
        return collapsed

    def delete(self, memory_id: str) -> bool:
        """Remove a memory node and all its edges from the graph.

        This is an alias for :meth:`remove_memory` using the task-specified
        method name. Calls ``DETACH DELETE`` so all incident edges are
        removed atomically with the node.

        Args:
            memory_id: The memory ID to delete.

        Returns:
            bool: True if the deletion query ran successfully.
        """
        return self.remove_memory(memory_id)

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
                Q.DELETE_MEMORY,
                {"id": memory_id},
            )
            return True
        except Exception as exc:
            logger.warning("SKGraph remove failed: %s", exc)
            return False

    # ─────────────────────────────────────────────────────────
    # Graph traversal
    # ─────────────────────────────────────────────────────────

    def traverse(self, memory_id: str, depth: int = 2) -> list[dict]:
        """Traverse the graph to find memories connected to a starting node.

        Follows any edge type up to ``depth`` hops from the starting
        memory. Results are sorted by hop distance (closest first) then
        by emotional intensity descending.

        Args:
            memory_id: Starting memory ID.
            depth: Maximum traversal depth (1–5, clamped).

        Returns:
            list[dict]: Connected memory stubs with ``id``, ``title``,
                ``layer``, ``intensity``, and ``distance`` (hop count).
        """
        return self.get_related(memory_id, depth=depth)

    def get_related(self, memory_id: str, depth: int = 2) -> list[dict]:
        """Traverse the graph to find related memories by hop distance.

        Args:
            memory_id: Starting memory ID.
            depth: How many hops to traverse (1–5, clamped).

        Returns:
            list[dict]: Related memory nodes with relationship info.
        """
        if not self._ensure_initialized():
            return []

        safe_depth = max(1, min(depth, 5))
        try:
            result = self._graph.query(
                Q.TRAVERSE_RELATED.format(depth=safe_depth),
                {"id": memory_id},
            )
            rows = [
                {
                    "id": row[0],
                    "title": row[1],
                    "layer": row[2],
                    "intensity": row[3],
                    "distance": row[4],
                    "canonical_id": row[5],
                    "canonical_title": row[6],
                    "canonical_layer": row[7],
                    "canonical_intensity": row[8],
                    "is_chunk": row[9],
                }
                for row in result.result_set
            ]
            collapsed = self._collapse_canonical_results(rows)
            for item in collapsed:
                distances = [
                    row["distance"]
                    for row in rows
                    if (row.get("canonical_id") or row["id"]) == item["id"]
                ]
                item["distance"] = min(distances) if distances else safe_depth
            collapsed.sort(key=lambda row: (row["distance"], -row["intensity"], row["title"]))
            return collapsed
        except Exception as exc:
            logger.warning("SKGraph traversal failed: %s", exc)
            return []

    def get_lineage(self, memory_id: str) -> list[dict]:
        """Get the promotion / seed lineage chain for a memory.

        Walks ``PROMOTED_FROM`` edges upward to recover the full
        ancestry of a promoted memory.

        Args:
            memory_id: Starting memory ID.

        Returns:
            list[dict]: Chain of ancestor memories with ``depth`` field.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                Q.TRAVERSE_LINEAGE,
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
        except Exception as exc:
            logger.warning("SKGraph lineage query failed: %s", exc)
            return []

    # ─────────────────────────────────────────────────────────
    # Cluster discovery
    # ─────────────────────────────────────────────────────────

    def find_clusters(self, min_size: int = 3) -> list[dict]:
        """Find memory clusters by discovering highly connected hub nodes.

        A cluster is defined as a Memory node with at least ``min_size``
        direct neighbours (any edge type). Returns each hub with the
        count of its connections so callers can rank by centrality.

        Args:
            min_size: Minimum number of direct neighbours for a node to
                be considered a cluster hub (default: 3).

        Returns:
            list[dict]: Cluster hubs with ``id``, ``title``, ``layer``,
                and ``connections`` count, ordered by connections desc.
        """
        return self.get_memory_clusters(min_connections=min_size)

    def get_memory_clusters(self, min_connections: int = 2) -> list[dict]:
        """Find clusters of highly connected memories.

        Args:
            min_connections: Minimum edges to be considered a cluster centre.

        Returns:
            list[dict]: Cluster centres with connection counts.
        """
        if not self._ensure_initialized():
            return []

        try:
            result = self._graph.query(
                Q.FIND_CLUSTER_HUBS,
                {"min_connections": min_connections},
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
        except Exception as exc:
            logger.warning("SKGraph cluster query failed: %s", exc)
            return []

    # ─────────────────────────────────────────────────────────
    # Introspection
    # ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return graph statistics: node count, edge count, tag distribution.

        Returns:
            dict: Statistics with keys ``node_count``, ``edge_count``,
                ``memory_count``, ``tag_distribution`` (list of
                ``{tag, memory_count}`` dicts), and ``ok`` bool.
        """
        if not self._ensure_initialized():
            return {"ok": False, "error": "Not initialized"}

        try:
            node_result = self._graph.query(Q.COUNT_NODES)
            node_count = node_result.result_set[0][0] if node_result.result_set else 0

            edge_result = self._graph.query(Q.COUNT_EDGES)
            edge_count = edge_result.result_set[0][0] if edge_result.result_set else 0

            mem_result = self._graph.query(Q.COUNT_MEMORIES)
            memory_count = mem_result.result_set[0][0] if mem_result.result_set else 0

            tag_result = self._graph.query(Q.TAG_DISTRIBUTION)
            tag_distribution = [
                {"tag": row[0], "memory_count": row[1]} for row in tag_result.result_set
            ]

            return {
                "ok": True,
                "node_count": node_count,
                "edge_count": edge_count,
                "memory_count": memory_count,
                "tag_distribution": tag_distribution,
            }
        except Exception as exc:
            logger.warning("SKGraph stats failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def health_check(self) -> dict:
        """Check FalkorDB backend connectivity and graph size.

        Returns:
            dict: Status with ``ok``, ``backend``, ``url``, ``graph``,
                and ``node_count``. On failure returns ``ok: False``
                with an ``error`` key.
        """
        if not self._ensure_initialized():
            return {
                "ok": False,
                "backend": "SKGraphBackend",
                "error": "Not initialized",
            }

        try:
            result = self._graph.query(Q.COUNT_NODES)
            node_count = result.result_set[0][0] if result.result_set else 0
            return {
                "ok": True,
                "backend": "SKGraphBackend",
                "url": self.url,
                "graph": self.graph_name,
                "node_count": node_count,
            }
        except Exception as exc:
            return {
                "ok": False,
                "backend": "SKGraphBackend",
                "error": str(exc),
            }
