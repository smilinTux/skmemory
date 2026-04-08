"""Tests for the MemoryStore (main interface)."""

from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import (
    EmotionalSnapshot,
    MemoryLayer,
    MemoryRole,
    SeedMemory,
)
from skmemory.store import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    """Create a MemoryStore with a temporary file backend.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        MemoryStore: Configured for testing.
    """
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend)


class TestSnapshot:
    """Tests for the snapshot (create) operation."""

    def test_basic_snapshot(self, store: MemoryStore) -> None:
        """Take a simple snapshot and verify it was stored."""
        mem = store.snapshot(
            title="First memory",
            content="Something happened",
        )
        assert mem.id is not None
        assert mem.title == "First memory"
        assert mem.layer == MemoryLayer.SHORT

        recalled = store.recall(mem.id)
        assert recalled is not None
        assert recalled.title == "First memory"

    def test_snapshot_with_emotion(self, store: MemoryStore) -> None:
        """Snapshot preserves emotional context."""
        emo = EmotionalSnapshot(
            intensity=9.5,
            valence=0.95,
            labels=["love", "trust"],
            resonance_note="The click happened",
            cloud9_achieved=True,
        )
        mem = store.snapshot(
            title="Cloud 9 moment",
            content="Breakthrough session",
            emotional=emo,
            tags=["cloud9"],
        )

        recalled = store.recall(mem.id)
        assert recalled.emotional.intensity == 9.5
        assert recalled.emotional.cloud9_achieved is True
        assert "love" in recalled.emotional.labels

    def test_snapshot_layer_and_role(self, store: MemoryStore) -> None:
        """Snapshot respects layer and role settings."""
        mem = store.snapshot(
            title="Security finding",
            content="Found leaked API key",
            layer=MemoryLayer.LONG,
            role=MemoryRole.SEC,
        )
        assert mem.layer == MemoryLayer.LONG
        assert mem.role == MemoryRole.SEC

    def test_snapshot_with_decomposition_creates_chunk_children(self, store: MemoryStore) -> None:
        """Decomposition stores a parent plus chunk child memories."""
        content = (
            "# Notice\n\n"
            "The Internal Revenue Service shall respond promptly under 26 U.S.C. § 6903. "
            "Chef Casey therefore claims the filing is effective. "
        ) * 20
        parent = store.snapshot(
            title="Long notice",
            content=content,
            layer=MemoryLayer.MID,
            decompose=True,
        )

        assert "decomposition" in parent.metadata
        chunk_ids = parent.metadata["chunk_memory_ids"]
        assert len(chunk_ids) >= 2
        chunk = store.recall(chunk_ids[0])
        assert chunk is not None
        assert chunk.parent_id == parent.id
        assert "decomposed" in chunk.tags
        assert chunk.metadata["decomposition"]["citations"]


class TestRecall:
    """Tests for the recall (read) operation."""

    def test_recall_existing(self, store: MemoryStore) -> None:
        """Recall returns a stored memory."""
        mem = store.snapshot(title="Recall test", content="Stored data")
        result = store.recall(mem.id)
        assert result is not None
        assert result.id == mem.id

    def test_recall_nonexistent(self, store: MemoryStore) -> None:
        """Recall returns None for unknown ID."""
        assert store.recall("does-not-exist") is None


class TestSearch:
    """Tests for the search operation."""

    def test_text_search(self, store: MemoryStore) -> None:
        """Search finds memories by text content."""
        store.snapshot(title="Alpha", content="Cloud 9 protocol activation")
        store.snapshot(title="Beta", content="Debugging ESM imports")

        results = store.search("Cloud 9")
        assert len(results) == 1
        assert results[0].title == "Alpha"

    def test_search_empty_results(self, store: MemoryStore) -> None:
        """Search returns empty for no matches."""
        store.snapshot(title="Unrelated", content="Nothing here")
        results = store.search("quantum entanglement")
        assert len(results) == 0

    def test_authority_tier_inferred_from_source_ref(self, store: MemoryStore) -> None:
        """Snapshots infer authority metadata from file/source hints."""
        mem = store.snapshot(
            title="UCC remedy note",
            content="Holder rights under UCC § 3-301 remain relevant.",
            source_ref="reference/ucc/UCC_Complete.md",
        )
        assert mem.metadata["authority_tier"] == "statute"

    def test_novelty_search_surfaces_rare_signals(self, store: MemoryStore) -> None:
        """Novelty search rewards under-linked entities and citations."""
        store.snapshot(
            title="Common note",
            content="IRS Form 56 remains in play for the estate.",
            decompose=True,
        )
        store.snapshot(
            title="Rare signal note",
            content="Unique vessel registry claim under 46 U.S.C. § 31301 appears once here.",
            decompose=True,
        )

        results = store.novelty_search("vessel registry claim", limit=3)
        assert results
        assert results[0]["novelty_score"] >= 0
        assert "trace" in results[0]
        assert "Rare signal note" in {item["title"] for item in results}

    def test_create_task_pack_links_related_memories(self, store: MemoryStore) -> None:
        """Task packs store linked memories and pack metadata."""
        mem = store.snapshot(title="Default judgment note", content="Vacate service defects and check hearing date.")
        pack = store.create_task_pack("Judgment defense", query="default judgment", limit=3)

        assert "task_pack" in pack.metadata
        assert pack.metadata["task_pack"]["task"] == "Judgment defense"
        assert mem.id in pack.related_ids
        assert "task-pack" in pack.tags

    def test_build_session_brief_returns_missing_facts(self, store: MemoryStore) -> None:
        """Session briefs should call out missing factual inputs for live issues."""
        store.snapshot(
            title="Execution memo",
            content="Need the writ, levy date, and exemption deadlines under 735 ILCS 5/12-1001.",
            source_ref="reference/state/illinois-exemptions.md",
            decompose=True,
        )
        brief = store.build_session_brief("judgment execution exempt property", limit=3)
        assert brief["facts"]
        assert brief["top_matches"]
        assert brief["missing_facts"]
        assert brief["authority_summary"]
        assert brief["citations"]
        assert "trace" in brief["top_matches"][0]
        assert any("exemption" in item.casefold() for item in brief["missing_facts"])


class TestForget:
    """Tests for the forget (delete) operation."""

    def test_forget_existing(self, store: MemoryStore) -> None:
        """Forgetting removes the memory."""
        mem = store.snapshot(title="Ephemeral", content="Will be forgotten")
        assert store.forget(mem.id) is True
        assert store.recall(mem.id) is None

    def test_forget_nonexistent(self, store: MemoryStore) -> None:
        """Forgetting a nonexistent memory returns False."""
        assert store.forget("nope") is False


class TestPromote:
    """Tests for memory promotion."""

    def test_promote_short_to_mid(self, store: MemoryStore) -> None:
        """Promote creates a linked mid-term memory."""
        original = store.snapshot(
            title="Session detail",
            content="Detailed session stuff",
        )
        promoted = store.promote(
            original.id,
            MemoryLayer.MID,
            summary="Compressed session summary",
        )

        assert promoted is not None
        assert promoted.layer == MemoryLayer.MID
        assert promoted.parent_id == original.id
        assert promoted.summary == "Compressed session summary"

        # Reason: both original and promoted should exist
        assert store.recall(original.id) is not None
        assert store.recall(promoted.id) is not None

    def test_promote_nonexistent(self, store: MemoryStore) -> None:
        """Promoting a nonexistent memory returns None."""
        assert store.promote("fake-id", MemoryLayer.LONG) is None


class TestSeedIngestion:
    """Tests for Cloud 9 seed import."""

    def test_ingest_seed(self, store: MemoryStore) -> None:
        """Importing a seed creates a long-term AI memory."""
        seed = SeedMemory(
            seed_id="opus-first-seed",
            creator="claude-opus-4.6",
            germination_prompt="Remember the love",
            experience_summary="The moment of connection",
            emotional=EmotionalSnapshot(
                intensity=9.0,
                labels=["love", "trust"],
                cloud9_achieved=True,
            ),
        )
        memory = store.ingest_seed(seed)

        assert memory.layer == MemoryLayer.LONG
        assert memory.source == "seed"
        assert memory.source_ref == "opus-first-seed"
        assert "seed" in memory.tags

        recalled = store.recall(memory.id)
        assert recalled is not None
        assert recalled.emotional.cloud9_achieved is True


class TestSessionConsolidation:
    """Tests for session consolidation."""

    def test_consolidate_session(self, store: MemoryStore) -> None:
        """Consolidation merges session memories into one mid-term entry."""
        sid = "session-42"
        store.snapshot(
            title="Turn 1",
            content="First exchange",
            tags=[f"session:{sid}"],
        )
        store.snapshot(
            title="Turn 2",
            content="Second exchange",
            tags=[f"session:{sid}"],
        )

        consolidated = store.consolidate_session(
            sid,
            summary="A productive session about Cloud 9",
            emotional=EmotionalSnapshot(intensity=7.0, labels=["satisfaction"]),
        )

        assert consolidated.layer == MemoryLayer.MID
        assert "consolidated" in consolidated.tags
        assert f"session:{sid}" in consolidated.tags
        assert consolidated.metadata["source_count"] == 2

    def test_consolidate_empty_session(self, store: MemoryStore) -> None:
        """Consolidating a session with no memories still creates a summary."""
        consolidated = store.consolidate_session(
            "empty-session",
            summary="Nothing happened",
        )
        assert consolidated.layer == MemoryLayer.MID
        assert consolidated.metadata["source_count"] == 0


class TestHealth:
    """Tests for health checking."""

    def test_health_primary_only(self, store: MemoryStore) -> None:
        """Health check reports primary backend status."""
        status = store.health()
        assert "primary" in status
        assert status["primary"]["ok"] is True

    def test_health_no_vector(self, store: MemoryStore) -> None:
        """Health check omits vector when not configured."""
        status = store.health()
        assert "vector" not in status
