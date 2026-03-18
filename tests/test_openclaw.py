"""Tests for the OpenClaw integration module."""

from pathlib import Path

import pytest

from skmemory.openclaw import SKMemoryPlugin


@pytest.fixture
def plugin(tmp_path):
    """Create a plugin instance with a temporary memory store."""
    return SKMemoryPlugin(base_path=str(tmp_path / "memories"))


class TestPluginInit:
    """Plugin initialization."""

    def test_plugin_creates_store(self, plugin):
        """Plugin instantiation creates a working MemoryStore."""
        assert plugin.store is not None

    def test_health_reports_ok(self, plugin):
        """Health check returns healthy status."""
        health = plugin.health()
        assert health["primary"]["ok"] is True


class TestPluginSnapshot:
    """Memory capture via the plugin."""

    def test_snapshot_returns_id(self, plugin):
        """Snapshot returns a non-empty memory ID."""
        mid = plugin.snapshot("Test moment", content="Some content")
        assert mid
        assert isinstance(mid, str)

    def test_snapshot_with_tags(self, plugin):
        """Snapshot with tags is searchable."""
        plugin.snapshot("Tagged moment", tags=["cloud9", "love"])
        results = plugin.search("cloud9")
        assert len(results) >= 1

    def test_snapshot_with_emotion(self, plugin):
        """Snapshot stores emotional intensity."""
        mid = plugin.snapshot(
            "Intense moment",
            intensity=9.5,
            valence=0.8,
            emotions=["joy"],
        )
        recalled = plugin.recall(mid)
        assert recalled is not None
        assert recalled["emotional"]["intensity"] == 9.5


class TestPluginSearch:
    """Search via the plugin."""

    def test_search_by_title(self, plugin):
        """Search finds memories by title text."""
        plugin.snapshot("Penguin Kingdom launch")
        results = plugin.search("Penguin")
        assert len(results) >= 1

    def test_search_no_results(self, plugin):
        """Search returns empty for no matches."""
        results = plugin.search("zzzznotfound")
        assert len(results) == 0


class TestPluginRecall:
    """Recall by ID."""

    def test_recall_returns_full_data(self, plugin):
        """Recall returns a complete memory dict."""
        mid = plugin.snapshot("Recall test", content="Full content here")
        mem = plugin.recall(mid)

        assert mem is not None
        assert mem["title"] == "Recall test"
        assert mem["content"] == "Full content here"

    def test_recall_nonexistent(self, plugin):
        """Recall returns None for missing ID."""
        assert plugin.recall("nonexistent-id") is None


class TestPluginContext:
    """Token-efficient context loading."""

    def test_load_context_returns_dict(self, plugin):
        """Context loading returns a structured dict with tiered keys."""
        plugin.snapshot("Context test", intensity=8.0)
        ctx = plugin.load_context(max_tokens=1000)

        assert isinstance(ctx, dict)
        assert "today" in ctx
        assert "yesterday" in ctx
        assert "older_summary" in ctx
        assert "token_estimate" in ctx
        assert "token_budget" in ctx


class TestPluginExport:
    """Export/import via the plugin."""

    def test_export_creates_backup(self, plugin, tmp_path):
        """Export creates a JSON file."""
        plugin.snapshot("Export test")
        path = plugin.export(str(tmp_path / "backup.json"))
        assert Path(path).exists()

    def test_import_restores(self, plugin, tmp_path):
        """Import restores memories from a backup."""
        plugin.snapshot("Import test")
        backup = str(tmp_path / "backup.json")
        plugin.export(backup)

        fresh = SKMemoryPlugin(base_path=str(tmp_path / "fresh_memories"))
        count = fresh.import_backup(backup)
        assert count == 1
