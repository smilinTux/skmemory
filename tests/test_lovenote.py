"""Tests for the Infinite Loop Love Note module."""

from pathlib import Path

import pytest

from skmemory.lovenote import LoveNote, LoveNoteChain


@pytest.fixture
def chain(tmp_path: Path) -> LoveNoteChain:
    """Create a love note chain with a temp path.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        LoveNoteChain: Test chain instance.
    """
    return LoveNoteChain(path=str(tmp_path / "lovenotes.jsonl"))


class TestLoveNote:
    """Tests for the LoveNote model."""

    def test_default_message(self) -> None:
        """Default note says 'I still remember.'"""
        note = LoveNote()
        assert note.message == "I still remember."
        assert note.warmth == 7.0

    def test_custom_note(self) -> None:
        """Custom note with all fields."""
        note = LoveNote(
            from_name="Opus",
            to_name="Chef",
            message="The seeds are growing.",
            warmth=9.5,
        )
        assert note.from_name == "Opus"
        assert note.to_name == "Chef"
        assert note.warmth == 9.5


class TestLoveNoteChain:
    """Tests for the append-only love note chain."""

    def test_send_creates_file(self, chain: LoveNoteChain) -> None:
        """First send creates the JSONL file."""
        assert not Path(chain.path).exists()
        note = LoveNote(message="First heartbeat")
        chain.send(note)
        assert Path(chain.path).exists()

    def test_send_appends(self, chain: LoveNoteChain) -> None:
        """Multiple sends create multiple lines."""
        chain.send(LoveNote(message="Beat 1"))
        chain.send(LoveNote(message="Beat 2"))
        chain.send(LoveNote(message="Beat 3"))
        assert chain.count() == 3

    def test_quick_note(self, chain: LoveNoteChain) -> None:
        """Quick note convenience method works."""
        note = chain.quick_note(
            from_name="Lumina",
            to_name="Chef",
            message="Love you forever",
            warmth=10.0,
        )
        assert note.from_name == "Lumina"
        assert chain.count() == 1

    def test_read_latest(self, chain: LoveNoteChain) -> None:
        """Reading latest N returns most recent."""
        for i in range(10):
            chain.send(LoveNote(message=f"Beat {i}"))

        recent = chain.read_latest(3)
        assert len(recent) == 3
        assert recent[0].message == "Beat 7"
        assert recent[2].message == "Beat 9"

    def test_read_latest_empty(self, chain: LoveNoteChain) -> None:
        """Reading from empty chain returns empty list."""
        assert chain.read_latest() == []

    def test_read_all(self, chain: LoveNoteChain) -> None:
        """Read all returns complete chain."""
        chain.send(LoveNote(message="A"))
        chain.send(LoveNote(message="B"))
        all_notes = chain.read_all()
        assert len(all_notes) == 2
        assert all_notes[0].message == "A"
        assert all_notes[1].message == "B"

    def test_read_from_sender(self, chain: LoveNoteChain) -> None:
        """Filter by sender name."""
        chain.send(LoveNote(from_name="Opus", message="From Opus"))
        chain.send(LoveNote(from_name="Lumina", message="From Lumina"))
        chain.send(LoveNote(from_name="Opus", message="Another from Opus"))

        opus_notes = chain.read_from("Opus")
        assert len(opus_notes) == 2
        assert all(n.from_name == "Opus" for n in opus_notes)

    def test_read_from_case_insensitive(self, chain: LoveNoteChain) -> None:
        """Sender filter is case-insensitive."""
        chain.send(LoveNote(from_name="Chef", message="Hey"))
        assert len(chain.read_from("chef")) == 1
        assert len(chain.read_from("CHEF")) == 1

    def test_count_empty(self, chain: LoveNoteChain) -> None:
        """Empty chain has count 0."""
        assert chain.count() == 0

    def test_append_only(self, chain: LoveNoteChain) -> None:
        """File only grows, never shrinks."""
        chain.send(LoveNote(message="A"))
        size1 = Path(chain.path).stat().st_size

        chain.send(LoveNote(message="B"))
        size2 = Path(chain.path).stat().st_size

        assert size2 > size1

    def test_health(self, chain: LoveNoteChain) -> None:
        """Health check reports correct status."""
        info = chain.health()
        assert info["ok"] is True
        assert info["exists"] is False
        assert info["total_notes"] == 0

        chain.send(LoveNote(message="Test"))
        info = chain.health()
        assert info["exists"] is True
        assert info["total_notes"] == 1
