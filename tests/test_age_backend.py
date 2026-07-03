"""
Tests for AGEGraphBackend (Apache AGE graph relationship backend).

Two tiers:

* Pure unit tests of ``_agtype()`` parsing — no DB required, always run.
* End-to-end tests against a **throwaway** graph (``skmemory_test_<pid>``)
  created in a session fixture and dropped in teardown. These run against
  the live skmem-pg Postgres and are automatically skipped (module-level
  ``skipif``) when it's unreachable, so CI without the DB still passes.

SAFETY: every e2e test operates exclusively on the throwaway graph handed
to it via the ``backend`` fixture. Nothing here ever names, queries, or
touches ``lumina_knowledge`` / ``opus_knowledge`` / ``personal_history``.
"""

from __future__ import annotations

import json
import os
import random
import string
import uuid

import pytest

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

DSN = os.environ.get("SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory")
TEST_GRAPH_NAME = f"skmemory_test_{os.getpid()}"


def _skmem_pg_available() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
        cur.execute("SELECT 1;")
        conn.close()
        return True
    except Exception:
        return False


SKMEM_PG_AVAILABLE = _skmem_pg_available()

requires_skmem_pg = pytest.mark.skipif(
    not SKMEM_PG_AVAILABLE,
    reason="skmem-pg (SKMEMORY_PG_DSN) unreachable or AGE extension unavailable",
)


def _unique_entity_word(prefix: str) -> str:
    """A unique Titlecase word matching the entity-extraction regex
    (``[A-Z][a-z]{2,}`` with a trailing word boundary): the whole word must
    be one capital letter followed by an unbroken run of lowercase letters,
    so ``prefix`` must not itself contain digits or a second capital, or the
    internal boundary would split the match and extraction would silently
    find nothing."""
    assert prefix[:1].isupper() and prefix[1:].islower(), (
        f"_unique_entity_word prefix {prefix!r} must be Titlecase with no "
        "internal capitals/digits (see docstring)"
    )
    suffix = "".join(random.choices(string.ascii_lowercase, k=8))
    return f"{prefix}{suffix}"


def make_memory(
    title: str = "Test Memory",
    content: str = "End-to-end AGE backend test content, long enough for entity extraction to run.",
    tags: list[str] | None = None,
    source: str = "age-test",
    source_ref: str = "",
    layer: MemoryLayer = MemoryLayer.SHORT,
    intensity: float = 5.0,
    valence: float = 0.5,
    parent_id: str | None = None,
    related_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> Memory:
    """Factory for Memory objects, unique id per call."""
    return Memory(
        id=str(uuid.uuid4()),
        title=title,
        content=content,
        tags=tags or [],
        source=source,
        source_ref=source_ref,
        layer=layer,
        emotional=EmotionalSnapshot(intensity=intensity, valence=valence),
        parent_id=parent_id,
        related_ids=related_ids or [],
        metadata=metadata or {},
    )


# ─────────────────────────────────────────────────────────
# Session-scoped throwaway graph
# ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def throwaway_graph():
    """Create skmemory_test_<pid> for the whole session, drop it after."""
    if not SKMEM_PG_AVAILABLE:
        pytest.skip("skmem-pg unreachable")

    import psycopg

    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
    cur.execute("SELECT create_graph(%s);", (TEST_GRAPH_NAME,))
    try:
        yield TEST_GRAPH_NAME
    finally:
        cur.execute("SELECT drop_graph(%s, true);", (TEST_GRAPH_NAME,))
        conn.close()


@pytest.fixture
def backend(throwaway_graph):
    """A fresh AGEGraphBackend pointed at the throwaway graph.

    Function-scoped so each test gets its own connection, but they all
    share the same underlying throwaway graph/data — tests use unique
    memory ids (via make_memory()) to avoid interfering with each other.
    """
    be = AGEGraphBackend(dsn=DSN, graph=throwaway_graph)
    yield be


# ═══════════════════════════════════════════════════════════
# Pure unit tests — _agtype() parsing (no DB)
# ═══════════════════════════════════════════════════════════


class TestAgtypeParsing:
    def setup_method(self):
        # No DB connection needed — construct with an obviously-unreachable
        # dsn so no lazy connection is ever attempted by these pure tests.
        self.be = AGEGraphBackend(dsn="postgresql://x:x@localhost:1/x", graph="unit_test_graph")

    def test_parses_vertex(self):
        raw = '{"id": 844424930131969, "label": "Memory", "properties": {"id": "abc", "title": "hi"}}::vertex'
        result = self.be._agtype(raw)
        assert result["label"] == "Memory"
        assert result["properties"] == {"id": "abc", "title": "hi"}

    def test_parses_edge(self):
        raw = '{"id": 1125899906842625, "label": "RELATED_TO", "start_id": 1, "end_id": 2, "properties": {}}::edge'
        result = self.be._agtype(raw)
        assert result["label"] == "RELATED_TO"
        assert result["properties"] == {}

    def test_parses_path_suffix(self):
        raw = '[1, 2, 3]::path'
        result = self.be._agtype(raw)
        assert result == [1, 2, 3]

    def test_parses_quoted_string_scalar(self):
        assert self.be._agtype('"infra"') == "infra"

    def test_parses_integer_scalar(self):
        assert self.be._agtype("5") == 5

    def test_parses_float_scalar(self):
        assert self.be._agtype("3.5") == 3.5

    def test_parses_bool_and_null(self):
        assert self.be._agtype("true") is True
        assert self.be._agtype("null") is None

    def test_parses_none_input(self):
        assert self.be._agtype(None) is None

    def test_passes_through_non_string(self):
        assert self.be._agtype(42) == 42
        assert self.be._agtype([1, 2]) == [1, 2]

    def test_malformed_returns_none(self):
        assert self.be._agtype("{not valid json") is None
        assert self.be._agtype("") is None
        assert self.be._agtype("   ") is None

    def test_props_extracts_from_vertex_dict(self):
        vertex = {"id": 1, "label": "Memory", "properties": {"id": "abc", "title": "hi"}}
        assert AGEGraphBackend._props(vertex) == {"id": "abc", "title": "hi"}

    def test_props_returns_empty_for_non_dict(self):
        assert AGEGraphBackend._props(None) == {}
        assert AGEGraphBackend._props("nope") == {}
        assert AGEGraphBackend._props(5) == {}

    def test_memory_dict_parses_metadata_json(self):
        vertex = {
            "properties": {
                "id": "abc",
                "title": "hi",
                "metadata_json": json.dumps({"decomposition": {"entities": ["Foo"]}}),
            }
        }
        result = AGEGraphBackend._memory_dict(vertex)
        assert result["id"] == "abc"
        assert result["metadata"] == {"decomposition": {"entities": ["Foo"]}}
        assert "metadata_json" not in result

    def test_memory_dict_none_for_empty_props(self):
        assert AGEGraphBackend._memory_dict({"properties": {}}) is None
        assert AGEGraphBackend._memory_dict(None) is None


class TestGraphNameValidation:
    def test_rejects_unsafe_graph_name(self):
        be = AGEGraphBackend(dsn=DSN, graph="bad; DROP TABLE foo; --")
        assert be.graph is None

    def test_accepts_safe_graph_name(self):
        be = AGEGraphBackend(dsn=DSN, graph="lumina_knowledge_v2")
        assert be.graph == "lumina_knowledge_v2"

    def test_default_agent_and_graph_derivation(self, monkeypatch):
        monkeypatch.delenv("SKAGENT", raising=False)
        monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        be = AGEGraphBackend(dsn=DSN)
        assert be.agent == "lumina"
        assert be.graph == "lumina_knowledge"

    def test_skagent_env_takes_priority(self, monkeypatch):
        monkeypatch.setenv("SKAGENT", "opus")
        monkeypatch.setenv("SKMEMORY_AGENT", "should-not-be-used")
        be = AGEGraphBackend(dsn=DSN)
        assert be.agent == "opus"
        assert be.graph == "opus_knowledge"

    def test_safe_dsn_redacts_password(self):
        be = AGEGraphBackend(dsn="postgresql://postgres:supersecret@localhost:5432/skmemory", graph="x")
        redacted = be._safe_dsn()
        assert "supersecret" not in redacted
        assert "postgres:***@" in redacted


# ═══════════════════════════════════════════════════════════
# E2E tests against the throwaway graph
# ═══════════════════════════════════════════════════════════


@requires_skmem_pg
class TestIndexAndGet:
    def test_index_memory_returns_true(self, backend):
        mem = make_memory(title="Index Test")
        assert backend.index_memory(mem) is True

    def test_index_then_get_round_trips_fields(self, backend):
        mem = make_memory(
            title="Round Trip",
            content="Field round trip content for AGE backend, long enough to trip entity extraction.",
            tags=["infra", "db"],
            metadata={"note": "hello", "nested": {"a": 1}},
        )
        assert backend.index_memory(mem) is True

        node = backend.get(mem.id)
        assert node is not None
        assert node["id"] == mem.id
        assert node["title"] == "Round Trip"
        assert node["content"] == mem.content
        assert node["layer"] == "short-term"
        assert node["metadata"] == {"note": "hello", "nested": {"a": 1}}
        assert set(node["tags"]) == {"infra", "db"}

    def test_save_returns_memory_id(self, backend):
        mem = make_memory(title="Save Returns Id")
        assert backend.save(mem) == mem.id

    def test_save_updates_existing_node(self, backend):
        mem = make_memory(title="Original Title")
        backend.save(mem)
        mem.title = "Updated Title"
        backend.save(mem)
        node = backend.get(mem.id)
        assert node["title"] == "Updated Title"

    def test_get_nonexistent_returns_none(self, backend):
        assert backend.get("does-not-exist-" + str(uuid.uuid4())) is None


@requires_skmem_pg
class TestEdges:
    def test_tagged_with_edge_created_and_searchable(self, backend):
        tag = f"uniquetag-{uuid.uuid4().hex[:8]}"
        mem = make_memory(title="Tag Edge Test", tags=[tag])
        backend.index_memory(mem)

        results = backend.search_by_tags([tag])
        ids = {r["id"] for r in results}
        assert mem.id in ids

    def test_from_source_edge_created(self, backend):
        source = f"src-{uuid.uuid4().hex[:8]}"
        mem = make_memory(title="Source Edge Test", source=source)
        backend.index_memory(mem)
        # No direct read API for FROM_SOURCE; verify via stats edge type presence.
        stats = backend.stats()
        assert stats["edges_by_type"].get("FROM_SOURCE", 0) >= 1

    def test_related_to_edge_traversable(self, backend):
        a = make_memory(title="Related A")
        backend.index_memory(a)
        b = make_memory(title="Related B", related_ids=[a.id])
        backend.index_memory(b)

        related = backend.get_related(b.id, depth=1)
        ids = {r["id"] for r in related}
        assert a.id in ids

    def test_mentions_edge_created_for_long_content(self, backend):
        entity_word = _unique_entity_word("Zorbaxia")
        mem = make_memory(
            title="Entity Test",
            content=(
                f"This is a long enough passage discussing {entity_word} in detail, "
                "well past the fifty character threshold for extraction."
            ),
        )
        backend.index_memory(mem)

        results = backend.search_by_entity(entity_word)
        ids = {r["id"] for r in results}
        assert mem.id in ids

    def test_mentions_edge_from_decomposition_metadata(self, backend):
        entity_word = f"DecompEntity{uuid.uuid4().hex[:6]}"
        mem = make_memory(
            title="Decomposed Entity Test",
            content="short",
            metadata={"decomposition": {"entities": [entity_word]}},
        )
        backend.index_memory(mem)
        results = backend.search_by_entity(entity_word)
        ids = {r["id"] for r in results}
        assert mem.id in ids


@requires_skmem_pg
class TestIdempotency:
    """The single most important correctness property: re-indexing never
    duplicates nodes or edges."""

    def test_reindex_same_memory_node_count_unchanged(self, backend):
        tag = f"idem-{uuid.uuid4().hex[:8]}"
        mem = make_memory(title="Idempotency Test", tags=[tag, "shared"])

        before = backend.stats()
        backend.index_memory(mem)
        after_first = backend.stats()
        backend.index_memory(mem)
        after_second = backend.stats()

        # First index adds nodes/edges relative to baseline.
        assert after_first["node_count"] > before["node_count"]
        # Second (identical) index must NOT add any more nodes or edges.
        assert after_second["node_count"] == after_first["node_count"]
        assert after_second["edge_count"] == after_first["edge_count"]

    def test_reindex_with_relationships_idempotent(self, backend):
        a = make_memory(title="Idem Parent")
        backend.index_memory(a)

        tag = f"idem2-{uuid.uuid4().hex[:8]}"
        entity_word = _unique_entity_word("Idement")
        b = make_memory(
            title="Idem Child",
            content=(
                f"Long content mentioning {entity_word} for entity extraction, "
                "well past fifty characters so MENTIONS gets created here too."
            ),
            tags=[tag],
            parent_id=a.id,
            related_ids=[a.id],
        )

        backend.index_memory(b)
        stats_1 = backend.stats()
        backend.index_memory(b)
        stats_2 = backend.stats()
        backend.index_memory(b)
        stats_3 = backend.stats()

        assert stats_1["node_count"] == stats_2["node_count"] == stats_3["node_count"]
        assert stats_1["edge_count"] == stats_2["edge_count"] == stats_3["edge_count"]

        # Sanity: the edges actually exist (not all silently failing to zero).
        related = backend.get_related(b.id, depth=1)
        assert a.id in {r["id"] for r in related}
        lineage = backend.get_lineage(b.id)
        assert a.id in {r["id"] for r in lineage}
        tags = backend.search_by_tags([tag])
        assert b.id in {r["id"] for r in tags}
        entities = backend.search_by_entity(entity_word)
        assert b.id in {r["id"] for r in entities}


@requires_skmem_pg
class TestTraversal:
    def test_get_related_depth_1_direct_only(self, backend):
        a = make_memory(title="Depth Root")
        backend.index_memory(a)
        b = make_memory(title="Depth Hop1", related_ids=[a.id])
        backend.index_memory(b)
        c = make_memory(title="Depth Hop2", related_ids=[b.id])
        backend.index_memory(c)

        depth1 = backend.get_related(a.id, depth=1)
        ids_1 = {r["id"] for r in depth1}
        assert b.id in ids_1
        assert c.id not in ids_1

    def test_get_related_depth_2_reaches_two_hops(self, backend):
        a = make_memory(title="Depth2 Root")
        backend.index_memory(a)
        b = make_memory(title="Depth2 Hop1", related_ids=[a.id])
        backend.index_memory(b)
        c = make_memory(title="Depth2 Hop2", related_ids=[b.id])
        backend.index_memory(c)

        depth2 = backend.get_related(a.id, depth=2)
        ids_2 = {r["id"] for r in depth2}
        assert b.id in ids_2
        assert c.id in ids_2

    def test_traverse_is_alias_for_get_related(self, backend):
        a = make_memory(title="Traverse Root")
        backend.index_memory(a)
        b = make_memory(title="Traverse Hop", related_ids=[a.id])
        backend.index_memory(b)

        assert backend.traverse(a.id, depth=1) == backend.get_related(a.id, depth=1)

    def test_get_related_unknown_id_returns_empty(self, backend):
        assert backend.get_related("nope-" + str(uuid.uuid4())) == []


@requires_skmem_pg
class TestLineage:
    def test_get_lineage_over_supersedes_chain(self, backend):
        gen1 = make_memory(title="Gen1")
        backend.index_memory(gen1)
        gen2 = make_memory(title="Gen2", parent_id=gen1.id)
        backend.index_memory(gen2)
        gen3 = make_memory(title="Gen3", parent_id=gen2.id)
        backend.index_memory(gen3)

        lineage = backend.get_lineage(gen3.id)
        ordered_ids = [entry["id"] for entry in lineage]
        assert ordered_ids == [gen2.id, gen1.id]
        assert lineage[0]["depth"] == 1
        assert lineage[1]["depth"] == 2

    def test_get_lineage_no_parent_returns_empty(self, backend):
        mem = make_memory(title="No Parent")
        backend.index_memory(mem)
        assert backend.get_lineage(mem.id) == []

    def test_get_lineage_unknown_id_returns_empty(self, backend):
        assert backend.get_lineage("nope-" + str(uuid.uuid4())) == []


@requires_skmem_pg
class TestSearch:
    def test_search_by_tags_or_logic(self, backend):
        tag_a = f"tagA-{uuid.uuid4().hex[:8]}"
        tag_b = f"tagB-{uuid.uuid4().hex[:8]}"
        m1 = make_memory(title="Tag Search 1", tags=[tag_a])
        m2 = make_memory(title="Tag Search 2", tags=[tag_b])
        m3 = make_memory(title="Tag Search 3", tags=["irrelevant"])
        for m in (m1, m2, m3):
            backend.index_memory(m)

        results = backend.search_by_tags([tag_a, tag_b])
        ids = {r["id"] for r in results}
        assert m1.id in ids
        assert m2.id in ids
        assert m3.id not in ids

    def test_search_by_tags_empty_list_returns_empty(self, backend):
        assert backend.search_by_tags([]) == []

    def test_search_by_entity_case_insensitive_substring(self, backend):
        entity_word = _unique_entity_word("Casesensitiveword")
        mem = make_memory(
            title="Case Test",
            content=f"A sufficiently long passage about {entity_word} for extraction to trigger.",
        )
        backend.index_memory(mem)

        results = backend.search_by_entity(entity_word.lower())
        ids = {r["id"] for r in results}
        assert mem.id in ids

    def test_search_by_entity_blank_query_returns_empty(self, backend):
        assert backend.search_by_entity("") == []
        assert backend.search_by_entity("   ") == []


@requires_skmem_pg
class TestClusters:
    def test_find_clusters_returns_well_connected_hub(self, backend):
        shared_tag = f"hub-{uuid.uuid4().hex[:8]}"
        hub = make_memory(title="Cluster Hub", tags=[shared_tag, "a", "b"], source=f"src-{uuid.uuid4().hex[:6]}")
        backend.index_memory(hub)

        results = backend.find_clusters(min_size=3)
        ids = {r["id"] for r in results}
        assert hub.id in ids

    def test_find_clusters_respects_min_size(self, backend):
        lonely = make_memory(title="Lonely Node", tags=[])
        backend.index_memory(lonely)

        results = backend.find_clusters(min_size=1000)
        ids = {r["id"] for r in results}
        assert lonely.id not in ids


@requires_skmem_pg
class TestContextGraph:
    def test_get_context_graph_shape(self, backend):
        tag = f"ctx-{uuid.uuid4().hex[:8]}"
        a = make_memory(title="Context Root", tags=[tag])
        backend.index_memory(a)
        b = make_memory(title="Context Related", related_ids=[a.id])
        backend.index_memory(b)

        ctx = backend.get_context_graph(a.id, depth=1)
        assert ctx["center_id"] == a.id
        assert tag in ctx["tags"]
        assert any(r["id"] == b.id for r in ctx["related"])
        assert isinstance(ctx["entities"], list)

    def test_get_context_graph_unknown_id_returns_empty_dict(self, backend):
        assert backend.get_context_graph("nope-" + str(uuid.uuid4())) == {}


@requires_skmem_pg
class TestDelete:
    def test_remove_memory_deletes_node(self, backend):
        mem = make_memory(title="To Delete")
        backend.index_memory(mem)
        assert backend.get(mem.id) is not None

        assert backend.remove_memory(mem.id) is True
        assert backend.get(mem.id) is None

    def test_remove_memory_removes_edges_too(self, backend):
        a = make_memory(title="Delete Edge Root")
        backend.index_memory(a)
        b = make_memory(title="Delete Edge Leaf", related_ids=[a.id])
        backend.index_memory(b)

        backend.remove_memory(b.id)
        related = backend.get_related(a.id, depth=1)
        assert b.id not in {r["id"] for r in related}

    def test_delete_is_alias_for_remove_memory(self, backend):
        mem = make_memory(title="Delete Alias")
        backend.index_memory(mem)
        assert backend.delete(mem.id) is True
        assert backend.get(mem.id) is None

    def test_remove_nonexistent_returns_true_not_error(self, backend):
        # No matching node is not a failure — the query still ran successfully.
        assert backend.remove_memory("nope-" + str(uuid.uuid4())) is True


@requires_skmem_pg
class TestStatsAndHealth:
    def test_stats_shape(self, backend):
        mem = make_memory(title="Stats Shape Test")
        backend.index_memory(mem)

        stats = backend.stats()
        assert stats["ok"] is True
        assert stats["backend"] == "AGEGraphBackend"
        assert stats["graph"] == TEST_GRAPH_NAME
        assert isinstance(stats["node_count"], int)
        assert isinstance(stats["edge_count"], int)
        assert isinstance(stats["nodes_by_label"], dict)
        assert isinstance(stats["edges_by_type"], dict)
        assert stats["nodes_by_label"].get("Memory", 0) >= 1

    def test_health_check_shape(self, backend):
        result = backend.health_check()
        assert result["ok"] is True
        assert result["backend"] == "AGEGraphBackend"
        assert result["graph"] == TEST_GRAPH_NAME
        assert "node_count" in result
        assert "skmemory" in result["dsn"]  # dsn present but password redacted
        assert "@" in result["dsn"]


@requires_skmem_pg
class TestSyncAll:
    def test_sync_all_indexes_flat_files(self, backend, tmp_path):
        short_dir = tmp_path / "short-term"
        short_dir.mkdir()
        mem = make_memory(title="Sync All Test")
        (short_dir / f"{mem.id}.json").write_text(json.dumps(mem.model_dump(mode="json")))

        result = backend.sync_all(tmp_path, "test-agent")
        assert result == {"indexed": 1, "errors": 0}
        assert backend.get(mem.id) is not None

    def test_sync_all_counts_errors_for_bad_files(self, backend, tmp_path):
        mid_dir = tmp_path / "mid-term"
        mid_dir.mkdir()
        (mid_dir / "broken.json").write_text("{not valid json")

        result = backend.sync_all(tmp_path, "test-agent")
        assert result["errors"] == 1
        assert result["indexed"] == 0

    def test_sync_all_missing_dir_is_safe(self, backend, tmp_path):
        result = backend.sync_all(tmp_path / "does-not-exist", "test-agent")
        assert result == {"indexed": 0, "errors": 0}


# ═══════════════════════════════════════════════════════════
# Error paths — never raise, always degrade to safe empty
# ═══════════════════════════════════════════════════════════


class TestErrorPaths:
    """Bad-connection tests don't need the throwaway graph fixture — they
    exercise the failure path itself, so they run whenever psycopg is
    importable (no live DB required, in fact these specifically target an
    UNREACHABLE dsn)."""

    @pytest.fixture
    def bad_backend(self):
        return AGEGraphBackend(
            dsn="postgresql://baduser:badpass@localhost:1/nonexistent",
            graph="whatever_knowledge",
        )

    def test_get_on_bad_dsn_returns_none(self, bad_backend):
        assert bad_backend.get("x") is None

    def test_index_memory_on_bad_dsn_returns_false(self, bad_backend):
        mem = make_memory(title="Bad DSN")
        assert bad_backend.index_memory(mem) is False

    def test_save_on_bad_dsn_still_returns_id(self, bad_backend):
        # save() mirrors SKGraphBackend: always returns the id regardless of
        # whether the underlying index succeeded.
        mem = make_memory(title="Bad DSN Save")
        assert bad_backend.save(mem) == mem.id

    def test_remove_memory_on_bad_dsn_returns_false(self, bad_backend):
        assert bad_backend.remove_memory("x") is False

    def test_get_related_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.get_related("x") == []

    def test_traverse_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.traverse("x") == []

    def test_get_lineage_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.get_lineage("x") == []

    def test_search_by_tags_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.search_by_tags(["x"]) == []

    def test_search_by_entity_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.search_by_entity("x") == []

    def test_find_clusters_on_bad_dsn_returns_empty_list(self, bad_backend):
        assert bad_backend.find_clusters() == []

    def test_get_context_graph_on_bad_dsn_returns_empty_dict(self, bad_backend):
        assert bad_backend.get_context_graph("x") == {}

    def test_stats_on_bad_dsn_returns_not_ok(self, bad_backend):
        result = bad_backend.stats()
        assert result["ok"] is False
        assert "error" in result

    def test_health_check_on_bad_dsn_returns_not_ok(self, bad_backend):
        result = bad_backend.health_check()
        assert result["ok"] is False
        assert "error" in result

    def test_sync_all_on_bad_dsn_counts_all_as_errors(self, bad_backend, tmp_path):
        short_dir = tmp_path / "short-term"
        short_dir.mkdir()
        mem = make_memory(title="Bad DSN Sync")
        (short_dir / f"{mem.id}.json").write_text(json.dumps(mem.model_dump(mode="json")))

        result = bad_backend.sync_all(tmp_path, "test-agent")
        assert result["indexed"] == 0
        assert result["errors"] == 1

    def test_unsafe_graph_name_disables_backend_safely(self):
        be = AGEGraphBackend(dsn=DSN, graph="not a valid name; DROP")
        assert be.graph is None
        assert be.get("x") is None
        assert be.index_memory(make_memory()) is False
        assert be.stats()["ok"] is False
        assert be.health_check()["ok"] is False


# ═══════════════════════════════════════════════════════════
# Deferred methods — explicit NotImplementedError, not silent no-ops
# ═══════════════════════════════════════════════════════════


class TestDeferredMethods:
    def setup_method(self):
        self.be = AGEGraphBackend(dsn=DSN, graph="unit_test_graph_deferred")

    def test_search_by_claim_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.be.search_by_claim("x")

    def test_search_by_section_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.be.search_by_section("x")

    def test_search_by_citation_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.be.search_by_citation("x")

    def test_related_claims_by_citation_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.be.related_claims_by_citation("x")

    def test_update_emotional_not_implemented(self):
        with pytest.raises(NotImplementedError):
            self.be.update_emotional("id", 5.0, 0.5, [])
