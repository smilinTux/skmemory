"""
AGE — graph relationship backend (Level 2), powered by Apache AGE.

Apache AGE is a Postgres extension providing a Cypher-queryable property
graph on top of the same `skmem-pg` Postgres instance that already hosts
`PGVectorBackend`. Unlike the (deprecated) FalkorDB-backed
:class:`~skmemory.backends.skgraph_backend.SKGraphBackend`, this backend
targets the **live, populated per-agent graphs** (``lumina_knowledge``,
``opus_knowledge``, ``personal_history``, ...) that already exist in
skmem-pg with the shared ontology described in
``docs/superpowers/specs/2026-07-03-age-graph-backend-design.md``.

Connection: ``SKMEMORY_PG_DSN`` (defaults to the local skmem-pg container).
Graph selection: ``f"{agent}_knowledge"`` where agent comes from
``SKAGENT`` / ``SKMEMORY_AGENT`` / ``SKCAPSTONE_AGENT`` (default ``lumina``).

This backend is SUPPLEMENTARY — it indexes relationships alongside the
primary backend. For CRUD of full memory content, always use the primary
backend (SQLite / file / PGVector). For relationship traversal and
cluster discovery, use this one.

Graph schema (matches the existing live ontology):

    (:Memory)  — core node, keyed by id, carries the full memory fields
    (:Tag)     — tag node, keyed by name
    (:Source)  — source node (mcp, cli, seed, session, ...)
    (:Entity)  — entity node extracted from content/decomposition

    (:Memory)-[:TAGGED_WITH]->(:Tag)
    (:Memory)-[:FROM_SOURCE]->(:Source)
    (:Memory)-[:MENTIONS]->(:Entity)
    (:Memory)-[:RELATED_TO]->(:Memory)
    (:Memory)-[:SUPERSEDES]->(:Memory)   -- promotion / lineage chain

Idempotency: every write uses ``MERGE`` (never bare ``CREATE``) so
re-indexing the same memory never duplicates nodes or edges — this is
the single most important correctness property of this backend (see
the MERGE-idempotency test in ``tests/test_age_backend.py``).

Error handling: connection or AGE failures are logged and degrade to
the "safe empty" value (``False`` / ``[]`` / ``{}`` / ``None``). This
backend NEVER raises out of a public method — callers (notably
``store.py``) wrap graph calls but should never actually need the
try/except to fire because of this backend.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..models import Memory

logger = logging.getLogger(__name__)

# Node-LOCAL writable DSN. Canonical model (prb-6f069c5e): skmem-pg is local,
# per-node, and rebuildable from source. It is NOT streaming-replicated, NOT a
# central/shared system of record, and NOT a SPOF. Each node runs its OWN writable
# skmem-pg on localhost:5432 (fleet-wide uniform port, env-free); agents connect only
# to localhost. Per-node override = SKMEMORY_PG_DSN (default localhost:5432).
#
# ALIGNMENT: the pgvector backend, this AGE backend, and skmemory/reconcile.py MUST
# resolve to the SAME node-local port/DB. Do not let them drift (e.g. one on :5432 and
# another on the retired :5433 standby port) or the vector and graph indexes for the
# same node end up in different databases. The `lumina_knowledge`-class graph is a
# derived cache: rebuild it from the synced flat JSON via `sync_all`, never a remote
# primary/replica.
DEFAULT_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory"
)

# Graph names are interpolated directly into SQL text (the `cypher('{graph}', ...)`
# call takes a literal, not a bind parameter for the graph name) so we validate
# it's a plain identifier before ever using it in a query.
_VALID_GRAPH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# agtype text values come back as JSON with an optional trailing type tag,
# e.g. '{"id": ..., "label": "Memory", "properties": {...}}::vertex' or '"foo"'.
_AGTYPE_SUFFIX_RE = re.compile(r"::(vertex|edge|path)\s*$")

# Lightweight entity extraction for memories without decomposition metadata,
# mirroring SKGraphBackend._extract_and_index_entities.
_ENTITY_PATTERN = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b")
_ENTITY_SKIP_WORDS = {"The", "This", "That", "These", "Those", "When", "Where", "What"}


def _default_agent() -> str:
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or "lumina"
    )


def _now_iso() -> str:
    """Current UTC instant as an ISO-8601 string (transaction time).

    Same format the :class:`~skmemory.models.Memory` model stamps into
    ``created_at`` / ``updated_at`` (``datetime.now(timezone.utc).isoformat()``),
    so the bitemporal ``recorded_at`` / ``valid_to`` edge props compare
    lexicographically against node ``created_at`` values (fixed-width UTC
    ISO strings with a ``+00:00`` offset sort in true chronological order).
    """
    return datetime.now(timezone.utc).isoformat()


def _unique(values: Any) -> list[str]:
    """Case-insensitive de-duplicate while preserving order, stringifying and
    dropping blanks. Mirrors SKGraphBackend._ordered_unique."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        clean = str(value).strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


class AGEGraphBackend:
    """Graph relationship backend over Apache AGE (property graph in Postgres).

    Args:
        dsn: Postgres DSN. Defaults to ``SKMEMORY_PG_DSN`` env var, then the
            local skmem-pg container DSN.
        agent: Agent name used to derive the default graph. Defaults to
            ``SKAGENT`` / ``SKMEMORY_AGENT`` / ``SKCAPSTONE_AGENT`` / ``"lumina"``.
        graph: Explicit graph name. Defaults to ``f"{agent}_knowledge"``.
    """

    def __init__(
        self,
        dsn: str | None = None,
        agent: str | None = None,
        graph: str | None = None,
    ) -> None:
        self.dsn = dsn or DEFAULT_DSN
        self.agent = agent or _default_agent()
        requested_graph = graph or f"{self.agent}_knowledge"
        if _VALID_GRAPH_NAME.match(requested_graph):
            self.graph: str | None = requested_graph
        else:
            logger.warning(
                "AGEGraphBackend: refusing unsafe graph name %r; backend disabled",
                requested_graph,
            )
            self.graph = None
        self._conn_obj = None

    # ─────────────────────────────────────────────────────────
    # Connection + query primitives
    # ─────────────────────────────────────────────────────────

    def _conn(self):
        """Lazily open (or reuse) a psycopg connection with AGE loaded.

        Returns:
            The live connection, or None if unavailable.
        """
        if self.graph is None:
            return None
        if self._conn_obj is not None and not self._conn_obj.closed:
            return self._conn_obj
        try:
            import psycopg

            conn = psycopg.connect(self.dsn, autocommit=True)
            cur = conn.cursor()
            cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
            self._conn_obj = conn
            return conn
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: connection failed (%s): %s", self._safe_dsn(), exc)
            self._conn_obj = None
            return None

    def _safe_dsn(self) -> str:
        """DSN with any password redacted, for logging/health output."""
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", self.dsn)

    def _cypher(
        self, query: str, params: dict | None = None, cols: str = "v agtype"
    ) -> list[tuple]:
        """Run a Cypher query against the target graph.

        Args:
            query: Cypher query body (no surrounding ``$$``).
            params: Query parameters, passed as a single agtype map.
            cols: Column spec for the ``AS (...)`` clause, e.g. ``"m agtype"``
                or ``"id agtype, title agtype"``.

        Returns:
            list[tuple]: Raw fetched rows (agtype text values). Empty list
            on any failure — this method never raises.
        """
        conn = self._conn()
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            sql = f"SELECT * FROM cypher('{self.graph}', $$ {query} $$, %s) AS ({cols});"
            cur.execute(sql, (json.dumps(params or {}),))
            return cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: cypher failed (graph=%s): %s", self.graph, exc)
            try:
                if conn is not None and not conn.closed:
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return []

    def _agtype(self, value: Any) -> Any:
        """Parse an agtype text value into a Python value.

        Handles the ``::vertex`` / ``::edge`` / ``::path`` suffix AGE
        appends to structural types, then JSON-decodes the remainder.
        Non-string inputs pass through unchanged. Malformed input returns
        None (never raises).
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return None
        text = _AGTYPE_SUFFIX_RE.sub("", text).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("AGEGraphBackend: could not parse agtype value: %r", value)
            return None

    @staticmethod
    def _props(vertex: Any) -> dict:
        """Extract the property map from a parsed vertex/edge dict."""
        if isinstance(vertex, dict):
            props = vertex.get("properties")
            if isinstance(props, dict):
                return props
            # Already a bare property map (e.g. a hand-built dict in tests).
            if "id" in vertex or "label" not in vertex:
                return vertex
        return {}

    def _rows_to_dicts(self, rows: list[tuple], keys: list[str]) -> list[dict]:
        out = []
        for row in rows:
            values = [self._agtype(v) for v in row]
            out.append(dict(zip(keys, values, strict=False)))
        return out

    @staticmethod
    def _memory_dict(vertex: Any) -> dict | None:
        props = AGEGraphBackend._props(vertex)
        if not props:
            return None
        result = dict(props)
        meta_json = result.pop("metadata_json", None)
        if meta_json:
            try:
                result["metadata"] = json.loads(meta_json)
            except (json.JSONDecodeError, TypeError):
                result["metadata"] = {}
        else:
            result["metadata"] = {}
        return result

    @staticmethod
    def _entities_for(memory: Memory) -> list[str]:
        """Entities to MENTIONS-link: prefer decomposition metadata, else
        regex-extract from content (mirrors SKGraphBackend)."""
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        decomposition = metadata.get("decomposition") or {}
        entities = decomposition.get("entities") if isinstance(decomposition, dict) else None
        if entities:
            return _unique(entities)[:10]
        if len(memory.content) > 50:
            found = []
            for match in _ENTITY_PATTERN.finditer(memory.content):
                entity = match.group(1).strip()
                if entity not in _ENTITY_SKIP_WORDS and len(entity) > 3:
                    found.append(entity)
            return _unique(found)[:10]
        return []

    # ─────────────────────────────────────────────────────────
    # Write operations
    # ─────────────────────────────────────────────────────────

    def _merge_edge_to_named(
        self, mem_id: str, label: str, key: str, value: str, rel: str
    ) -> bool:
        """MERGE a `(label {key: value})` node and a `(Memory)-[:rel]->(node)` edge.

        `label`, `key`, `rel` are internal constants only (never user input),
        so simple concatenation is safe here — avoids f-string brace escaping.
        """
        query = (
            "MATCH (m:Memory {id: $mem_id}) "
            "MERGE (n:" + label + " {" + key + ": $value}) "
            "MERGE (m)-[:" + rel + "]->(n) "
            "RETURN n"
        )
        rows = self._cypher(query, {"mem_id": mem_id, "value": value}, cols="n agtype")
        return bool(rows)

    def _merge_related(self, a_id: str, b_id: str) -> bool:
        query = (
            "MATCH (a:Memory {id: $a_id}) "
            "MERGE (b:Memory {id: $b_id}) "
            "MERGE (a)-[:RELATED_TO]->(b) "
            "RETURN b"
        )
        rows = self._cypher(query, {"a_id": a_id, "b_id": b_id}, cols="b agtype")
        return bool(rows)

    def _merge_supersedes(
        self, child_id: str, parent_id: str, child_recorded_at: str | None = None
    ) -> bool:
        """MERGE a ``(child)-[:SUPERSEDES]->(parent)`` edge that CLOSES the
        parent's validity (bitemporal supersession).

        When ``child`` supersedes ``parent``, the parent fact stops being the
        currently-valid one. We record that on the SUPERSEDES edge with three
        bitemporal props (matching the ep-bitemporal-kg edge schema):

        * ``valid_to``   = the superseding memory's recorded instant
          (``child_recorded_at``). This is the moment the parent stopped being
          current — i.e. "set parent edge valid_to = new.recorded_at".
        * ``valid_from`` = the parent's own valid-time start (its
          ``created_at`` when known), so the edge spans the parent's whole
          currency window; falls back to ``valid_to`` (a degenerate window)
          only when the parent node has no ``created_at`` yet.
        * ``recorded_at``= transaction time: when we first learned of the
          supersession (index time), stamped once via ``coalesce`` so re-index
          keeps the original value.

        ``valid_to`` is derived from the child's stable ``created_at`` (not
        wall-clock ``now``), so re-indexing the same superseding memory is
        idempotent in both edge COUNT and edge PROP values.
        """
        recorded = child_recorded_at or _now_iso()
        query = (
            "MATCH (child:Memory {id: $child_id}) "
            "MERGE (parent:Memory {id: $parent_id}) "
            "MERGE (child)-[e:SUPERSEDES]->(parent) "
            "SET e.valid_to = $valid_to, "
            "e.recorded_at = coalesce(e.recorded_at, $now), "
            "e.valid_from = coalesce(e.valid_from, coalesce(parent.created_at, $valid_to)) "
            "RETURN parent"
        )
        rows = self._cypher(
            query,
            {
                "child_id": child_id,
                "parent_id": parent_id,
                "valid_to": recorded,
                "now": _now_iso(),
            },
            cols="parent agtype",
        )
        return bool(rows)

    def index_memory(self, memory: Memory) -> bool:
        """MERGE a memory node and all its relationships into the graph.

        Idempotent: safe to call repeatedly for the same memory — node and
        edge counts never grow on re-index (everything is MERGE-keyed).

        Edges created: ``FROM_SOURCE``, ``TAGGED_WITH`` (one per tag),
        ``RELATED_TO`` (from ``related_ids``), ``SUPERSEDES`` (from
        ``parent_id``), ``MENTIONS`` (from decomposition entities or a
        lightweight regex extraction).

        Args:
            memory: The memory to index.

        Returns:
            bool: True if the core Memory node was indexed successfully.
                False on any connection/AGE failure (never raises).
        """
        if self.graph is None:
            return False

        props = {
            "id": memory.id,
            "title": memory.title,
            "content": memory.content,
            "summary": memory.summary,
            "layer": memory.layer.value if hasattr(memory.layer, "value") else str(memory.layer),
            "role": memory.role.value if hasattr(memory.role, "value") else str(memory.role),
            "source": memory.source,
            "source_ref": memory.source_ref,
            "context_tag": memory.context_tag,
            "created_at": memory.created_at,
            "updated_at": memory.updated_at,
            "intensity": memory.emotional.intensity,
            "valence": memory.emotional.valence,
            "tags": list(memory.tags),
            "metadata_json": json.dumps(memory.metadata or {}, default=str),
        }
        query = (
            "MERGE (m:Memory {id: $id}) "
            "SET m.title = $title, m.content = $content, m.summary = $summary, "
            "m.layer = $layer, m.role = $role, m.source = $source, "
            "m.source_ref = $source_ref, m.context_tag = $context_tag, "
            "m.created_at = $created_at, m.updated_at = $updated_at, "
            "m.intensity = $intensity, m.valence = $valence, "
            "m.tags = $tags, m.metadata_json = $metadata_json "
            "RETURN m"
        )
        rows = self._cypher(query, props, cols="m agtype")
        if not rows:
            return False

        try:
            if memory.source:
                self._merge_edge_to_named(
                    memory.id, "Source", "name", memory.source, "FROM_SOURCE"
                )

            for tag in _unique(memory.tags):
                self._merge_edge_to_named(memory.id, "Tag", "name", tag, "TAGGED_WITH")

            for related_id in _unique(memory.related_ids):
                if related_id != memory.id:
                    self._merge_related(memory.id, related_id)

            if memory.parent_id and memory.parent_id != memory.id:
                # Supersession CLOSES the parent's validity: the parent edge's
                # valid_to is set to this (superseding) memory's recorded instant.
                self._merge_supersedes(
                    memory.id, memory.parent_id, child_recorded_at=memory.created_at
                )

            for entity in self._entities_for(memory):
                self._merge_edge_to_named(memory.id, "Entity", "name", entity, "MENTIONS")
        except Exception as exc:  # noqa: BLE001
            # The core node write already succeeded; a relationship failure
            # shouldn't fail the whole index — log and move on.
            logger.warning(
                "AGEGraphBackend: relationship indexing failed for %s: %s", memory.id, exc
            )

        return True

    def save(self, memory: Memory) -> str:
        """Store a memory node + relationships. Thin wrapper over index_memory.

        Args:
            memory: The Memory object to store as a graph node.

        Returns:
            str: The memory ID (unchanged), matching other backends' convention.
        """
        self.index_memory(memory)
        return memory.id

    def remove_memory(self, memory_id: str) -> bool:
        """Remove a memory node and all its incident edges (DETACH DELETE).

        Args:
            memory_id: The memory ID to remove.

        Returns:
            bool: True if the deletion query ran successfully (including the
                case where no such node existed). False only on connection/
                AGE failure.
        """
        if self.graph is None:
            return False
        conn = self._conn()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ "
                "MATCH (m:Memory {id: $id}) DETACH DELETE m RETURN 1"
                " $$, %s) AS (result agtype);"
            )
            cur.execute(sql, (json.dumps({"id": memory_id}),))
            cur.fetchall()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: remove_memory failed for %s: %s", memory_id, exc)
            try:
                if conn is not None and not conn.closed:
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False

    def delete(self, memory_id: str) -> bool:
        """Alias for :meth:`remove_memory`."""
        return self.remove_memory(memory_id)

    # ─────────────────────────────────────────────────────────
    # Read operations
    # ─────────────────────────────────────────────────────────

    def get(self, memory_id: str) -> dict | None:
        """Retrieve the full graph-stored fields for a memory by ID.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[dict]: Node properties (including parsed ``metadata``)
                if found, None otherwise.
        """
        if self.graph is None:
            return None
        rows = self._cypher(
            "MATCH (m:Memory {id: $id}) RETURN m",
            {"id": memory_id},
            cols="m agtype",
        )
        if not rows:
            return None
        vertex = self._agtype(rows[0][0])
        return self._memory_dict(vertex)

    def get_related(self, memory_id: str, depth: int = 2) -> list[dict]:
        """Traverse Memory-to-Memory relationship edges only, up to ``depth`` hops.

        Constrained to ``RELATED_TO`` and ``SUPERSEDES`` — the only edge
        types that connect ``(:Memory)-(:Memory)`` (see the graph schema
        docstring at the top of this module). Deliberately excludes
        ``TAGGED_WITH`` / ``FROM_SOURCE`` / ``MENTIONS``, which connect a
        Memory to a Tag / Source / Entity hub node respectively — walking
        through those would traverse ANY two memories that merely share a
        popular tag or source, exploding through hub nodes rather than
        reflecting real relatedness.

        Args:
            memory_id: Starting memory ID.
            depth: How many hops to traverse (1-5, clamped).

        Returns:
            list[dict]: Related memory stubs with ``id``, ``title``,
                ``layer``, ``distance`` (hop count), sorted by distance
                then title.
        """
        if self.graph is None:
            return []
        safe_depth = max(1, min(int(depth), 5))
        # Apache AGE's Cypher parser (1.7.x) does not support relationship-type
        # alternation (`-[:A|B*1..N]-`) in a pattern — it's a hard syntax
        # error, not just a semantic gap — so each Memory-to-Memory edge
        # type is queried separately and the results are merged here,
        # keeping the minimum hop-distance per related memory (matching
        # what a single combined query's `min(length(path))` would have
        # produced).
        merged: dict[str, dict] = {}
        for rel_type in ("RELATED_TO", "SUPERSEDES"):
            query = (
                "MATCH (start:Memory {id: $id}) "
                "MATCH path = (start)-[:"
                + rel_type
                + "*1.."
                + str(safe_depth)
                + "]-(related:Memory) "
                "WHERE related.id <> $id "
                "WITH related, min(length(path)) AS distance "
                "RETURN related.id, related.title, related.layer, distance"
            )
            rows = self._cypher(
                query,
                {"id": memory_id},
                cols="id agtype, title agtype, layer agtype, distance agtype",
            )
            for row in self._rows_to_dicts(rows, ["id", "title", "layer", "distance"]):
                rid = row["id"]
                existing = merged.get(rid)
                if existing is None or (row["distance"] or 0) < (existing["distance"] or 0):
                    merged[rid] = row

        results = sorted(merged.values(), key=lambda r: (r["distance"] or 0, r["title"] or ""))
        return results[:50]

    def traverse(self, memory_id: str, depth: int = 2) -> list[dict]:
        """Alias for :meth:`get_related` (matches SKGraphBackend's convention)."""
        return self.get_related(memory_id, depth=depth)

    def get_lineage(self, memory_id: str) -> list[dict]:
        """Walk the ``SUPERSEDES`` chain outward from a memory (its ancestors).

        Args:
            memory_id: Starting (most recent) memory ID.

        Returns:
            list[dict]: Ancestor memories with ``id``, ``title``, ``layer``,
                and ``depth`` (chain distance), nearest first.
        """
        if self.graph is None:
            return []
        query = (
            "MATCH (start:Memory {id: $id}) "
            "MATCH path = (start)-[:SUPERSEDES*1..10]->(anc:Memory) "
            "RETURN anc.id, anc.title, anc.layer, length(path) "
            "ORDER BY length(path) ASC"
        )
        rows = self._cypher(
            query, {"id": memory_id}, cols="id agtype, title agtype, layer agtype, depth agtype"
        )
        return self._rows_to_dicts(rows, ["id", "title", "layer", "depth"])

    # ─────────────────────────────────────────────────────────
    # Bitemporal / point-in-time reads (ep-bitemporal-kg)
    # ─────────────────────────────────────────────────────────

    def _superseded_ids_as_of(self, ts: str) -> set[str]:
        """IDs of memories whose validity was already CLOSED as of ``ts``.

        A memory is superseded-as-of-``ts`` iff it has an incoming
        ``SUPERSEDES`` edge whose ``valid_to`` is non-null and ``<= ts``.
        (Timestamps are fixed-width UTC ISO strings, so the ``<=`` compares
        chronologically.) Split out from :meth:`currently_valid_memories` and
        done as a separate query — rather than an ``EXISTS {}`` subquery or a
        ``WHERE`` on an ``OPTIONAL MATCH`` — because AGE 1.x's Cypher subset
        does not reliably support either construct.
        """
        if self.graph is None:
            return set()
        rows = self._cypher(
            "MATCH (:Memory)-[e:SUPERSEDES]->(p:Memory) "
            "WHERE e.valid_to IS NOT NULL AND e.valid_to <= $ts "
            "RETURN DISTINCT p.id",
            {"ts": ts},
            cols="id agtype",
        )
        out: set[str] = set()
        for row in rows:
            pid = self._agtype(row[0])
            if pid:
                out.add(pid)
        return out

    def currently_valid_memories(
        self, as_of: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Memories that are the currently-valid fact as of ``as_of`` (default now).

        A memory is valid at time ``T`` iff:

        * it has come into being — its ``created_at`` is null or ``<= T``; and
        * it has NOT been superseded as of ``T`` — no incoming ``SUPERSEDES``
          edge carries a ``valid_to <= T``.

        Because a superseding memory CLOSES its parent's validity (the parent's
        incoming SUPERSEDES edge gets ``valid_to = child.created_at``), the
        superseded parent drops out of this current view while remaining in the
        graph for history and lineage traversal. Passing an ``as_of`` in the
        past returns the *then*-valid set: before a supersession the parent is
        still valid and the (not-yet-created) child is excluded.

        Args:
            as_of: ISO-8601 UTC timestamp to evaluate validity at. ``None``
                (default) means "now" — the currently-valid set.
            limit: Maximum candidate memories to scan/return.

        Returns:
            list[dict]: Valid memory stubs (``id``, ``title``, ``layer``,
                ``created_at``), superseded ones excluded.
        """
        if self.graph is None:
            return []
        ts = as_of or _now_iso()
        superseded = self._superseded_ids_as_of(ts)
        rows = self._cypher(
            "MATCH (m:Memory) "
            "WHERE m.created_at IS NULL OR m.created_at <= $ts "
            "RETURN m.id, m.title, m.layer, m.created_at "
            "LIMIT $limit",
            {"ts": ts, "limit": int(limit)},
            cols="id agtype, title agtype, layer agtype, created_at agtype",
        )
        out: list[dict] = []
        for row in self._rows_to_dicts(rows, ["id", "title", "layer", "created_at"]):
            if row["id"] in superseded:
                continue
            out.append(row)
        return out

    def is_currently_valid(self, memory_id: str, as_of: str | None = None) -> bool:
        """Whether ``memory_id`` is a currently-valid (non-superseded) fact.

        Convenience over :meth:`currently_valid_memories`: returns True iff the
        memory exists, has come into being by ``as_of`` (default now), and has
        not been superseded as of that time.
        """
        if self.graph is None:
            return False
        ts = as_of or _now_iso()
        if memory_id in self._superseded_ids_as_of(ts):
            return False
        rows = self._cypher(
            "MATCH (m:Memory {id: $id}) "
            "WHERE m.created_at IS NULL OR m.created_at <= $ts "
            "RETURN m.id",
            {"id": memory_id, "ts": ts},
            cols="id agtype",
        )
        return bool(rows)

    def search_by_tags(self, tags: list[str], limit: int = 20) -> list[dict]:
        """Find memories sharing any of the given tags (OR logic).

        Args:
            tags: Tag names to search for.
            limit: Maximum results.

        Returns:
            list[dict]: Matching memory stubs (``id``, ``title``, ``layer``).
        """
        if self.graph is None or not tags:
            return []
        query = (
            "UNWIND $tags AS tag "
            "MATCH (t:Tag {name: tag})<-[:TAGGED_WITH]-(m:Memory) "
            "RETURN DISTINCT m.id, m.title, m.layer "
            "LIMIT $limit"
        )
        rows = self._cypher(
            query,
            {"tags": list(tags), "limit": int(limit)},
            cols="id agtype, title agtype, layer agtype",
        )
        return self._rows_to_dicts(rows, ["id", "title", "layer"])

    def search_by_entity(self, query: str, limit: int = 20) -> list[dict]:
        """Find memories mentioning an entity matching ``query`` (case-insensitive substring).

        Args:
            query: Entity name or substring to search for.
            limit: Maximum results.

        Returns:
            list[dict]: Matching memory stubs with the matched entity name.
        """
        if self.graph is None or not query or not query.strip():
            return []
        pattern = "(?i).*" + re.escape(query.strip()) + ".*"
        cyq = (
            "MATCH (e:Entity) WHERE e.name =~ $pattern "
            "MATCH (m:Memory)-[:MENTIONS]->(e) "
            "RETURN DISTINCT m.id, m.title, m.layer, e.name "
            "LIMIT $limit"
        )
        rows = self._cypher(
            cyq,
            {"pattern": pattern, "limit": int(limit)},
            cols="id agtype, title agtype, layer agtype, entity agtype",
        )
        return self._rows_to_dicts(rows, ["id", "title", "layer", "entity"])

    def find_clusters(self, min_size: int = 3) -> list[dict]:
        """Find Memory nodes with at least ``min_size`` distinct neighbours (any edge type).

        Args:
            min_size: Minimum number of direct neighbours to count as a hub.

        Returns:
            list[dict]: Cluster hubs with ``id``, ``title``, ``layer``,
                ``connections``, ordered by connections descending.
        """
        if self.graph is None:
            return []
        cyq = (
            "MATCH (m:Memory)-[r]-(x) "
            "WITH m, count(DISTINCT x) AS connections "
            "WHERE connections >= $min_size "
            "RETURN m.id, m.title, m.layer, connections "
            "ORDER BY connections DESC LIMIT 50"
        )
        rows = self._cypher(
            cyq,
            {"min_size": int(min_size)},
            cols="id agtype, title agtype, layer agtype, connections agtype",
        )
        return self._rows_to_dicts(rows, ["id", "title", "layer", "connections"])

    def get_context_graph(self, memory_id: str, depth: int = 2) -> dict:
        """Return the N-hop neighbourhood plus tags/entities as a structured dict.

        Args:
            memory_id: Center memory ID.
            depth: How many hops to traverse.

        Returns:
            dict: ``{center_id, related, tags, entities}``, or ``{}`` if the
                memory isn't found / on failure.
        """
        if self.graph is None:
            return {}
        related = self.get_related(memory_id, depth=depth)
        if not related and self.get(memory_id) is None:
            return {}
        tag_rows = self._cypher(
            "MATCH (m:Memory {id: $id})-[:TAGGED_WITH]->(t:Tag) RETURN t.name",
            {"id": memory_id},
            cols="name agtype",
        )
        tags = [self._agtype(r[0]) for r in tag_rows]
        entity_rows = self._cypher(
            "MATCH (m:Memory {id: $id})-[:MENTIONS]->(e:Entity) RETURN e.name LIMIT 10",
            {"id": memory_id},
            cols="name agtype",
        )
        entities = [self._agtype(r[0]) for r in entity_rows]
        return {"center_id": memory_id, "related": related, "tags": tags, "entities": entities}

    # ─────────────────────────────────────────────────────────
    # Ops / introspection
    # ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return graph statistics: node/edge counts, broken down by label/type.

        Returns:
            dict: ``{ok, backend, graph, node_count, edge_count,
                nodes_by_label, edges_by_type}``, or ``{ok: False, error}``
                on failure.
        """
        if self.graph is None:
            return {"ok": False, "backend": "AGEGraphBackend", "error": "invalid graph name"}
        conn = self._conn()
        if conn is None:
            return {"ok": False, "backend": "AGEGraphBackend", "error": "connection failed"}
        try:
            node_rows = self._cypher(
                "MATCH (n) RETURN label(n) AS lbl, count(n) AS c", cols="lbl agtype, c agtype"
            )
            nodes_by_label = {self._agtype(lbl): self._agtype(c) for lbl, c in node_rows}
            edge_rows = self._cypher(
                "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c", cols="t agtype, c agtype"
            )
            edges_by_type = {self._agtype(t): self._agtype(c) for t, c in edge_rows}
            return {
                "ok": True,
                "backend": "AGEGraphBackend",
                "graph": self.graph,
                "node_count": sum(nodes_by_label.values()) if nodes_by_label else 0,
                "edge_count": sum(edges_by_type.values()) if edges_by_type else 0,
                "nodes_by_label": nodes_by_label,
                "edges_by_type": edges_by_type,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: stats failed: %s", exc)
            return {"ok": False, "backend": "AGEGraphBackend", "error": str(exc)}

    def health_check(self) -> dict:
        """Check AGE backend connectivity and graph size.

        Returns:
            dict: ``{ok, backend, dsn (redacted), graph, node_count}`` on
                success, ``{ok: False, ..., error}`` otherwise.
        """
        if self.graph is None:
            return {"ok": False, "backend": "AGEGraphBackend", "error": "invalid graph name"}
        conn = self._conn()
        if conn is None:
            return {
                "ok": False,
                "backend": "AGEGraphBackend",
                "dsn": self._safe_dsn(),
                "graph": self.graph,
                "error": "connection failed",
            }
        try:
            rows = self._cypher("MATCH (n) RETURN count(n)", cols="c agtype")
            node_count = self._agtype(rows[0][0]) if rows else 0
            return {
                "ok": True,
                "backend": "AGEGraphBackend",
                "dsn": self._safe_dsn(),
                "graph": self.graph,
                "node_count": node_count,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: health_check failed: %s", exc)
            return {
                "ok": False,
                "backend": "AGEGraphBackend",
                "dsn": self._safe_dsn(),
                "graph": self.graph,
                "error": str(exc),
            }

    def sync_all(self, flat_files_dir, agent_name: str) -> dict:
        """Backfill the graph from flat-file memories on disk.

        Walks ``<flat_files_dir>/{short,mid,long}-term/*.json`` and calls
        :meth:`index_memory` for each. Idempotent (MERGE semantics) so
        re-running is safe.

        Args:
            flat_files_dir: Path to the agent's memory directory.
            agent_name: Used only for log lines.

        Returns:
            dict: ``{"indexed": N, "errors": K}``.
        """
        from pathlib import Path

        stats = {"indexed": 0, "errors": 0}
        if self.graph is None:
            logger.warning("AGEGraphBackend sync_all: invalid graph name for %s", agent_name)
            return stats

        base = Path(flat_files_dir)
        for tier in ("short-term", "mid-term", "long-term"):
            tier_dir = base / tier
            if not tier_dir.exists():
                continue
            for json_file in tier_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    memory = Memory.model_validate(data)
                    if self.index_memory(memory):
                        stats["indexed"] += 1
                    else:
                        stats["errors"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "AGEGraphBackend sync_all: failed on %s: %s", json_file.name, exc
                    )
                    stats["errors"] += 1

        logger.info(
            "AGEGraphBackend sync_all for '%s': indexed=%d errors=%d",
            agent_name,
            stats["indexed"],
            stats["errors"],
        )
        return stats

    # ─────────────────────────────────────────────────────────
    # Deferred (v1 scope boundary — see design doc)
    # ─────────────────────────────────────────────────────────

    def search_by_claim(self, query: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError(
            "AGEGraphBackend.search_by_claim is deferred to a follow-on pass "
            "(decomposition Claim/Citation/Section wiring not built in v1)."
        )

    def search_by_section(self, query: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError(
            "AGEGraphBackend.search_by_section is deferred to a follow-on pass."
        )

    def search_by_citation(self, query: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError(
            "AGEGraphBackend.search_by_citation is deferred to a follow-on pass."
        )

    def related_claims_by_citation(self, query: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError(
            "AGEGraphBackend.related_claims_by_citation is deferred to a follow-on pass."
        )

    def update_emotional(
        self, memory_id: str, intensity: float, valence: float, labels: list[str]
    ) -> bool:
        raise NotImplementedError(
            "AGEGraphBackend.update_emotional is deferred — re-run index_memory() "
            "with the updated Memory object instead."
        )
