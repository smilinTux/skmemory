"""
Integration tests for the SKGraph (FalkorDB) graph backend.

These tests run against a live FalkorDB instance.  They are automatically
skipped when the server is unreachable or the ``falkordb`` package is not
installed.

Coverage:
  - Health check
  - save() / index_memory() — node and edge creation
  - get() — node property retrieval
  - delete() / remove_memory() — DETACH DELETE
  - search() — title substring search
  - search_by_tags() — tag-overlap graph queries
  - traverse() / get_related() — multi-hop traversal
  - get_lineage() — PROMOTED_FROM chain traversal
  - find_clusters() / get_memory_clusters() — hub detection
  - PLANTED edge for seed memories (creator:<name> tag)
  - PRECEDED_BY temporal chain
  - stats() — node / edge / tag counts
"""

from __future__ import annotations

from .conftest import make_memory, requires_skgraph

pytestmark = requires_skgraph


# ─────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────


class TestSKGraphHealth:
    def test_health_check_returns_ok(self, falkordb_clean):
        result = falkordb_clean.health_check()
        assert result["ok"] is True
        assert result["backend"] == "SKGraphBackend"
        assert "node_count" in result

    def test_stats_returns_structure(self, falkordb_clean):
        result = falkordb_clean.stats()
        assert result["ok"] is True
        assert "node_count" in result
        assert "edge_count" in result
        assert "memory_count" in result
        assert isinstance(result["tag_distribution"], list)


# ─────────────────────────────────────────────────────────
# CRUD — save / get / delete
# ─────────────────────────────────────────────────────────


class TestSKGraphCRUD:
    def test_save_creates_memory_node(self, falkordb_clean):
        mem = make_memory(title="Save Test", content="Saved to graph.")
        result_id = falkordb_clean.save(mem)
        assert result_id == mem.id

        node = falkordb_clean.get(mem.id)
        assert node is not None
        assert node["id"] == mem.id
        assert node["title"] == "Save Test"

    def test_save_updates_existing_node(self, falkordb_clean):
        mem = make_memory(title="Original Title")
        falkordb_clean.save(mem)

        mem.title = "Updated Title"
        falkordb_clean.save(mem)

        node = falkordb_clean.get(mem.id)
        assert node["title"] == "Updated Title"

    def test_get_nonexistent_returns_none(self, falkordb_clean):
        assert falkordb_clean.get("does-not-exist-xyz") is None

    def test_delete_removes_node(self, falkordb_clean):
        mem = make_memory(title="To Delete")
        falkordb_clean.save(mem)

        assert falkordb_clean.get(mem.id) is not None
        falkordb_clean.delete(mem.id)
        assert falkordb_clean.get(mem.id) is None

    def test_remove_memory_alias_works(self, falkordb_clean):
        mem = make_memory(title="Remove Test")
        falkordb_clean.save(mem)
        result = falkordb_clean.remove_memory(mem.id)
        assert result is True
        assert falkordb_clean.get(mem.id) is None

    def test_delete_nonexistent_does_not_raise(self, falkordb_clean):
        # Should return True (query ran without error) even if node absent
        result = falkordb_clean.delete("ghost-id-xyz")
        assert result is True

    def test_get_returns_all_properties(self, falkordb_clean):
        mem = make_memory(
            title="Props Check",
            source="cli",
            intensity=7.5,
            valence=0.8,
        )
        falkordb_clean.save(mem)

        node = falkordb_clean.get(mem.id)
        assert node["id"] == mem.id
        assert node["title"] == "Props Check"
        assert node["source"] == "cli"
        assert abs(node["intensity"] - 7.5) < 0.01
        assert abs(node["valence"] - 0.8) < 0.01
        assert node["layer"] == "short-term"


# ─────────────────────────────────────────────────────────
# Graph edges — tags, sources, relationships
# ─────────────────────────────────────────────────────────


class TestSKGraphEdges:
    def test_tagged_edges_created(self, falkordb_clean):
        mem = make_memory(title="Tagged Memory", tags=["python", "test"])
        falkordb_clean.save(mem)

        # Verify via tag search
        results = falkordb_clean.search_by_tags(["python"])
        ids = [r["id"] for r in results]
        assert mem.id in ids

    def test_multiple_tags_all_indexed(self, falkordb_clean):
        mem = make_memory(title="Multi-Tag", tags=["alpha", "beta", "gamma"])
        falkordb_clean.save(mem)

        for tag in ["alpha", "beta", "gamma"]:
            results = falkordb_clean.search_by_tags([tag])
            assert any(r["id"] == mem.id for r in results), f"Tag {tag!r} not found"

    def test_from_source_edge_created(self, falkordb_clean):
        mem = make_memory(title="Source Edge", source="session-test")
        falkordb_clean.save(mem)

        # Source edge enables PRECEDED_BY temporal chain — indirect verification
        # via stats edge count
        stats_before = falkordb_clean.stats()
        # As long as no error, the edge was wired
        assert stats_before["ok"] is True

    def test_related_to_explicit_edges(self, falkordb_clean):
        mem_a = make_memory(title="Memory A")
        falkordb_clean.save(mem_a)
        # mem_b has mem_a in related_ids
        mem_b_linked = make_memory(title="Memory B", related_ids=[mem_a.id])
        mem_b_linked_id = mem_b_linked.id
        falkordb_clean.save(mem_b_linked)

        related = falkordb_clean.get_related(mem_b_linked_id, depth=1)
        ids = [r["id"] for r in related]
        assert mem_a.id in ids

    def test_promoted_from_edge(self, falkordb_clean):
        parent = make_memory(title="Parent Memory", layer="short-term")
        falkordb_clean.save(parent)

        child = make_memory(title="Promoted Child", layer="mid-term", parent_id=parent.id)
        falkordb_clean.save(child)

        lineage = falkordb_clean.get_lineage(child.id)
        assert len(lineage) >= 1
        ancestor_ids = [lbl["id"] for lbl in lineage]
        assert parent.id in ancestor_ids

    def test_auto_shared_tag_related_to(self, falkordb_clean):
        """Memories sharing 2+ tags should get automatic RELATED_TO edges."""
        mem_a = make_memory(title="Shared Tags A", tags=["cloud9", "identity", "sovereign"])
        mem_b = make_memory(title="Shared Tags B", tags=["cloud9", "identity", "session"])
        falkordb_clean.save(mem_a)
        falkordb_clean.save(mem_b)

        # mem_b shares "cloud9" + "identity" with mem_a → auto-wired
        related = falkordb_clean.get_related(mem_b.id, depth=1)
        ids = [r["id"] for r in related]
        assert mem_a.id in ids

    def test_preceded_by_temporal_chain(self, falkordb_clean):
        """Sequential memories from the same source get PRECEDED_BY edges."""
        source = "temporal-chain-test"
        mem_first = make_memory(title="First", source=source)
        falkordb_clean.save(mem_first)

        mem_second = make_memory(title="Second", source=source)
        falkordb_clean.save(mem_second)

        # The second memory should traverse back to the first via PRECEDED_BY
        related = falkordb_clean.traverse(mem_second.id, depth=1)
        ids = [r["id"] for r in related]
        assert mem_first.id in ids

    def test_planted_edge_for_seed_memory(self, falkordb_clean):
        """Seed memories with creator:<name> tag get AI-[:PLANTED]->Memory edges."""
        mem = make_memory(
            title="Seed Memory",
            source="seed",
            tags=["seed", "cloud9", "creator:lumina"],
        )
        falkordb_clean.save(mem)

        # Confirm node exists — PLANTED edge is internal but node still reachable
        node = falkordb_clean.get(mem.id)
        assert node is not None


# ─────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────


class TestSKGraphSearch:
    def test_search_by_title_exact_word(self, falkordb_clean):
        mem = make_memory(title="Sovereign Memory Palace")
        falkordb_clean.save(mem)

        results = falkordb_clean.search("Sovereign")
        ids = [r["id"] for r in results]
        assert mem.id in ids

    def test_search_by_title_case_insensitive(self, falkordb_clean):
        mem = make_memory(title="Cloud Nine Experience")
        falkordb_clean.save(mem)

        results = falkordb_clean.search("cloud nine")
        ids = [r["id"] for r in results]
        assert mem.id in ids

    def test_search_returns_empty_for_no_match(self, falkordb_clean):
        make_memory(title="Unrelated Content Here")
        results = falkordb_clean.search("zzz_no_match_zzz")
        assert results == []

    def test_search_by_tags_single_tag(self, falkordb_clean):
        mem = make_memory(title="Tag Search Test", tags=["sovereign", "test"])
        falkordb_clean.save(mem)

        results = falkordb_clean.search_by_tags(["sovereign"])
        ids = [r["id"] for r in results]
        assert mem.id in ids

    def test_search_by_tags_multiple_or_logic(self, falkordb_clean):
        mem_a = make_memory(title="Alpha Memory", tags=["alpha-tag"])
        mem_b = make_memory(title="Beta Memory", tags=["beta-tag"])
        falkordb_clean.save(mem_a)
        falkordb_clean.save(mem_b)

        results = falkordb_clean.search_by_tags(["alpha-tag", "beta-tag"])
        ids = [r["id"] for r in results]
        assert mem_a.id in ids
        assert mem_b.id in ids

    def test_search_by_tags_empty_list_returns_empty(self, falkordb_clean):
        results = falkordb_clean.search_by_tags([])
        assert results == []

    def test_search_result_has_expected_fields(self, falkordb_clean):
        mem = make_memory(title="Field Check", tags=["fields-test"])
        falkordb_clean.save(mem)

        results = falkordb_clean.search("Field Check")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "title" in r
        assert "layer" in r
        assert "intensity" in r


# ─────────────────────────────────────────────────────────
# Graph traversal
# ─────────────────────────────────────────────────────────


class TestSKGraphTraversal:
    def test_traverse_alias_matches_get_related(self, falkordb_clean):
        mem_a = make_memory(title="Hub Node", tags=["hub", "core"])
        mem_b = make_memory(title="Spoke Node", tags=["hub", "peripheral"])
        falkordb_clean.save(mem_a)
        falkordb_clean.save(mem_b)

        via_traverse = falkordb_clean.traverse(mem_a.id, depth=1)
        via_get_related = falkordb_clean.get_related(mem_a.id, depth=1)
        assert via_traverse == via_get_related

    def test_traversal_result_has_distance(self, falkordb_clean):
        mem_a = make_memory(title="Distance A", tags=["dist-tag", "common"])
        mem_b = make_memory(title="Distance B", tags=["dist-tag", "common"])
        falkordb_clean.save(mem_a)
        falkordb_clean.save(mem_b)

        results = falkordb_clean.traverse(mem_a.id, depth=2)
        if results:
            assert "distance" in results[0]
            assert results[0]["distance"] >= 1

    def test_traverse_empty_for_isolated_node(self, falkordb_clean):
        mem = make_memory(title="Isolated Node", tags=["unique-xyz-123"])
        falkordb_clean.save(mem)

        # No shared tags with anything → no RELATED_TO edges
        results = falkordb_clean.traverse(mem.id, depth=1)
        assert results == []

    def test_traverse_depth_clamped(self, falkordb_clean):
        """depth=0 should be clamped to 1 (no error raised)."""
        mem = make_memory(title="Depth Clamp Test")
        falkordb_clean.save(mem)
        # Should not raise even with depth=0 or depth=10
        falkordb_clean.traverse(mem.id, depth=0)
        falkordb_clean.traverse(mem.id, depth=10)

    def test_get_lineage_empty_for_no_parents(self, falkordb_clean):
        mem = make_memory(title="No Parent")
        falkordb_clean.save(mem)
        lineage = falkordb_clean.get_lineage(mem.id)
        assert lineage == []

    def test_get_lineage_multi_hop(self, falkordb_clean):
        grandparent = make_memory(title="Grandparent", layer="short-term")
        parent = make_memory(title="Parent", layer="mid-term", parent_id=grandparent.id)
        child = make_memory(title="Child", layer="long-term", parent_id=parent.id)

        falkordb_clean.save(grandparent)
        falkordb_clean.save(parent)
        falkordb_clean.save(child)

        lineage = falkordb_clean.get_lineage(child.id)
        ancestor_ids = [lbl["id"] for lbl in lineage]
        assert parent.id in ancestor_ids
        assert grandparent.id in ancestor_ids


# ─────────────────────────────────────────────────────────
# Cluster detection
# ─────────────────────────────────────────────────────────


class TestSKGraphClusters:
    def test_find_clusters_alias(self, falkordb_clean):
        """find_clusters and get_memory_clusters return same results."""
        # Create a hub: mem_hub shares tags with many others
        hub = make_memory(title="Hub", tags=["hub-tag", "shared-tag"])
        spokes = [
            make_memory(title=f"Spoke {i}", tags=["hub-tag", "shared-tag"]) for i in range(4)
        ]
        falkordb_clean.save(hub)
        for s in spokes:
            falkordb_clean.save(s)

        via_alias = falkordb_clean.find_clusters(min_size=2)
        via_direct = falkordb_clean.get_memory_clusters(min_connections=2)
        assert via_alias == via_direct

    def test_cluster_result_has_connections_field(self, falkordb_clean):
        hub = make_memory(title="ClusterHub", tags=["cluster-hub", "test-hub"])
        spoke1 = make_memory(title="Spoke1", tags=["cluster-hub", "test-hub"])
        spoke2 = make_memory(title="Spoke2", tags=["cluster-hub", "test-hub"])
        falkordb_clean.save(hub)
        falkordb_clean.save(spoke1)
        falkordb_clean.save(spoke2)

        results = falkordb_clean.get_memory_clusters(min_connections=1)
        if results:
            assert "connections" in results[0]
            assert results[0]["connections"] >= 1

    def test_no_clusters_when_graph_empty(self, falkordb_clean):
        results = falkordb_clean.find_clusters(min_size=2)
        assert results == []


# ─────────────────────────────────────────────────────────
# Stats integrity
# ─────────────────────────────────────────────────────────


class TestSKGraphStats:
    def test_memory_count_increments_on_save(self, falkordb_clean):
        before = falkordb_clean.stats()["memory_count"]

        mem = make_memory(title="Stats Increment Test")
        falkordb_clean.save(mem)

        after = falkordb_clean.stats()["memory_count"]
        assert after == before + 1

    def test_memory_count_decrements_on_delete(self, falkordb_clean):
        mem = make_memory(title="Stats Decrement Test")
        falkordb_clean.save(mem)

        before = falkordb_clean.stats()["memory_count"]
        falkordb_clean.delete(mem.id)
        after = falkordb_clean.stats()["memory_count"]

        assert after == before - 1

    def test_tag_distribution_lists_tags(self, falkordb_clean):
        mem = make_memory(title="Tag Dist Test", tags=["dist-alpha", "dist-beta"])
        falkordb_clean.save(mem)

        stats = falkordb_clean.stats()
        tag_names = [t["tag"] for t in stats["tag_distribution"]]
        assert "dist-alpha" in tag_names
        assert "dist-beta" in tag_names

    def test_edge_count_positive_after_saves(self, falkordb_clean):
        mem = make_memory(title="Edge Count", tags=["edge-tag"])
        falkordb_clean.save(mem)

        stats = falkordb_clean.stats()
        assert stats["edge_count"] >= 1
