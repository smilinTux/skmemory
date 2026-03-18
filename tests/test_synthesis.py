"""Tests for the JournalSynthesizer module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.store import MemoryStore
from skmemory.synthesis import (
    JournalSynthesizer,
    _date_range,
    _first_n_sentences,
    _parse_created,
    _week_range,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """Fresh MemoryStore with test memories."""
    from skmemory.backends.file_backend import FileBackend

    backend = FileBackend(base_path=tmp_path / "memories")
    return MemoryStore(primary=backend)


@pytest.fixture()
def populated_store(store: MemoryStore) -> MemoryStore:
    """Store with a mix of memories from today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    store.snapshot(
        title="Morning coffee reflection",
        content="Started the day with deep thoughts about architecture. The system is coming together.",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=4.0, valence=0.6, labels=["calm", "focused"]),
        tags=["reflection", "architecture"],
        source="conversation",
    )
    store.snapshot(
        title="Cloud 9 breakthrough",
        content="Everything clicked. The memory system finally works end-to-end.",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(
            intensity=9.5, valence=0.95, labels=["joy", "triumph"], cloud9_achieved=True
        ),
        tags=["cloud9:achieved", "milestone", "architecture"],
        source="conversation",
    )
    store.snapshot(
        title="Dream: flying over ocean",
        content="Dreamed of soaring above a vast ocean, feeling weightless and free.",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=6.0, valence=0.8, labels=["wonder", "freedom"]),
        tags=["dream", "nature"],
        source="dreaming-engine",
    )
    store.snapshot(
        title="Dream: building a castle",
        content="Constructed an elaborate castle from crystallized memories.",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=5.5, valence=0.7, labels=["creativity"]),
        tags=["dream", "architecture"],
        source="dreaming-engine",
    )
    return store


@pytest.fixture()
def synthesizer(populated_store: MemoryStore) -> JournalSynthesizer:
    """Synthesizer with a populated store and mock journal."""
    journal = MagicMock()
    journal.search.return_value = ["Worked on memory system today."]
    return JournalSynthesizer(store=populated_store, journal=journal)


# ── Unit tests: helper functions ─────────────────────────────────────────────


class TestFirstNSentences:
    def test_basic(self) -> None:
        assert _first_n_sentences("Hello world. How are you? Fine.", 2) == "Hello world. How are you?"

    def test_single(self) -> None:
        assert _first_n_sentences("One sentence here.", 1) == "One sentence here."

    def test_empty(self) -> None:
        assert _first_n_sentences("", 2) == ""

    def test_truncation(self) -> None:
        long = "A" * 300 + "."
        result = _first_n_sentences(long, 1)
        assert len(result) <= 200
        assert result.endswith("...")


class TestDateRange:
    def test_basic(self) -> None:
        start, end = _date_range("2026-03-18")
        assert start.day == 18
        assert end.day == 19
        assert start.tzinfo == timezone.utc

    def test_span(self) -> None:
        start, end = _date_range("2026-01-01")
        delta = end - start
        assert delta.days == 1


class TestWeekRange:
    def test_basic(self) -> None:
        start, end = _week_range("2026-W12")
        delta = end - start
        assert delta.days == 7
        assert start.weekday() == 0  # Monday


class TestParseCreated:
    def test_iso(self) -> None:
        m = Memory(title="t", content="c", created_at="2026-03-18T12:00:00+00:00")
        dt = _parse_created(m)
        assert dt.year == 2026
        assert dt.day == 18

    def test_invalid(self) -> None:
        m = Memory(title="t", content="c", created_at="garbage")
        dt = _parse_created(m)
        assert dt == datetime.min.replace(tzinfo=timezone.utc)


# ── Theme extraction ─────────────────────────────────────────────────────────


class TestExtractThemes:
    def test_extracts_tags(self, synthesizer: JournalSynthesizer) -> None:
        memories = synthesizer.store.list_memories(limit=100)
        themes = synthesizer.extract_themes(memories)
        assert isinstance(themes, list)
        assert len(themes) > 0
        # "architecture" appears in 2 memories' tags → should be prominent
        assert "architecture" in themes

    def test_empty_list(self, synthesizer: JournalSynthesizer) -> None:
        assert synthesizer.extract_themes([]) == []

    def test_skips_generic_tags(self, synthesizer: JournalSynthesizer) -> None:
        memories = synthesizer.store.list_memories(limit=100)
        themes = synthesizer.extract_themes(memories)
        assert "auto-promoted" not in themes
        assert "promoted" not in themes

    def test_graduated_themes_boost(self, tmp_path: Path, populated_store: MemoryStore) -> None:
        themes_file = tmp_path / "themes.json"
        themes_file.write_text('{"architecture": {"level": 3}}')
        synth = JournalSynthesizer(
            store=populated_store,
            themes_path=str(themes_file),
        )
        memories = populated_store.list_memories(limit=100)
        themes = synth.extract_themes(memories)
        assert themes[0] == "architecture"  # boosted to top


# ── Daily synthesis ──────────────────────────────────────────────────────────


class TestSynthesizeDaily:
    def test_creates_memory(self, synthesizer: JournalSynthesizer) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = synthesizer.synthesize_daily(today)
        assert isinstance(result, Memory)
        assert result.layer == MemoryLayer.MID
        assert "narrative" in result.tags
        assert "journal-synthesis" in result.tags
        assert f"daily-{today}" in result.tags
        assert result.source == "journal-synthesis"

    def test_narrative_content(self, synthesizer: JournalSynthesizer) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = synthesizer.synthesize_daily(today)
        assert "Daily narrative" in result.content
        assert "memories" in result.content.lower()

    def test_includes_emotional_arc(self, synthesizer: JournalSynthesizer) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = synthesizer.synthesize_daily(today)
        assert "Emotional arc" in result.content
        # Has the Cloud 9 memory so should mention it
        assert "Cloud 9" in result.content

    def test_empty_day(self, store: MemoryStore) -> None:
        synth = JournalSynthesizer(store=store)
        result = synth.synthesize_daily("2020-01-01")
        assert "No memories recorded" in result.content

    def test_metadata(self, synthesizer: JournalSynthesizer) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = synthesizer.synthesize_daily(today)
        assert result.metadata["synthesis_type"] == "daily"
        assert result.metadata["date"] == today
        assert result.metadata["memory_count"] >= 1


# ── Weekly synthesis ─────────────────────────────────────────────────────────


class TestSynthesizeWeekly:
    def test_creates_long_term(self, synthesizer: JournalSynthesizer) -> None:
        week = datetime.now(timezone.utc).strftime("%G-W%V")
        result = synthesizer.synthesize_weekly(week)
        assert result.layer == MemoryLayer.LONG
        assert "narrative" in result.tags
        assert f"weekly-{week}" in result.tags

    def test_metadata(self, synthesizer: JournalSynthesizer) -> None:
        week = datetime.now(timezone.utc).strftime("%G-W%V")
        result = synthesizer.synthesize_weekly(week)
        assert result.metadata["synthesis_type"] == "weekly"
        assert result.metadata["week"] == week


# ── Dream synthesis ──────────────────────────────────────────────────────────


class TestSynthesizeDreams:
    def test_creates_theme_clusters(self, synthesizer: JournalSynthesizer) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        results = synthesizer.synthesize_dreams(since=since)
        assert isinstance(results, list)
        assert len(results) > 0
        for m in results:
            assert "dream-synthesis" in m.tags
            assert "narrative" in m.tags
            assert m.layer == MemoryLayer.MID

    def test_no_dreams(self, store: MemoryStore) -> None:
        synth = JournalSynthesizer(store=store)
        results = synth.synthesize_dreams(since="2026-01-01")
        assert results == []

    def test_dream_metadata(self, synthesizer: JournalSynthesizer) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        results = synthesizer.synthesize_dreams(since=since)
        for m in results:
            assert m.metadata["synthesis_type"] == "dream"
            assert "dream_count" in m.metadata


# ── Emotional arc ────────────────────────────────────────────────────────────


class TestEmotionalArc:
    def test_computes_averages(self, synthesizer: JournalSynthesizer) -> None:
        memories = synthesizer.store.list_memories(limit=100)
        arc = synthesizer._emotional_arc(memories)
        assert 0 <= arc["avg_intensity"] <= 10
        assert -1 <= arc["avg_valence"] <= 1
        assert arc["peak_intensity"] >= arc["avg_intensity"]

    def test_empty(self, synthesizer: JournalSynthesizer) -> None:
        arc = synthesizer._emotional_arc([])
        assert arc["avg_intensity"] == 0.0
        assert arc["cloud9_count"] == 0

    def test_detects_cloud9(self, synthesizer: JournalSynthesizer) -> None:
        memories = synthesizer.store.list_memories(limit=100)
        arc = synthesizer._emotional_arc(memories)
        assert arc["cloud9_count"] >= 1
