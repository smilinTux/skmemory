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

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from ..invalid_records import payload_memory_id, quarantine_invalid_flat_file, require_memory_id
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

#: Non-Memory node labels that only exist to hang edges off Memory nodes.
#: After a Memory prune they can be left with zero incident edges; the
#: guarded reconcile cleans them up (card c25e2513). Cleanup refuses any
#: label outside this set.
AUX_NODE_LABELS = ("Tag", "Source", "AI", "Entity", "Citation", "Claim", "Section")

#: Labels whose nodes are looked up by a property on EVERY index_memory()
#: (``Memory {id}``, ``Tag/Entity/Source {name}``). AGE compiles those
#: patterns to ``properties @> {...}``, which is a sequential scan without a
#: GIN index on ``properties``. On lumina (57k Memory nodes, ~10 lookups per
#: memory) that made a full sync_all take far longer than its 3h unit
#: timeout (measured 2026-10-27: 66-106 ms per Memory lookup, 0.4 ms with
#: the index). ensure_property_indexes() creates them idempotently.
PROPERTY_INDEXED_LABELS = ("Memory", "Tag", "Entity", "Source")

#: Bump to make the next sync_all re-index every memory once (for example
#: after changing which edges index_memory() writes). It is folded into each
#: node's ``sync_hash``, so an old hash simply stops matching.
GRAPH_SYNC_INDEX_VERSION = "1"


#: Stamp the open bitemporal interval on an edge ONLY when it has none yet.
#: This used to be an unconditional ``SET e.valid_from = coalesce(...)``:
#: a no-op by value on every re-index, but AGE still rewrote every edge row,
#: and that SET was ~97% of index_memory's time (measured 2026-10-27 on
#: lumina: 45-75 ms for a 7-tag memory with it, ~1 ms without). Filtering
#: first keeps the original stamps and writes nothing for existing edges.
#: Expects the edge bound as ``e`` and the far node as ``n``.
_STAMP_NEW_EDGE = (
    "WITH n, e WHERE e.valid_from IS NULL "
    "SET e.valid_from = $now, e.recorded_at = coalesce(e.recorded_at, $now) "
)


def _graph_sync_budget_s() -> float:
    """Wall-clock budget for one sync_all() pass, seconds (0 = unbounded).

    The graph is the last, slowest and least critical phase of
    ``skmemory sync``. Without a budget a large backlog held the whole unit
    until systemd killed it, so the cheap phases (SQLite reindex) only ran
    once per timeout instead of once per timer tick. A pass that runs out of
    budget stops between memories and reports the remainder as ``deferred``;
    the next pass resumes, newest memories first.
    """
    raw = (os.environ.get("SKMEMORY_GRAPH_SYNC_BUDGET_S") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "AGEGraphBackend: bad SKMEMORY_GRAPH_SYNC_BUDGET_S=%r, using default", raw
            )
    return 900.0


def graph_sync_hash(payload: bytes) -> str:
    """Fingerprint of one flat file as indexed into the graph.

    Content-addressed rather than a timestamp watermark: Syncthing preserves
    the SOURCE node's mtime, so a file that arrives late can carry an mtime
    older than any watermark and would be skipped forever. A per-node hash
    of the exact bytes that were indexed has no such blind spot.
    """
    digest = hashlib.sha256()
    digest.update(GRAPH_SYNC_INDEX_VERSION.encode())
    digest.update(b"\0")
    digest.update(payload)
    return digest.hexdigest()


def _search_timeout_ms() -> int:
    """Server-side statement timeout for graph SEARCH queries, milliseconds.

    The entity arm measured 10.72s of an 11.38s fused search on a real corpus
    (14,203 Entity nodes, 259,010 MENTIONS edges), because the entity lookup is
    a case-insensitive REGEX over a property AGE cannot index. Semantic was
    0.26s and BM25 0.27s in the same run, so one arm was 94% of the latency and
    made interactive search unusable.

    A deadline is the right answer rather than an index guess: pg_trgm is not
    installed and AGE's agtype blocks a plain functional index, so there is no
    index fix to reach for. The fusion layer already treats an arm that returns
    nothing as having abstained, so a capped arm degrades the result quality it
    contributes instead of blocking the query.

    0 or negative disables the cap. Only SEARCH paths pass it; mutation and
    the guarded reconcile reads deliberately run uncapped, because a truncated
    read there could be mistaken for "the graph is nearly empty" and drive a
    mass prune.
    """
    raw = (os.environ.get("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "AGEGraphBackend: bad SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS=%r, using default", raw
            )
    return 2500


class GraphTransportError(RuntimeError):
    """The AGE graph connection cannot carry queries; callers fail closed.

    Distinct from the backend's lenient degradation (empty results on
    failure): reconcile and parity paths must never mistake a dead graph
    for an empty one.
    """


# Lightweight entity extraction for memories without decomposition metadata,
# mirroring SKGraphBackend._extract_and_index_entities.
_ENTITY_PATTERN = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b")
_ENTITY_SKIP_WORDS = {"The", "This", "That", "These", "Those", "When", "Where", "What"}


def _default_agent() -> str:
    from ..agents import require_memory_profile

    return require_memory_profile().profile_id


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
        from ..agents import require_memory_profile

        self.dsn = dsn or DEFAULT_DSN
        self.agent = require_memory_profile(agent).profile_id if agent else _default_agent()
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

    def _cypher_strict(
        self,
        query: str,
        params: dict | None = None,
        cols: str = "v agtype",
        timeout_ms: int | None = None,
    ) -> list[tuple]:
        """Run a Cypher query, raising on any failure (fail-closed variant).

        The lenient :meth:`_cypher` cannot distinguish "the graph answered
        empty" from "the graph never answered". The guarded reconcile path
        needs that distinction, so this variant raises instead.

        Args:
            query: Cypher query body (no surrounding ``$$``).
            params: Query parameters, passed as a single agtype map.
            cols: Column spec for the ``AS (...)`` clause.

        Returns:
            list[tuple]: Raw fetched rows (agtype text values).

        Raises:
            GraphTransportError: Graph disabled, connection unavailable, or
                the query itself failed.
        """
        if self.graph is None:
            raise GraphTransportError("AGE graph disabled: invalid graph name")
        conn = self._conn()
        if conn is None:
            raise GraphTransportError(f"AGE connection unavailable ({self._safe_dsn()})")
        try:
            cur = conn.cursor()
            # SET LOCAL so the cap dies with this transaction and cannot leak
            # onto a later mutation sharing the same connection. Only callers
            # that pass timeout_ms get capped; everything else runs uncapped.
            if timeout_ms and timeout_ms > 0:
                # SET takes no bind parameters ("syntax error at or near $1"),
                # so the value is interpolated. int() is the whole sanitiser
                # and it is sufficient: a non-integer cannot survive it.
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            sql = f"SELECT * FROM cypher('{self.graph}', $$ {query} $$, %s) AS ({cols});"
            cur.execute(sql, (json.dumps(params or {}),))
            return cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            try:
                if conn is not None and not conn.closed:
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise GraphTransportError(f"AGE cypher failed (graph={self.graph}): {exc}") from exc

    def _cypher(
        self,
        query: str,
        params: dict | None = None,
        cols: str = "v agtype",
        timeout_ms: int | None = None,
    ) -> list[tuple]:
        """Run a Cypher query against the target graph.

        Args:
            query: Cypher query body (no surrounding ``$$``).
            params: Query parameters, passed as a single agtype map.
            cols: Column spec for the ``AS (...)`` clause, e.g. ``"m agtype"``
                or ``"id agtype, title agtype"``.

        Returns:
            list[tuple]: Raw fetched rows (agtype text values). Empty list
            on any failure; this method never raises.
        """
        try:
            return self._cypher_strict(query, params, cols, timeout_ms=timeout_ms)
        except GraphTransportError as exc:
            logger.warning("AGEGraphBackend: cypher failed (graph=%s): %s", self.graph, exc)
            return []

    def probe_connection(self) -> None:
        """Fail closed unless the graph connection answers a trivial query.

        The guarded reconcile path runs this before any report or mutation,
        so a dead graph can never be misread as "zero graph nodes" (which
        would trigger a mass backfill) or "zero flat files" (a mass prune).

        Raises:
            GraphTransportError: Graph disabled, connection unavailable, or
                the probe query failed.
        """
        if self.graph is None:
            raise GraphTransportError("AGE graph disabled: invalid graph name")
        conn = self._conn()
        if conn is None:
            raise GraphTransportError(f"AGE connection unavailable ({self._safe_dsn()})")
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1;")
        except Exception as exc:  # noqa: BLE001
            raise GraphTransportError(f"AGE probe failed ({self._safe_dsn()}): {exc}") from exc

    def graph_memory_ids(self) -> set[str]:
        """Every Memory node id in the graph.

        STRICT (raises :class:`GraphTransportError`): the parity report keys
        safety decisions off this set, so a failed read must never look like
        an empty graph.

        Returns:
            set[str]: All Memory node ids (empty set when the graph holds
            none).
        """
        rows = self._cypher_strict("MATCH (m:Memory) RETURN m.id", cols="id agtype")
        out: set[str] = set()
        for (value,) in rows:
            parsed = self._agtype(value)
            if isinstance(parsed, str) and parsed:
                out.add(parsed)
        return out

    def graph_sync_state(self) -> dict[str, str | None] | None:
        """Map Memory id -> stored ``sync_hash`` (None when the node has none).

        One scan of the Memory label. Lenient: returns None when the graph
        could not answer, and :meth:`sync_all` then treats every memory as
        needing an index, which is slow but never skips real work.
        """
        try:
            rows = self._cypher_strict(
                "MATCH (m:Memory) RETURN m.id, m.sync_hash",
                cols="id agtype, h agtype",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend: could not read graph sync state: %s", exc)
            return None
        out: dict[str, str | None] = {}
        for mid_raw, hash_raw in rows:
            mid = self._agtype(mid_raw)
            digest = self._agtype(hash_raw)
            if isinstance(mid, str) and mid:
                out[mid] = digest if isinstance(digest, str) else None
        return out

    def ensure_property_indexes(self) -> list[str]:
        """Create the GIN ``properties`` indexes index_memory() depends on.

        Idempotent. A label that does not exist yet is created first
        (``create_vlabel``) so a brand-new graph gets its indexes before the
        first backfill rather than after the slow one.

        Returns:
            list[str]: Names of indexes created by this call.

        Raises:
            GraphTransportError: Graph disabled or connection unavailable.
        """
        if self.graph is None:
            raise GraphTransportError("AGE graph disabled: invalid graph name")
        conn = self._conn()
        if conn is None:
            raise GraphTransportError(f"AGE connection unavailable ({self._safe_dsn()})")
        created: list[str] = []
        try:
            with conn.cursor() as cur:
                for label in PROPERTY_INDEXED_LABELS:
                    cur.execute(
                        "SELECT 1 FROM ag_catalog.ag_label l "
                        "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
                        "WHERE g.name::text = %s AND l.name::text = %s",
                        (self.graph, label),
                    )
                    if cur.fetchone() is None:
                        cur.execute("SELECT ag_catalog.create_vlabel(%s, %s)", (self.graph, label))
                    index_name = f"{label}_properties_gin"
                    cur.execute(
                        "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                        (self.graph, index_name),
                    )
                    if cur.fetchone() is not None:
                        continue
                    # graph is validated by _VALID_GRAPH_NAME and label is an
                    # internal constant, so interpolation is safe here.
                    cur.execute(
                        f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                        f'ON "{self.graph}"."{label}" USING gin (properties)'
                    )
                    created.append(index_name)
        except Exception as exc:  # noqa: BLE001
            self._rollback(conn)
            raise GraphTransportError(
                f"could not create property indexes on {self.graph!r}: {exc}"
            ) from exc
        if created:
            logger.info("AGEGraphBackend: created %s on %s", ", ".join(created), self.graph)
        return created

    def memory_node_strict(self, memory_id: str) -> dict | None:
        """Graph-stored fields for one memory, or None when absent. STRICT.

        Same payload shape as :meth:`get`, but transport failures raise
        :class:`GraphTransportError` instead of reading as a missing node.
        """
        rows = self._cypher_strict(
            "MATCH (m:Memory {id: $id}) RETURN m", {"id": memory_id}, cols="m agtype"
        )
        if not rows:
            return None
        return self._memory_dict(self._agtype(rows[0][0]))

    def memory_edge_inventory(self, memory_id: str) -> list[dict]:
        """Incident-edge inventory for one Memory node, for prune backups.

        STRICT. Returns one dict per incident edge (either direction):
        ``{"type", "other_label", "other_properties"}``.

        Args:
            memory_id: The Memory node id.

        Returns:
            list[dict]: Edge inventory rows (empty when the node has none
            or does not exist).
        """
        rows = self._cypher_strict(
            "MATCH (m:Memory {id: $id})-[r]-(o) "
            "RETURN type(r) AS rel, label(o) AS other_label, o AS other",
            {"id": memory_id},
            cols="rel agtype, other_label agtype, other agtype",
        )
        out: list[dict] = []
        for rel, other_label, other in rows:
            out.append(
                {
                    "type": self._agtype(rel),
                    "other_label": self._agtype(other_label),
                    "other_properties": self._props(self._agtype(other)),
                }
            )
        return out

    def _aux_ids(self, label: str) -> set[int]:
        """Internal AGE ids of every node carrying ``label``. STRICT."""
        rows = self._cypher_strict(f"MATCH (n:{label}) RETURN id(n)", cols="vid agtype")
        return {self._agtype(row[0]) for row in rows}

    def _aux_used_ids(self, label: str) -> set[int]:
        """Internal AGE ids of ``label`` nodes with at least one edge. STRICT."""
        rows = self._cypher_strict(
            f"MATCH (n:{label})-[r]-() RETURN DISTINCT id(n)", cols="vid agtype"
        )
        return {self._agtype(row[0]) for row in rows}

    def _orphan_aux_ids(self, label: str) -> set[int]:
        """Internal ids of ``label`` nodes left with zero incident edges.

        Computed as ``all minus used`` in Python because AGE 1.x does not
        reliably support pattern predicates (``WHERE NOT (n)--()``).
        """
        return self._aux_ids(label) - self._aux_used_ids(label)

    def count_orphaned_aux_nodes(self, labels: tuple[str, ...]) -> dict[str, int]:
        """Count zero-edge nodes per aux label without deleting. STRICT.

        Args:
            labels: Aux node labels (validated against AUX_NODE_LABELS).

        Returns:
            dict[str, int]: Orphan count per label, in label order.
        """
        return {label: len(self._orphan_aux_ids(label)) for label in labels}

    def delete_orphaned_aux_nodes(self, labels: tuple[str, ...]) -> dict[str, int]:
        """Delete aux nodes left with zero incident edges. STRICT.

        Per label: compute orphan internal ids, then ``DELETE`` each by id
        (same per-node pattern as :meth:`remove_memory`; no bulk ad-hoc
        Cypher). Any failure raises :class:`GraphTransportError` mid-loop so
        a partial cleanup is reported, never silently continued.

        Args:
            labels: Aux node labels (validated against AUX_NODE_LABELS).

        Returns:
            dict[str, int]: Deleted count per label.
        """
        out: dict[str, int] = {}
        for label in labels:
            if label not in AUX_NODE_LABELS:
                raise ValueError(f"refusing aux cleanup for unknown label: {label!r}")
            removed = 0
            for vid in sorted(self._orphan_aux_ids(label)):
                rows = self._cypher_strict(
                    f"MATCH (n:{label}) WHERE id(n) = $vid DELETE n RETURN 1",
                    {"vid": vid},
                    cols="x agtype",
                )
                removed += len(rows)
            out[label] = removed
        return out

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
        so simple concatenation is safe here (avoids f-string brace escaping).

        The edge carries an OPEN bitemporal interval (matching the
        ep-bitemporal-kg edge schema): ``valid_from`` + ``recorded_at`` are
        stamped once via ``coalesce`` so re-index keeps the original values,
        and ``valid_to`` is left NULL (the edge is currently valid). Unlike
        SUPERSEDES, which CLOSES an interval, these general edges never set
        ``valid_to``.
        """
        query = (
            "MATCH (m:Memory {id: $mem_id}) "
            "MERGE (n:" + label + " {" + key + ": $value}) "
            "MERGE (m)-[e:" + rel + "]->(n) " + _STAMP_NEW_EDGE + "RETURN n"
        )
        rows = self._cypher(
            query,
            {"mem_id": mem_id, "value": value, "now": _now_iso()},
            cols="n agtype",
        )
        return bool(rows)

    def _merge_edges_to_named(
        self, mem_id: str, label: str, key: str, values: list[str], rel: str
    ) -> None:
        """Batched :meth:`_merge_edge_to_named`: one round trip for N values.

        Same MERGE semantics and edge properties, driven by UNWIND. A memory
        with ten tags and ten entities was twenty sequential round trips; it
        is now two.
        """
        values = _unique(values)
        if not values:
            return
        query = (
            "MATCH (m:Memory {id: $mem_id}) "
            "UNWIND $values AS value "
            "MERGE (n:" + label + " {" + key + ": value}) "
            "MERGE (m)-[e:" + rel + "]->(n) " + _STAMP_NEW_EDGE + "RETURN n"
        )
        self._cypher(
            query,
            {"mem_id": mem_id, "values": values, "now": _now_iso()},
            cols="n agtype",
        )

    def _merge_related(self, a_id: str, b_id: str) -> bool:
        """MERGE a ``(a)-[:RELATED_TO]->(b)`` edge with an OPEN bitemporal
        interval: ``valid_from`` + ``recorded_at`` stamped once via
        ``coalesce`` (idempotent), ``valid_to`` left NULL (currently valid).
        """
        query = (
            "MATCH (a:Memory {id: $a_id}) "
            "MERGE (b:Memory {id: $b_id}) "
            "MERGE (a)-[e:RELATED_TO]->(b) "
            "WITH b, e WHERE e.valid_from IS NULL "
            "SET e.valid_from = $now, e.recorded_at = coalesce(e.recorded_at, $now) "
            "RETURN b"
        )
        rows = self._cypher(
            query,
            {"a_id": a_id, "b_id": b_id, "now": _now_iso()},
            cols="b agtype",
        )
        # True only when this call stamped a NEW edge; an existing one is
        # left untouched (see _STAMP_NEW_EDGE).
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

    def index_memory(self, memory: Memory, sync_hash: str | None = None) -> bool:
        """MERGE a memory node and all its relationships into the graph.

        Idempotent: safe to call repeatedly for the same memory — node and
        edge counts never grow on re-index (everything is MERGE-keyed).

        Edges created: ``FROM_SOURCE``, ``TAGGED_WITH`` (one per tag),
        ``RELATED_TO`` (from ``related_ids``), ``SUPERSEDES`` (from
        ``parent_id``), ``MENTIONS`` (from decomposition entities or a
        lightweight regex extraction).

        Args:
            memory: The memory to index.
            sync_hash: Fingerprint of the flat file this memory was read from
                (:func:`graph_sync_hash`), stored on the node so the next
                :meth:`sync_all` can skip it. Callers outside sync pass
                nothing, which clears it: the next sync re-indexes that
                memory once from its flat file, the source of truth.

        Returns:
            bool: True if the core Memory node was indexed successfully.
                False on any connection/AGE failure (never raises).
        """
        try:
            require_memory_id(memory.id)
        except ValueError as exc:
            logger.warning("AGEGraphBackend: refusing invalid memory ID: %s", exc)
            return False
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
            "sync_hash": sync_hash,
        }
        query = (
            "MERGE (m:Memory {id: $id}) "
            "SET m.title = $title, m.content = $content, m.summary = $summary, "
            "m.layer = $layer, m.role = $role, m.source = $source, "
            "m.source_ref = $source_ref, m.context_tag = $context_tag, "
            "m.created_at = $created_at, m.updated_at = $updated_at, "
            "m.intensity = $intensity, m.valence = $valence, "
            "m.tags = $tags, m.metadata_json = $metadata_json, "
            "m.sync_hash = $sync_hash "
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

            self._merge_edges_to_named(memory.id, "Tag", "name", list(memory.tags), "TAGGED_WITH")

            for related_id in _unique(memory.related_ids):
                if related_id != memory.id:
                    self._merge_related(memory.id, related_id)

            if memory.parent_id and memory.parent_id != memory.id:
                # Supersession CLOSES the parent's validity: the parent edge's
                # valid_to is set to this (superseding) memory's recorded instant.
                self._merge_supersedes(
                    memory.id, memory.parent_id, child_recorded_at=memory.created_at
                )

            self._merge_edges_to_named(
                memory.id, "Entity", "name", self._entities_for(memory), "MENTIONS"
            )
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

    def currently_valid_memories(self, as_of: str | None = None, limit: int = 200) -> list[dict]:
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

    def neighbors(self, memory_id: str, as_of: str | None = None) -> list[dict]:
        """Direct (1-hop) Memory-to-Memory neighbourhood valid as of ``as_of``.

        Point-in-time counterpart of :meth:`get_related` (card c915b47d):
        returns the memories directly connected to ``memory_id`` by an edge
        that was *valid at* ``as_of`` (default now). An edge is valid at time
        ``T`` iff its interval contains ``T``::

            (valid_from IS NULL OR valid_from <= T)
            AND (valid_to IS NULL OR valid_to > T)

        The lower-bound NULL guard mirrors :meth:`currently_valid_memories`'
        ``created_at`` guard, so pre-bitemporal edges (no ``valid_from``) are
        treated as always-started rather than silently dropped. Open general
        edges (``RELATED_TO``, ``valid_to`` NULL) are always current once their
        ``valid_from`` has passed; a ``SUPERSEDES`` edge (whose ``valid_to`` is
        the child's creation instant) drops out of the neighbourhood once
        ``as_of`` reaches that instant. So an ``as_of`` *before* a supersession
        still sees the then-valid link, and *after* it does not.

        Constrained to the two Memory-to-Memory edge types (``RELATED_TO``,
        ``SUPERSEDES``) exactly like :meth:`get_related`, deliberately excluding
        the ``TAGGED_WITH`` / ``FROM_SOURCE`` / ``MENTIONS`` hub edges (walking
        those explodes through shared-tag/source/entity hubs). AGE 1.7.x cannot
        do relationship-type alternation in a pattern, so each edge type is
        queried separately and merged in Python (mirroring ``get_related``).

        Args:
            memory_id: The memory whose neighbourhood to return.
            as_of: ISO-8601 UTC timestamp to evaluate edge validity at.
                ``None`` (default) means "now".

        Returns:
            list[dict]: Neighbour stubs (``id``, ``title``, ``layer``,
                ``distance`` == 1), sorted by title, capped at 50 (matching
                :meth:`get_related`'s return shape).
        """
        if self.graph is None:
            return []
        ts = as_of or _now_iso()
        merged: dict[str, dict] = {}
        for rel_type in ("RELATED_TO", "SUPERSEDES"):
            query = (
                "MATCH (start:Memory {id: $id})-[e:" + rel_type + "]-(n:Memory) "
                "WHERE n.id <> $id "
                "AND (e.valid_from IS NULL OR e.valid_from <= $ts) "
                "AND (e.valid_to IS NULL OR e.valid_to > $ts) "
                "RETURN n.id, n.title, n.layer"
            )
            rows = self._cypher(
                query,
                {"id": memory_id, "ts": ts},
                cols="id agtype, title agtype, layer agtype",
            )
            for row in self._rows_to_dicts(rows, ["id", "title", "layer"]):
                rid = row["id"]
                if rid and rid not in merged:
                    row["distance"] = 1
                    merged[rid] = row
        return sorted(merged.values(), key=lambda r: r["title"] or "")[:50]

    def supersedes_chain(self, memory_id: str, as_of: str | None = None) -> list[dict]:
        """Walk the ``SUPERSEDES`` lineage valid as of ``as_of`` (card c915b47d).

        Point-in-time counterpart of :meth:`get_lineage`: follows outgoing
        ``SUPERSEDES`` edges (toward older, superseded ancestors) but only over
        edges whose validity interval contains ``as_of`` (default now), using
        the same window predicate as :meth:`neighbors`::

            (valid_from IS NULL OR valid_from <= T)
            AND (valid_to IS NULL OR valid_to > T)

        A ``SUPERSEDES`` edge to a parent spans the parent's currency window
        ``[parent.created_at, child.created_at)`` (``valid_to`` == the child's
        creation instant, i.e. when the parent stopped being current). So a
        query *before* a supersession (``as_of`` inside that window) returns the
        then-current parent, and a query *after* it (``as_of`` >= ``valid_to``)
        excludes that now-closed link. With ``as_of=None`` the chain is the set
        of ancestors whose currency window still contains *now* (the
        currently-valid lineage).

        AGE 1.7.x cannot reliably filter every edge of a variable-length path
        (no reliable ``ALL(r IN relationships(path) ...)`` / ``EXISTS {}``), so
        the chain is walked one hop at a time with the window predicate applied
        per hop and results merged in Python (keeping the minimum depth per
        ancestor), rather than a single ``*1..N`` pattern.

        Note: :meth:`_superseded_ids_as_of` deliberately is NOT reused here. It
        selects the *opposite* window (edges already CLOSED as of ``ts``:
        ``valid_to <= ts``), whereas this chain needs edges still OPEN at ``ts``.

        Args:
            memory_id: Starting (most recent) memory ID.
            as_of: ISO-8601 UTC timestamp to evaluate edge validity at.
                ``None`` (default) means "now".

        Returns:
            list[dict]: Ancestor stubs (``id``, ``title``, ``layer``,
                ``depth`` == chain distance), nearest first (matching
                :meth:`get_lineage`'s return shape).
        """
        if self.graph is None:
            return []
        ts = as_of or _now_iso()
        chain: dict[str, dict] = {}
        visited: set[str] = {memory_id}
        frontier: list[tuple[str, int]] = [(memory_id, 0)]
        while frontier:
            current, dist = frontier.pop(0)
            if dist >= 10:
                continue
            query = (
                "MATCH (c:Memory {id: $id})-[e:SUPERSEDES]->(p:Memory) "
                "WHERE (e.valid_from IS NULL OR e.valid_from <= $ts) "
                "AND (e.valid_to IS NULL OR e.valid_to > $ts) "
                "RETURN p.id, p.title, p.layer"
            )
            rows = self._cypher(
                query,
                {"id": current, "ts": ts},
                cols="id agtype, title agtype, layer agtype",
            )
            for row in self._rows_to_dicts(rows, ["id", "title", "layer"]):
                pid = row["id"]
                if not pid or pid in visited:
                    continue
                visited.add(pid)
                row["depth"] = dist + 1
                chain[pid] = row
                frontier.append((pid, dist + 1))
        return sorted(chain.values(), key=lambda r: r["depth"])

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
            # Capped: this is the regex-over-every-Entity query that was 94%
            # of fused search latency. An abstaining arm beats a hung query.
            timeout_ms=_search_timeout_ms(),
        )
        return self._rows_to_dicts(rows, ["id", "title", "layer", "entity"])

    #: Query words too common to be worth an entity lookup. Deliberately tiny:
    #: the discriminative-token ordering below handles the rest, and a long
    #: hand-maintained stopword list is its own source of drift.
    _TOKEN_STOPWORDS = frozenset(
        {
            "what",
            "when",
            "where",
            "which",
            "about",
            "from",
            "with",
            "that",
            "this",
            "there",
            "their",
            "they",
            "then",
            "than",
            "have",
            "has",
            "had",
            "did",
            "does",
            "doing",
            "said",
            "says",
            "the",
            "and",
            "for",
            "was",
            "were",
            "been",
            "being",
            "into",
            "over",
            "under",
            "after",
            "before",
            "would",
            "could",
            "should",
            "will",
            "shall",
            "much",
            "many",
            "some",
            "any",
            "all",
            "only",
            "just",
            "also",
            "more",
            "most",
            "like",
            "want",
            "need",
            "know",
            "think",
            "make",
            "made",
        }
    )

    def search_by_entity_tokens(self, query: str, limit: int = 20) -> list[dict]:
        """Entity lookup on query TOKENS rather than the whole query string.

        ``search_by_entity`` substring-matches the ENTIRE query against entity
        names, which means it only ever fires when the query is itself close to
        a bare entity name. Measured against lumina's real graph on ten
        realistic multi-word agent queries ("skgateway routing decision", "what
        did Casey say about the invoice", "beelama kv degeneracy fix", and so
        on), the whole-query form fired on **0 of 10**. The same ten queries
        matched entities on **10 of 10** once each query word was looked up
        separately. An arm attached but never firing is an unmeasured feature,
        not a working one.

        Tokens are tried MOST DISCRIMINATIVE FIRST, by how few memories each
        one reaches. That is the same principle the rare-term reranker uses: a
        token matching three memories says far more about intent than one
        matching three hundred, and ordering by it means the per-arm cap keeps
        the informative hits rather than whatever the query happened to say
        first.

        Returns the same row shape as :meth:`search_by_entity`, so either can
        back the fusion graph arm.
        """
        if self.graph is None or not query or not query.strip():
            return []

        # MOST SPECIFIC FIRST. The whole query is tried before any token,
        # because for a query that IS close to an entity name the whole-query
        # match is strictly better: tokenizing splits multi-word entity names
        # ("Ali Al-Zaidi", "Zero One") and loses them. Measured on 40 bare
        # entity queries, whole-query scored precision@5 0.400 against 0.350
        # for token-only, while token-only was the ONLY variant that fired at
        # all on realistic multi-word queries (10 of 10 versus 0 of 10).
        # Neither alone is right, so both run and the more specific one leads.
        #
        # This is a decision about the QUERY SHAPE, taken before anything is
        # retrieved, not a fallback triggered by disliking the rows that came
        # back. That distinction is the whole point of R1.
        # ONE budget for the whole fan-out, not per sub-query. Each
        # search_by_entity below is individually capped, but this method issues
        # one per token, so N tokens could still total N times the cap. The
        # whole-query lookup always runs (it is the most specific and the one
        # worth paying for); token lookups stop once the budget is spent.
        budget_s = max(_search_timeout_ms(), 0) / 1000.0
        started = time.monotonic()

        def budget_left() -> bool:
            return budget_s <= 0 or (time.monotonic() - started) < budget_s

        out: list[dict] = []
        seen: set[str] = set()
        for row in self.search_by_entity(query, limit=limit) or []:
            row_id = str(row.get("id") or "")
            if row_id and row_id not in seen:
                seen.add(row_id)
                out.append(row)
        if len(out) >= limit:
            return out[:limit]

        tokens = [
            tok
            for tok in dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_.-]{3,}", query))
            if tok.casefold() not in self._TOKEN_STOPWORDS
        ]
        # A single token identical to the query adds nothing the block above
        # did not already collect.
        tokens = [t for t in tokens if t.casefold() != query.strip().casefold()]
        if not tokens:
            return out

        # Collect per token, then order tokens by how few rows they reached.
        per_token: list[tuple[int, str, list[dict]]] = []
        for tok in tokens:
            if not budget_left():
                logger.debug(
                    "AGEGraphBackend: entity-token budget spent after %.2fs, "
                    "skipping %d remaining token(s)",
                    time.monotonic() - started,
                    len(tokens) - tokens.index(tok),
                )
                break
            rows = self.search_by_entity(tok, limit=limit) or []
            if rows:
                per_token.append((len(rows), tok, rows))
        per_token.sort(key=lambda t: (t[0], t[1]))

        # Interleave by rank within each token so one broad token cannot fill
        # the whole budget before a narrow one is heard from.
        depth = 0
        while len(out) < limit and per_token:
            progressed = False
            for _count, _tok, rows in per_token:
                if depth >= len(rows):
                    continue
                progressed = True
                row = rows[depth]
                row_id = str(row.get("id") or "")
                if row_id and row_id not in seen:
                    seen.add(row_id)
                    out.append(row)
                    if len(out) >= limit:
                        break
            if not progressed:
                break
            depth += 1
        return out

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

    def ensure_graph(self) -> bool:
        """Create this agent's AGE graph if it is not registered yet.

        Nothing in skmemory provisioned a per-agent graph. The graph role was
        wired, queried and health-checked, but whoever added an agent had to
        know to run `SELECT ag_catalog.create_graph('<agent>_knowledge')` by
        hand. Three agents on one node had no graph at all as a result, and
        until the health fix landed they reported a healthy EMPTY graph rather
        than a missing one, so nobody found out.

        Two things make this easy to get wrong by hand, both of them real:

        * `create_graph` needs `ag_catalog` on the search_path, or it fails
          with `operator class "graphid_ops" does not exist for access method
          "btree"`, which says nothing about search_path.
        * It is not idempotent, so a second call errors rather than no-ops.

        Both are handled here: the catalog is checked first and the search_path
        is set for the duration of this transaction only.

        Returns:
            bool: True if a graph was created, False if it already existed.

        Raises:
            GraphTransportError: Graph disabled or connection unavailable.
        """
        if self.graph is None:
            raise GraphTransportError("AGE graph disabled: invalid graph name")
        conn = self._conn()
        if conn is None:
            raise GraphTransportError(f"AGE connection unavailable ({self._safe_dsn()})")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ag_catalog.ag_graph WHERE name::text = %s", (self.graph,)
                )
                if cur.fetchone() is not None:
                    return False
                # ag_catalog must be on the path for create_graph's internal
                # index creation to resolve graphid_ops. SET LOCAL keeps it
                # scoped to this transaction.
                cur.execute('SET LOCAL search_path = ag_catalog, "$user", public')
                cur.execute("SELECT ag_catalog.create_graph(%s)", (self.graph,))
            conn.commit()
            logger.info("AGEGraphBackend: created graph %s", self.graph)
            return True
        except Exception as exc:  # noqa: BLE001
            self._rollback(conn)
            raise GraphTransportError(f"could not create AGE graph {self.graph!r}: {exc}") from exc

    def _rollback(self, conn) -> None:
        """Best-effort rollback so one failed probe cannot poison the session."""
        try:
            if conn is not None and not conn.closed:
                conn.rollback()
        except Exception:  # noqa: BLE001
            pass

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
        # A graph that does NOT EXIST must not look like an empty one.
        #
        # This used to call the lenient `_cypher`, which swallows every failure
        # and returns [], so `node_count` fell to 0 and the result still said
        # ok: True. An agent whose <agent>_knowledge graph had never been
        # created therefore reported a perfectly healthy, empty graph.
        #
        # Found 2026-10-20: `architect_knowledge` was absent from
        # ag_catalog.ag_graph entirely while health returned
        # {"ok": true, "node_count": 0}. Same shape as a missing tier
        # directory reading as an empty tier, and the catalog is the authority
        # here, so ask it rather than inferring from a count.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ag_catalog.ag_graph WHERE name::text = %s", (self.graph,)
                )
                registered = cur.fetchone() is not None
        except Exception as exc:  # noqa: BLE001
            self._rollback(conn)
            return {
                "ok": False,
                "backend": "AGEGraphBackend",
                "dsn": self._safe_dsn(),
                "graph": self.graph,
                "error": f"could not read ag_catalog.ag_graph: {exc}",
            }
        if not registered:
            return {
                "ok": False,
                "backend": "AGEGraphBackend",
                "dsn": self._safe_dsn(),
                "graph": self.graph,
                "node_count": None,
                "error": (
                    f"graph {self.graph!r} is not registered in ag_catalog.ag_graph "
                    "(never created). Provision it with "
                    "SELECT ag_catalog.create_graph(...) after setting "
                    "search_path to include ag_catalog."
                ),
            }
        try:
            # STRICT: a count that could not run is not a count of zero.
            rows = self._cypher_strict("MATCH (n) RETURN count(n)", cols="c agtype")
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

    def sync_all(
        self,
        flat_files_dir,
        agent_name: str,
        *,
        budget_s: float | None = None,
        full: bool | None = None,
    ) -> dict:
        """Backfill the graph from flat-file memories on disk, incrementally.

        Walks ``<flat_files_dir>/{short,mid,long}-term/*.json``. A memory is
        re-indexed only when the graph does not already hold it at the exact
        bytes on disk (``Memory.sync_hash``, see :func:`graph_sync_hash`), so
        a steady-state pass reads files and one graph scan and writes
        nothing. Memories the graph has never seen go first, so a pass that
        runs out of budget still lands the newest work.

        Bounded by ``budget_s`` (default ``SKMEMORY_GRAPH_SYNC_BUDGET_S``,
        900s; 0 = unbounded). It stops between memories and reports the rest
        as ``deferred``. Every index commits on its own, so a stopped or
        killed pass loses nothing and the next one resumes.

        Args:
            flat_files_dir: Path to the agent's memory directory.
            agent_name: Used only for log lines.
            budget_s: Wall-clock budget override, seconds.
            full: Ignore stored hashes and re-index everything (still
                budgeted). Defaults to ``SKMEMORY_GRAPH_SYNC_FULL=1``.

        Returns:
            dict: ``{"indexed", "errors", "skipped", "deferred"}``.
        """
        from pathlib import Path

        stats = {"indexed": 0, "errors": 0, "skipped": 0, "deferred": 0}
        if self.graph is None:
            logger.warning("AGEGraphBackend sync_all: invalid graph name for %s", agent_name)
            return stats

        budget = _graph_sync_budget_s() if budget_s is None else float(budget_s)
        deadline = time.monotonic() + budget if budget > 0 else None
        if full is None:
            full = (os.environ.get("SKMEMORY_GRAPH_SYNC_FULL") or "").strip() == "1"

        base = Path(flat_files_dir)
        # Snapshot by file name before indexing. Syncthing and promotion can
        # move or remove a flat file between directory enumeration and read.
        # A vanished file is no longer source truth, so it is not a projection
        # error. If it moved between tiers, resolve its current path and index
        # the current record once.
        tiers = ("short-term", "mid-term", "long-term")
        snapshot: dict[str, Path] = {}
        for tier in tiers:
            tier_dir = base / tier
            if not tier_dir.exists():
                continue
            for json_file in tier_dir.glob("*.json"):
                if not json_file.stem:
                    quarantine_invalid_flat_file(
                        base,
                        json_file,
                        reason="empty filename memory ID",
                    )
                    continue
                snapshot[json_file.name] = json_file
        if not snapshot:
            return stats

        # Indexes before the first lookup, or the first backfill is the slow one.
        try:
            self.ensure_property_indexes()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGEGraphBackend sync_all: property indexes unavailable: %s", exc)
        state = None if full else self.graph_sync_state()
        known = state or {}

        def read_current(name: str, original_path: Path):
            candidates = [original_path]
            candidates.extend(base / tier / name for tier in tiers)
            for candidate in dict.fromkeys(candidates):
                try:
                    return candidate.read_bytes(), candidates
                except FileNotFoundError:
                    continue
            return None, candidates

        def load_memory(name: str, payload: bytes, candidates: list[Path]) -> Memory | None:
            data = json.loads(payload)
            try:
                payload_memory_id(data, Path(name).stem)
            except ValueError as exc:
                current = next(
                    (c for c in dict.fromkeys(candidates) if c.exists()),
                    None,
                )
                if current is not None:
                    quarantine_invalid_flat_file(base, current, reason=str(exc))
                return None
            return Memory.model_validate(data)

        # Pass 1: cheap. Hash bytes, skip what the graph already holds, and
        # order the rest new-first. Only (priority, name, path, hash) is kept,
        # not parsed memories, so a full backlog does not sit in RAM.
        pending: list[tuple[int, str, Path, str]] = []
        for name, original_path in snapshot.items():
            payload, _ = read_current(name, original_path)
            if payload is None:
                continue
            digest = graph_sync_hash(payload)
            stem = Path(name).stem
            if stem in known:
                if known[stem] == digest:
                    stats["skipped"] += 1
                    continue
                pending.append((1, name, original_path, digest))
            else:
                # Not in the graph by file stem. A shortform stem maps to a
                # full id inside the payload; resolved in pass 2.
                pending.append((0, name, original_path, digest))
        pending.sort(key=lambda item: item[0])

        # Pass 2: index, newest-unknown first, until the budget runs out.
        for position, (_prio, name, original_path, digest) in enumerate(pending):
            if deadline is not None and time.monotonic() >= deadline:
                stats["deferred"] = len(pending) - position
                break
            payload, candidates = read_current(name, original_path)
            if payload is None:
                continue
            try:
                digest = graph_sync_hash(payload)
                memory = load_memory(name, payload, candidates)
                if memory is None:
                    continue
                if known.get(memory.id) == digest:
                    stats["skipped"] += 1
                    continue
                if self.index_memory(memory, sync_hash=digest):
                    stats["indexed"] += 1
                else:
                    stats["errors"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("AGEGraphBackend sync_all: failed on %s: %s", name, exc)
                stats["errors"] += 1

        if stats["deferred"]:
            logger.warning(
                "AGEGraphBackend sync_all for '%s': budget of %.0fs spent, "
                "%d memory(ies) deferred to the next pass",
                agent_name,
                budget,
                stats["deferred"],
            )
        logger.info(
            "AGEGraphBackend sync_all for '%s': indexed=%d skipped=%d deferred=%d errors=%d",
            agent_name,
            stats["indexed"],
            stats["skipped"],
            stats["deferred"],
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
