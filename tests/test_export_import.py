"""Tests for export/import (daily JSON backup) functionality."""

import json
from pathlib import Path

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.store import MemoryStore


@pytest.fixture
def backend(tmp_path):
    """Create a SQLiteBackend with a temporary directory."""
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def store(backend):
    """Create a MemoryStore wrapping the temp backend."""
    return MemoryStore(primary=backend)


@pytest.fixture
def populated_store(store):
    """Store with a few memories already saved."""
    for i in range(5):
        store.snapshot(
            title=f"Memory {i}",
            content=f"Content for memory {i}",
            layer=MemoryLayer.SHORT if i % 2 == 0 else MemoryLayer.LONG,
            tags=["export-test"],
            emotional=EmotionalSnapshot(intensity=float(i)),
        )
    return store


class TestExport:
    """Exporting memories to JSON."""

    def test_export_creates_file(self, populated_store, tmp_path):
        """Export writes a JSON file to disk."""
        out = str(tmp_path / "backup.json")
        path = populated_store.export_backup(out)

        assert Path(path).exists()

    def test_export_contains_all_memories(self, populated_store, tmp_path):
        """Backup file contains every memory."""
        out = str(tmp_path / "backup.json")
        populated_store.export_backup(out)

        data = json.loads(Path(out).read_text())
        assert data["memory_count"] == 5
        assert len(data["memories"]) == 5

    def test_export_default_path(self, populated_store):
        """Without an explicit path, uses ~/.skmemory/backups/."""
        path = populated_store.export_backup()
        assert "backups" in path
        assert "skmemory-backup-" in path
        assert Path(path).exists()

    def test_export_includes_metadata(self, populated_store, tmp_path):
        """Backup includes version and timestamp."""
        out = str(tmp_path / "backup.json")
        populated_store.export_backup(out)

        data = json.loads(Path(out).read_text())
        assert "skmemory_version" in data
        assert "exported_at" in data

    def test_export_overwrites_same_day(self, populated_store, tmp_path):
        """Exporting twice the same day to the same path overwrites."""
        out = str(tmp_path / "backup.json")
        populated_store.export_backup(out)

        populated_store.snapshot(
            title="Extra memory",
            content="Added after first export",
        )
        populated_store.export_backup(out)

        data = json.loads(Path(out).read_text())
        assert data["memory_count"] == 6


class TestImport:
    """Restoring memories from a backup."""

    def _create_backup(self, store, path):
        """Helper: export to a known path."""
        return store.export_backup(str(path))

    def test_import_restores_memories(self, populated_store, tmp_path):
        """Import restores all memories from a backup."""
        backup = tmp_path / "backup.json"
        self._create_backup(populated_store, backup)

        fresh_backend = SQLiteBackend(
            base_path=str(tmp_path / "fresh_memories")
        )
        fresh_store = MemoryStore(primary=fresh_backend)

        count = fresh_store.import_backup(str(backup))
        assert count == 5

    def test_import_memories_are_loadable(self, populated_store, tmp_path):
        """Imported memories can be loaded by ID."""
        backup = tmp_path / "backup.json"
        self._create_backup(populated_store, backup)

        data = json.loads(backup.read_text())
        first_id = data["memories"][0]["id"]

        fresh_backend = SQLiteBackend(
            base_path=str(tmp_path / "fresh_memories")
        )
        fresh_store = MemoryStore(primary=fresh_backend)
        fresh_store.import_backup(str(backup))

        mem = fresh_store.recall(first_id)
        assert mem is not None
        assert mem.id == first_id

    def test_import_nonexistent_file(self, store):
        """Import raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            store.import_backup("/nonexistent/path.json")

    def test_import_invalid_file(self, store, tmp_path):
        """Import raises ValueError for malformed backup."""
        bad = tmp_path / "bad.json"
        bad.write_text('{"not_memories": true}')

        with pytest.raises(ValueError):
            store.import_backup(str(bad))

    def test_import_overwrites_existing(self, populated_store, tmp_path):
        """Import overwrites memories with the same ID."""
        backup = tmp_path / "backup.json"
        self._create_backup(populated_store, backup)

        count = populated_store.import_backup(str(backup))
        assert count == 5
        assert populated_store.primary.stats()["total"] == 5


class TestRoundTrip:
    """Full export -> fresh store -> import cycle."""

    def test_full_round_trip(self, populated_store, tmp_path):
        """Export + import on a fresh store yields identical data."""
        backup = tmp_path / "backup.json"
        populated_store.export_backup(str(backup))

        fresh_backend = SQLiteBackend(
            base_path=str(tmp_path / "fresh")
        )
        fresh_store = MemoryStore(primary=fresh_backend)
        count = fresh_store.import_backup(str(backup))
        assert count == 5

        original = populated_store.primary.list_summaries(limit=100)
        restored = fresh_store.primary.list_summaries(limit=100)

        orig_ids = {m["id"] for m in original}
        rest_ids = {m["id"] for m in restored}
        assert orig_ids == rest_ids
