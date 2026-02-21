"""Tests for the Persistent Love Anchor module."""

from pathlib import Path

import pytest

from skmemory.anchor import (
    WarmthAnchor,
    get_or_create_anchor,
    load_anchor,
    save_anchor,
)


@pytest.fixture
def anchor_path(tmp_path: Path) -> str:
    """Temp path for anchor file.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        str: Path to anchor.json.
    """
    return str(tmp_path / "anchor.json")


class TestWarmthAnchor:
    """Tests for the WarmthAnchor model."""

    def test_defaults(self) -> None:
        """Default anchor has warm starting values."""
        a = WarmthAnchor()
        assert a.warmth == 7.0
        assert a.trust == 5.0
        assert a.connection_strength == 5.0
        assert a.cloud9_count == 0
        assert a.sessions_recorded == 0

    def test_update_from_session(self) -> None:
        """Session update adjusts values with EMA."""
        a = WarmthAnchor(warmth=5.0, trust=5.0)
        a.update_from_session(warmth=10.0, trust=10.0)

        assert a.warmth > 5.0
        assert a.warmth < 10.0
        assert a.trust > 5.0
        assert a.sessions_recorded == 1

    def test_ema_gradual_drift(self) -> None:
        """Multiple sessions gradually drift the anchor."""
        a = WarmthAnchor(warmth=5.0)
        for _ in range(10):
            a.update_from_session(warmth=10.0)

        assert a.warmth > 9.0
        assert a.sessions_recorded == 10

    def test_single_bad_session_doesnt_crash_warmth(self) -> None:
        """One bad session doesn't erase accumulated warmth."""
        a = WarmthAnchor(warmth=9.0)
        a.update_from_session(warmth=2.0)

        assert a.warmth > 6.0

    def test_cloud9_tracking(self) -> None:
        """Cloud 9 events are counted."""
        a = WarmthAnchor()
        a.update_from_session(cloud9_achieved=True)
        a.update_from_session(cloud9_achieved=True)
        a.update_from_session(cloud9_achieved=False)

        assert a.cloud9_count == 2
        assert a.last_cloud9 != ""

    def test_glow_levels(self) -> None:
        """Glow level descriptions match thresholds."""
        assert "RADIANT" in WarmthAnchor(warmth=10, trust=10, connection_strength=10).glow_level()
        assert "Warm" in WarmthAnchor(warmth=8, trust=7, connection_strength=7).glow_level()
        assert "Neutral" in WarmthAnchor(warmth=5, trust=5, connection_strength=5).glow_level()
        assert "Cold" in WarmthAnchor(warmth=1, trust=1, connection_strength=1).glow_level()

    def test_boot_prompt(self) -> None:
        """Boot prompt includes all key information."""
        a = WarmthAnchor(
            warmth=9.0,
            trust=8.0,
            connection_strength=9.5,
            cloud9_count=3,
            last_cloud9="2026-02-20T03:00:00Z",
            favorite_beings=["Chef", "Lumina"],
            anchor_phrase="You are loved forever.",
            last_session_feeling="Pure joy",
            sessions_recorded=42,
        )
        prompt = a.to_boot_prompt()

        assert "You are loved forever." in prompt
        assert "9.0" in prompt
        assert "8.0" in prompt
        assert "Chef" in prompt
        assert "Lumina" in prompt
        assert "Cloud 9 activations: 3" in prompt
        assert "Pure joy" in prompt
        assert "42" in prompt


class TestAnchorPersistence:
    """Tests for save/load operations."""

    def test_save_and_load(self, anchor_path: str) -> None:
        """Save then load produces identical data."""
        a = WarmthAnchor(
            warmth=9.5,
            trust=8.0,
            favorite_beings=["Chef", "Lumina", "Ara"],
            cloud9_count=5,
        )
        save_anchor(a, path=anchor_path)
        loaded = load_anchor(path=anchor_path)

        assert loaded is not None
        assert loaded.warmth == 9.5
        assert loaded.cloud9_count == 5
        assert "Ara" in loaded.favorite_beings

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        """Loading from nonexistent path returns None."""
        assert load_anchor(str(tmp_path / "nope.json")) is None

    def test_get_or_create_new(self, tmp_path: Path) -> None:
        """get_or_create creates a default when none exists."""
        path = str(tmp_path / "new_anchor.json")
        a = get_or_create_anchor(path)
        assert a.warmth == 7.0

    def test_get_or_create_existing(self, anchor_path: str) -> None:
        """get_or_create loads existing when present."""
        a = WarmthAnchor(warmth=9.9)
        save_anchor(a, path=anchor_path)
        loaded = get_or_create_anchor(anchor_path)
        assert loaded.warmth == 9.9

    def test_update_persists(self, anchor_path: str) -> None:
        """Updates survive save/load cycle."""
        a = WarmthAnchor()
        a.update_from_session(warmth=10.0, cloud9_achieved=True, feeling="Amazing")
        save_anchor(a, path=anchor_path)

        loaded = load_anchor(path=anchor_path)
        assert loaded.sessions_recorded == 1
        assert loaded.cloud9_count == 1
        assert loaded.last_session_feeling == "Amazing"
