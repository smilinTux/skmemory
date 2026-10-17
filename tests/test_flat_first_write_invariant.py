"""Regression tests for the flat-first write invariant (card: sqlite-only
orphans, 2026-10-16).

On 2026-10-16 Lumina's index.db on Chef's laptop held 23,525 memories with no
backing flat JSON file. Those rows never synced anywhere, because index.db is
a local, unsynced-per-node index while the flat files are what Syncthing
actually carries between nodes. Forensics on a pre-materialization backup of
that index showed every orphaned row already had the *correct* file_path
computed, so the bug was never "the wrong path got indexed" -- it was that
the flat write for that path was not durable: a plain ``Path.write_text``
only lands in the OS page cache, and an unclean shutdown (the exact failure
mode a laptop hits constantly: sleep, battery cutoff, forced reboot) can lose
that buffered write while the SQLite WAL commit for the same row survives.

The invariant this file locks in: **the flat file must be written durably
before the memory is indexed, and if the flat write fails, the memory must
not be indexed at all.** Every backend that writes a memory JSON file and
also maintains a SQLite index must honor this.
"""

from __future__ import annotations

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.backends.vaulted_backend import VaultedSQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole


def _mem(title: str = "Test moment") -> Memory:
    return Memory(
        title=title,
        content=f"Full content for {title} that must never be lost.",
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        source="test",
    )


def _row_exists(backend: SQLiteBackend, memory_id: str) -> bool:
    conn = backend._get_conn()
    row = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return row is not None


class TestSQLiteBackendFlatFirst:
    """SQLiteBackend.save(): no durable flat write => no index row."""

    def test_normal_save_writes_flat_file_before_indexing(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory = _mem()
        backend.save(memory)

        flat_path = backend._file_path(memory)
        assert flat_path.exists()
        assert _row_exists(backend, memory.id)

    def test_failed_flat_write_raises_and_does_not_index(self, tmp_path, monkeypatch):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory = _mem()

        monkeypatch.setattr(
            "skmemory.backends.sqlite_backend.atomic_write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("simulated disk failure")),
        )

        with pytest.raises(OSError):
            backend.save(memory)

        flat_path = backend._file_path(memory)
        assert not flat_path.exists()
        assert not _row_exists(backend, memory.id)

    def test_failed_flat_write_leaves_no_partial_json(self, tmp_path, monkeypatch):
        """A crash mid-write must never leave a truncated/corrupt flat file."""
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory = _mem()

        def boom(*a, **k):
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr("skmemory.backends.sqlite_backend.atomic_write_text", boom)

        with pytest.raises(OSError):
            backend.save(memory)

        layer_dir = tmp_path / "memories" / "short-term"
        leftovers = list(layer_dir.glob("*")) if layer_dir.exists() else []
        assert leftovers == []


class TestFileBackendFlatFirst:
    """FileBackend.save() must use the durable atomic writer too (it backs
    export_orphans_to_flat's recovery writes, and any use_sqlite=False
    deployment)."""

    def test_save_is_durable_and_atomic(self, tmp_path, monkeypatch):
        backend = FileBackend(base_path=str(tmp_path / "memories"))
        memory = _mem()

        def boom(*a, **k):
            raise OSError("simulated disk failure")

        monkeypatch.setattr("skmemory.backends.file_backend.atomic_write_text", boom)

        with pytest.raises(OSError):
            backend.save(memory)

        assert backend._find_file(memory.id) is None

    def test_normal_save_round_trips(self, tmp_path):
        backend = FileBackend(base_path=str(tmp_path / "memories"))
        memory = _mem()
        backend.save(memory)
        loaded = backend.load(memory.id)
        assert loaded is not None
        assert loaded.id == memory.id


class TestVaultedBackendFlatFirst:
    def test_failed_encrypted_write_does_not_index(self, tmp_path, monkeypatch):
        backend = VaultedSQLiteBackend(
            passphrase="test-pass-not-real", base_path=str(tmp_path / "memories")
        )
        memory = _mem()

        monkeypatch.setattr(
            "skmemory.backends.vaulted_backend.atomic_write_bytes",
            lambda *a, **k: (_ for _ in ()).throw(OSError("simulated disk failure")),
        )

        with pytest.raises(OSError):
            backend.save(memory)

        assert not _row_exists(backend, memory.id)


class TestImportBackupFlatFirst:
    """Bulk import must skip (not index) any memory whose flat write fails,
    while still importing the memories around it."""

    def test_one_failed_write_does_not_index_that_memory(self, tmp_path, monkeypatch):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        good_a = _mem("Good A")
        bad = _mem("Bad")
        good_b = _mem("Good B")

        backup_path = tmp_path / "backup.json"
        import json

        payload = {
            "memories": [
                good_a.model_dump(mode="json"),
                bad.model_dump(mode="json"),
                good_b.model_dump(mode="json"),
            ]
        }
        backup_path.write_text(json.dumps(payload, default=str))

        real_atomic_write_text = __import__(
            "skmemory.atomic_io", fromlist=["atomic_write_text"]
        ).atomic_write_text

        def flaky(path, text, *a, **k):
            if str(bad.id) in str(path):
                raise OSError("simulated disk failure")
            return real_atomic_write_text(path, text, *a, **k)

        monkeypatch.setattr("skmemory.backends.sqlite_backend.atomic_write_text", flaky)

        count = backend.import_backup(str(backup_path))

        assert count == 2
        assert _row_exists(backend, good_a.id)
        assert _row_exists(backend, good_b.id)
        assert not _row_exists(backend, bad.id)
        assert not backend._file_path(bad).exists()
