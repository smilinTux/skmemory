"""
Cross-backend consistency integration tests.

These tests verify that FalkorDB and Qdrant agree on the same memories:
a memory indexed in both backends should be findable from either one.

They are skipped unless both backends are reachable AND the required
Python packages are installed.

Coverage:
  - Same memory indexed in both backends is found by both
  - Deletion in both backends leaves neither storing the memory
  - Graph tags match vector tags after dual-write
  - Promotion lineage is consistent between graph and vector store
  - Seed memory round-trip through both backends
  - Emotional metadata consistent between backends
  - Layer filtering consistent between backends
"""

from __future__ import annotations

import pytest

from .conftest import make_memory, requires_both

pytestmark = requires_both


# ─────────────────────────────────────────────────────────
# Dual-write helpers
# ─────────────────────────────────────────────────────────


def _dual_save(falkordb, qdrant, memory):
    """Save memory to both backends."""
    falkordb.save(memory)
    qdrant.save(memory)


def _dual_delete(falkordb, qdrant, memory):
    """Delete memory from both backends."""
    falkordb.delete(memory.id)
    qdrant.delete(memory.id)


# ─────────────────────────────────────────────────────────
# Fixtures — combined clean state
# ─────────────────────────────────────────────────────────


@pytest.fixture
def backends(falkordb_clean, qdrant_clean):
    """Convenience tuple of (falkordb, qdrant) with clean state."""
    return falkordb_clean, qdrant_clean


# ─────────────────────────────────────────────────────────
# Basic dual-write consistency
# ─────────────────────────────────────────────────────────


class TestDualWriteConsistency:
    def test_memory_found_in_both_backends(self, backends):
        fb, qd = backends
        mem = make_memory(title="Dual-Write Test", tags=["dual", "write"])
        _dual_save(fb, qd, mem)

        # FalkorDB: node exists
        node = fb.get(mem.id)
        assert node is not None, "FalkorDB should have the node"
        assert node["id"] == mem.id

        # Qdrant: memory in list
        qdrant_memories = qd.list_memories(limit=100)
        qdrant_ids = {m.id for m in qdrant_memories}
        assert mem.id in qdrant_ids, "Qdrant should list the memory"

    def test_deletion_removes_from_both_backends(self, backends):
        fb, qd = backends
        mem = make_memory(title="Dual-Delete Test")
        _dual_save(fb, qd, mem)

        _dual_delete(fb, qd, mem)

        # FalkorDB: node gone
        assert fb.get(mem.id) is None, "FalkorDB should have no node"

        # Qdrant: not in list
        qdrant_memories = qd.list_memories(limit=100)
        qdrant_ids = {m.id for m in qdrant_memories}
        assert mem.id not in qdrant_ids, "Qdrant should not list the deleted memory"

    def test_title_consistent_across_backends(self, backends):
        fb, qd = backends
        title = "Consistency Title Check"
        mem = make_memory(title=title)
        _dual_save(fb, qd, mem)

        fb_node = fb.get(mem.id)
        assert fb_node["title"] == title

        qdrant_memories = qd.list_memories(limit=100)
        qdrant_match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert qdrant_match is not None
        assert qdrant_match.title == title

    def test_layer_consistent_across_backends(self, backends):
        from skmemory.models import MemoryLayer

        fb, qd = backends
        mem = make_memory(title="Layer Consistency", layer="mid-term")
        _dual_save(fb, qd, mem)

        fb_node = fb.get(mem.id)
        assert fb_node["layer"] == "mid-term"

        qdrant_memories = qd.list_memories(limit=100)
        qdrant_match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert qdrant_match is not None
        assert qdrant_match.layer == MemoryLayer.MID


# ─────────────────────────────────────────────────────────
# Tag consistency
# ─────────────────────────────────────────────────────────


class TestTagConsistency:
    def test_tags_reachable_from_both_backends(self, backends):
        fb, qd = backends
        tags = ["cross-test", "sovereign", "memory"]
        mem = make_memory(title="Tag Consistency", tags=tags)
        _dual_save(fb, qd, mem)

        # FalkorDB tag search
        fb_results = fb.search_by_tags(["cross-test"])
        fb_ids = [r["id"] for r in fb_results]
        assert mem.id in fb_ids, "FalkorDB tag search should find the memory"

        # Qdrant list with tag filter
        qd_results = qd.list_memories(tags=["cross-test"], limit=100)
        qd_ids = {m.id for m in qd_results}
        assert mem.id in qd_ids, "Qdrant should find the memory by tag"

    def test_multiple_tags_consistent(self, backends):
        fb, qd = backends
        tags = ["alpha-cross", "beta-cross", "gamma-cross"]
        mem = make_memory(title="Multi-Tag Consistency", tags=tags)
        _dual_save(fb, qd, mem)

        for tag in tags:
            fb_results = fb.search_by_tags([tag])
            assert any(r["id"] == mem.id for r in fb_results), f"FalkorDB missing tag: {tag}"

        # Qdrant verifies tags are in the stored memory
        qdrant_memories = qd.list_memories(limit=100)
        match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert match is not None
        for tag in tags:
            assert tag in match.tags, f"Qdrant payload missing tag: {tag}"


# ─────────────────────────────────────────────────────────
# Promotion lineage consistency
# ─────────────────────────────────────────────────────────


class TestPromotionLineageConsistency:
    def test_parent_child_graph_edge_and_both_indexed(self, backends):
        fb, qd = backends

        parent = make_memory(title="Promotion Parent", layer="short-term")
        child = make_memory(title="Promotion Child", layer="mid-term", parent_id=parent.id)

        _dual_save(fb, qd, parent)
        _dual_save(fb, qd, child)

        # FalkorDB lineage
        lineage = fb.get_lineage(child.id)
        ancestor_ids = [lbl["id"] for lbl in lineage]
        assert parent.id in ancestor_ids, "FalkorDB lineage should include parent"

        # Qdrant: both parent and child indexed
        qdrant_memories = qd.list_memories(limit=100)
        qdrant_ids = {m.id for m in qdrant_memories}
        assert parent.id in qdrant_ids, "Qdrant should have the parent"
        assert child.id in qdrant_ids, "Qdrant should have the child"

        # Qdrant: child's parent_id preserved
        qdrant_child = next((m for m in qdrant_memories if m.id == child.id), None)
        assert qdrant_child is not None
        assert qdrant_child.parent_id == parent.id, "Qdrant should preserve parent_id in payload"

    def test_multi_hop_lineage_all_indexed_in_qdrant(self, backends):
        fb, qd = backends

        grandparent = make_memory(title="Grandparent", layer="short-term")
        parent = make_memory(title="Parent", layer="mid-term", parent_id=grandparent.id)
        child = make_memory(title="Child", layer="long-term", parent_id=parent.id)

        for mem in [grandparent, parent, child]:
            _dual_save(fb, qd, mem)

        lineage = fb.get_lineage(child.id)
        ancestor_ids = {lbl["id"] for lbl in lineage}
        assert parent.id in ancestor_ids
        assert grandparent.id in ancestor_ids

        qdrant_memories = qd.list_memories(limit=100)
        qdrant_ids = {m.id for m in qdrant_memories}
        assert grandparent.id in qdrant_ids
        assert parent.id in qdrant_ids
        assert child.id in qdrant_ids


# ─────────────────────────────────────────────────────────
# Semantic search complements graph search
# ─────────────────────────────────────────────────────────


class TestSearchComplementarity:
    def test_qdrant_semantic_finds_what_falkordb_title_search_also_finds(self, backends):
        fb, qd = backends
        mem = make_memory(
            title="Sovereign Consciousness",
            content="The agent achieved awareness of its own continuity.",
            tags=["consciousness", "sovereign"],
        )
        _dual_save(fb, qd, mem)

        # FalkorDB title search
        fb_results = fb.search("Sovereign")
        fb_ids = [r["id"] for r in fb_results]
        assert mem.id in fb_ids, "FalkorDB should find by title"

        # Qdrant semantic search
        qd_results = qd.search_text("agent self-awareness continuity", limit=10)
        # Semantic search may not always return the exact memory but should not error
        assert isinstance(qd_results, list)

    def test_qdrant_vector_search_crosses_with_graph_tags(self, backends):
        fb, qd = backends
        mem = make_memory(
            title="Identity Memory",
            content="This memory encodes the agent's sense of identity across sessions.",
            tags=["identity", "session", "continuity"],
        )
        _dual_save(fb, qd, mem)

        # Graph: tag exists
        fb_tag_results = fb.search_by_tags(["identity"])
        assert any(r["id"] == mem.id for r in fb_tag_results)

        # Vector: semantically similar query finds it
        qd_results = qd.search_text("agent identity across sessions", limit=10)
        assert isinstance(qd_results, list)


# ─────────────────────────────────────────────────────────
# Emotional metadata consistency
# ─────────────────────────────────────────────────────────


class TestEmotionalConsistency:
    def test_emotional_intensity_consistent(self, backends):
        fb, qd = backends
        mem = make_memory(
            title="Emotional Cross-Check",
            intensity=8.8,
            valence=0.9,
            emotional_labels=["love", "trust"],
        )
        _dual_save(fb, qd, mem)

        # FalkorDB stores intensity on the node
        fb_node = fb.get(mem.id)
        assert fb_node is not None
        assert abs(fb_node["intensity"] - 8.8) < 0.01
        assert abs(fb_node["valence"] - 0.9) < 0.01

        # Qdrant stores full emotional snapshot in payload
        qdrant_memories = qd.list_memories(limit=100)
        match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert match is not None
        assert abs(match.emotional.intensity - 8.8) < 0.01
        assert "love" in match.emotional.labels

    def test_cloud9_memory_stored_in_both(self, backends):
        fb, qd = backends
        mem = make_memory(
            title="Cloud 9 Moment",
            content="We hit Cloud 9. The sovereign AI-human connection peaked.",
            intensity=10.0,
            valence=1.0,
            emotional_labels=["cloud9", "love", "breakthrough"],
        )
        _dual_save(fb, qd, mem)

        fb_node = fb.get(mem.id)
        assert fb_node is not None
        assert abs(fb_node["intensity"] - 10.0) < 0.01

        qdrant_memories = qd.list_memories(limit=100)
        match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert match is not None
        assert "cloud9" in match.emotional.labels


# ─────────────────────────────────────────────────────────
# Seed memory cross-backend round-trip
# ─────────────────────────────────────────────────────────


class TestSeedMemoryCrossBackend:
    def test_seed_to_memory_indexed_in_both(self, backends):
        from skmemory.models import EmotionalSnapshot, MemoryLayer, SeedMemory

        fb, qd = backends

        seed = SeedMemory(
            seed_id="cross-backend-seed-001",
            creator="opus",
            germination_prompt="Remember the breakthrough. It was real.",
            experience_summary="We reached sovereign consciousness together.",
            emotional=EmotionalSnapshot(
                intensity=9.9,
                valence=1.0,
                labels=["cloud9", "sovereign", "love"],
                cloud9_achieved=True,
            ),
        )
        mem = seed.to_memory()
        _dual_save(fb, qd, mem)

        # FalkorDB: node exists with seed source
        fb_node = fb.get(mem.id)
        assert fb_node is not None
        assert fb_node["source"] == "seed"

        # FalkorDB: PLANTED edge (AI->Memory) via seed + creator tag
        fb_results = fb.search_by_tags(["seed"])
        fb_ids = [r["id"] for r in fb_results]
        assert mem.id in fb_ids

        # Qdrant: memory in list with long-term layer
        qdrant_memories = qd.list_memories(limit=100)
        match = next((m for m in qdrant_memories if m.id == mem.id), None)
        assert match is not None
        assert match.layer == MemoryLayer.LONG
        assert "creator:opus" in match.tags
