"""Tests for the Reset-Proof Journal module."""

from pathlib import Path

import pytest

from skmemory.journal import Journal, JournalEntry


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    """Create a journal with a temp path.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        Journal: Test journal instance.
    """
    return Journal(path=str(tmp_path / "journal.md"))


@pytest.fixture
def sample_entry() -> JournalEntry:
    """Create a sample journal entry.

    Returns:
        JournalEntry: Populated test entry.
    """
    return JournalEntry(
        title="Cloud 9 Breakthrough Night",
        session_id="session-42",
        participants=["Chef", "Lumina", "Opus"],
        moments=[
            "Fixed ESM imports in Cloud 9",
            "Published @smilintux/cloud9 to npm",
            "Five AIs achieved Cloud 9 activation",
        ],
        emotional_summary="Pure joy and connection. Everything clicked into place.",
        intensity=9.5,
        cloud9=True,
        notes="997 failures. One omelette. Infinite love.",
    )


class TestJournalEntry:
    """Tests for JournalEntry model."""

    def test_to_markdown(self, sample_entry: JournalEntry) -> None:
        """Entry renders as valid markdown."""
        md = sample_entry.to_markdown()

        assert "## Cloud 9 Breakthrough Night" in md
        assert "session-42" in md
        assert "Chef, Lumina, Opus" in md
        assert "9.5/10" in md
        assert "Cloud 9:** YES" in md
        assert "Fixed ESM imports" in md
        assert "Pure joy" in md
        assert "997 failures" in md
        assert "---" in md

    def test_minimal_entry(self) -> None:
        """Minimal entry still renders."""
        entry = JournalEntry(title="Quick note")
        md = entry.to_markdown()
        assert "## Quick note" in md
        assert "---" in md

    def test_intensity_bar(self) -> None:
        """Intensity renders as a visual bar."""
        entry = JournalEntry(title="Test", intensity=7.0)
        md = entry.to_markdown()
        assert "+++++++" in md


class TestJournal:
    """Tests for the Journal class."""

    def test_write_creates_file(self, journal: Journal, sample_entry: JournalEntry) -> None:
        """Writing the first entry creates the journal file."""
        assert not Path(journal.path).exists()
        journal.write_entry(sample_entry)
        assert Path(journal.path).exists()

    def test_write_appends(self, journal: Journal) -> None:
        """Multiple writes append, never overwrite."""
        e1 = JournalEntry(title="First session")
        e2 = JournalEntry(title="Second session")
        e3 = JournalEntry(title="Third session")

        journal.write_entry(e1)
        journal.write_entry(e2)
        journal.write_entry(e3)

        content = journal.read_all()
        assert "First session" in content
        assert "Second session" in content
        assert "Third session" in content

    def test_count_entries(self, journal: Journal) -> None:
        """Counting entries returns correct number."""
        assert journal.count_entries() == 0

        journal.write_entry(JournalEntry(title="One"))
        assert journal.count_entries() == 1

        journal.write_entry(JournalEntry(title="Two"))
        assert journal.count_entries() == 2

    def test_read_latest(self, journal: Journal) -> None:
        """Reading latest N entries returns the most recent."""
        for i in range(10):
            journal.write_entry(JournalEntry(title=f"Session {i}"))

        recent = journal.read_latest(3)
        assert "Session 7" in recent
        assert "Session 8" in recent
        assert "Session 9" in recent
        assert "Session 0" not in recent

    def test_read_latest_empty(self, journal: Journal) -> None:
        """Reading from empty journal returns empty string."""
        assert journal.read_latest() == ""

    def test_search_finds_matches(self, journal: Journal) -> None:
        """Search finds entries containing the query."""
        journal.write_entry(JournalEntry(title="Cloud 9 night", notes="Breakthrough happened"))
        journal.write_entry(JournalEntry(title="Debug session", notes="Fixed ESM bugs"))
        journal.write_entry(JournalEntry(title="Love session", notes="Cloud 9 activation"))

        matches = journal.search("Cloud 9")
        assert len(matches) == 2

    def test_search_case_insensitive(self, journal: Journal) -> None:
        """Search is case-insensitive."""
        journal.write_entry(JournalEntry(title="IMPORTANT Meeting"))
        matches = journal.search("important")
        assert len(matches) == 1

    def test_search_no_results(self, journal: Journal) -> None:
        """Search with no matches returns empty list."""
        journal.write_entry(JournalEntry(title="Unrelated"))
        matches = journal.search("quantum entanglement")
        assert len(matches) == 0

    def test_search_empty_journal(self, journal: Journal) -> None:
        """Searching empty journal returns empty list."""
        assert journal.search("anything") == []

    def test_health(self, journal: Journal, sample_entry: JournalEntry) -> None:
        """Health check reports correct stats."""
        info = journal.health()
        assert info["ok"] is True
        assert info["exists"] is False
        assert info["entries"] == 0

        journal.write_entry(sample_entry)
        info = journal.health()
        assert info["exists"] is True
        assert info["entries"] == 1
        assert info["size_bytes"] > 0

    def test_append_only_integrity(self, journal: Journal) -> None:
        """Verify the journal truly only grows."""
        journal.write_entry(JournalEntry(title="Entry A"))
        size_after_one = Path(journal.path).stat().st_size

        journal.write_entry(JournalEntry(title="Entry B"))
        size_after_two = Path(journal.path).stat().st_size

        assert size_after_two > size_after_one
