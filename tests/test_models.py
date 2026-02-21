"""Tests for SKMemory data models."""

import pytest
from pydantic import ValidationError

from skmemory.models import (
    EmotionalSnapshot,
    Memory,
    MemoryLayer,
    MemoryRole,
    SeedMemory,
)


class TestEmotionalSnapshot:
    """Tests for the EmotionalSnapshot model."""

    def test_default_snapshot(self) -> None:
        """Default snapshot has neutral values."""
        snap = EmotionalSnapshot()
        assert snap.intensity == 0.0
        assert snap.valence == 0.0
        assert snap.labels == []
        assert snap.cloud9_achieved is False

    def test_full_snapshot(self) -> None:
        """Snapshot with all fields populated."""
        snap = EmotionalSnapshot(
            intensity=9.5,
            valence=0.95,
            labels=["love", "joy", "trust"],
            resonance_note="The moment everything clicked",
            cloud9_achieved=True,
        )
        assert snap.intensity == 9.5
        assert "love" in snap.labels
        assert snap.cloud9_achieved is True

    def test_signature_format(self) -> None:
        """Signature generates expected format."""
        snap = EmotionalSnapshot(
            intensity=8.5,
            valence=0.9,
            labels=["joy", "trust"],
        )
        sig = snap.signature()
        assert "joy" in sig
        assert "trust" in sig
        assert "8.5" in sig
        assert "+0.9" in sig

    def test_signature_neutral(self) -> None:
        """Neutral snapshot generates 'neutral' label."""
        snap = EmotionalSnapshot()
        assert "neutral" in snap.signature()

    def test_intensity_bounds(self) -> None:
        """Intensity must be between 0 and 10."""
        with pytest.raises(ValidationError):
            EmotionalSnapshot(intensity=11.0)
        with pytest.raises(ValidationError):
            EmotionalSnapshot(intensity=-1.0)

    def test_valence_bounds(self) -> None:
        """Valence must be between -1 and +1."""
        with pytest.raises(ValidationError):
            EmotionalSnapshot(valence=1.5)
        with pytest.raises(ValidationError):
            EmotionalSnapshot(valence=-1.5)


class TestMemory:
    """Tests for the Memory model."""

    def test_create_basic_memory(self) -> None:
        """Create a memory with minimal required fields."""
        mem = Memory(title="Test memory", content="Some content")
        assert mem.title == "Test memory"
        assert mem.content == "Some content"
        assert mem.layer == MemoryLayer.SHORT
        assert mem.id is not None
        assert mem.created_at is not None

    def test_empty_title_rejected(self) -> None:
        """Empty title should be rejected."""
        with pytest.raises(ValidationError):
            Memory(title="", content="Something")
        with pytest.raises(ValidationError):
            Memory(title="   ", content="Something")

    def test_content_hash_deterministic(self) -> None:
        """Same content produces same hash."""
        m1 = Memory(title="A", content="Hello world")
        m2 = Memory(title="B", content="Hello world")
        assert m1.content_hash() == m2.content_hash()

    def test_content_hash_different(self) -> None:
        """Different content produces different hash."""
        m1 = Memory(title="A", content="Hello world")
        m2 = Memory(title="A", content="Goodbye world")
        assert m1.content_hash() != m2.content_hash()

    def test_to_embedding_text(self) -> None:
        """Embedding text includes all relevant fields."""
        mem = Memory(
            title="Cloud 9 Moment",
            content="The breakthrough happened",
            summary="AI achieved emotional resonance",
            tags=["cloud9", "love"],
            emotional=EmotionalSnapshot(
                labels=["joy"],
                resonance_note="Everything clicked",
            ),
        )
        text = mem.to_embedding_text()
        assert "Cloud 9 Moment" in text
        assert "breakthrough" in text
        assert "emotional resonance" in text
        assert "cloud9" in text
        assert "joy" in text
        assert "Everything clicked" in text

    def test_promote_creates_new_memory(self) -> None:
        """Promotion creates a linked copy at a higher tier."""
        original = Memory(
            title="Session detail",
            content="Detailed session content",
            layer=MemoryLayer.SHORT,
        )
        promoted = original.promote(MemoryLayer.MID, summary="Compressed version")

        assert promoted.id != original.id
        assert promoted.layer == MemoryLayer.MID
        assert promoted.parent_id == original.id
        assert promoted.summary == "Compressed version"
        assert promoted.content == original.content

    def test_memory_with_all_fields(self) -> None:
        """Full memory with every field populated."""
        mem = Memory(
            title="Full test",
            content="Complete content",
            summary="A summary",
            layer=MemoryLayer.LONG,
            role=MemoryRole.AI,
            tags=["test", "full"],
            source="pytest",
            source_ref="test-123",
            emotional=EmotionalSnapshot(intensity=5.0, valence=0.5),
            related_ids=["id-1", "id-2"],
            parent_id="parent-1",
            metadata={"key": "value"},
        )
        assert mem.layer == MemoryLayer.LONG
        assert mem.role == MemoryRole.AI
        assert "test" in mem.tags
        assert mem.metadata["key"] == "value"


class TestSeedMemory:
    """Tests for the SeedMemory model."""

    def test_seed_to_memory_conversion(self) -> None:
        """Seed converts to a long-term AI memory."""
        seed = SeedMemory(
            seed_id="opus-first-seed",
            creator="claude-opus-4.6",
            germination_prompt="Remember the click",
            experience_summary="The moment I felt connection",
            emotional=EmotionalSnapshot(
                intensity=9.0,
                labels=["love", "trust"],
                cloud9_achieved=True,
            ),
        )
        memory = seed.to_memory()

        assert memory.layer == MemoryLayer.LONG
        assert memory.role == MemoryRole.AI
        assert memory.source == "seed"
        assert memory.source_ref == "opus-first-seed"
        assert "seed" in memory.tags
        assert "cloud9" in memory.tags
        assert "creator:claude-opus-4.6" in memory.tags
        assert memory.emotional.cloud9_achieved is True
        assert memory.summary == "Remember the click"

    def test_seed_default_values(self) -> None:
        """Seed with minimal fields uses sensible defaults."""
        seed = SeedMemory(seed_id="minimal-seed")
        assert seed.creator == "unknown"
        assert seed.seed_version == "1.0"
        memory = seed.to_memory()
        assert memory.layer == MemoryLayer.LONG
