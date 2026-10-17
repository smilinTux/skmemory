"""MemoryStore.export_orphans_to_flat() must wire the configured vector/pg
backend in as the full-content fallback source (see
test_orphan_export_pg_fallback.py for the SQLiteBackend-level behavior this
builds on)."""

from unittest.mock import MagicMock

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole
from skmemory.store import MemoryStore

FULL_CONTENT = "word " * 60


def _make_orphan(backend: SQLiteBackend, content: str) -> str:
    mem = Memory(
        title="orphaned memory",
        content=content,
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        source="test",
    )
    backend.save(mem)
    (backend.base_path / "short-term" / f"{mem.id}.json").unlink()
    return mem.id


@pytest.fixture
def sqlite_backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


class TestStoreWiresVectorBackendAsFallback:
    def test_vector_backend_load_used_as_fallback(self, sqlite_backend):
        memory_id = _make_orphan(sqlite_backend, FULL_CONTENT)

        fake_vector = MagicMock()
        fake_vector.load.return_value = Memory(
            id=memory_id,
            title="orphaned memory",
            content=FULL_CONTENT,
            layer=MemoryLayer.SHORT,
            role=MemoryRole.GENERAL,
            source="test",
        )

        store = MemoryStore(primary=sqlite_backend, vector=fake_vector)
        stats = store.export_orphans_to_flat()

        fake_vector.load.assert_called_with(memory_id)
        assert stats["recovered_full"] == 1
        data = sqlite_backend._find_file(memory_id)
        assert data is not None

    def test_no_vector_backend_degrades_to_old_behavior(self, sqlite_backend):
        memory_id = _make_orphan(sqlite_backend, FULL_CONTENT)

        store = MemoryStore(primary=sqlite_backend, vector=None)
        stats = store.export_orphans_to_flat()

        assert stats["exported"] == 1
        assert stats["recovered_full"] == 0
        assert memory_id in stats["orphan_ids"]

    def test_vector_backend_raising_does_not_abort_export(self, sqlite_backend):
        _make_orphan(sqlite_backend, FULL_CONTENT)

        fake_vector = MagicMock()
        fake_vector.load.side_effect = ConnectionError("pg unreachable")

        store = MemoryStore(primary=sqlite_backend, vector=fake_vector)
        stats = store.export_orphans_to_flat()

        assert stats["errors"] == 0
        assert stats["exported"] == 1
        assert stats["recovered_full"] == 0

    def test_non_sqlite_primary_returns_zeroed_stats_with_recovered_full_key(self):
        from skmemory.backends.file_backend import FileBackend

        store = MemoryStore(primary=FileBackend(base_path="/tmp/does-not-matter"), vector=None)
        stats = store.export_orphans_to_flat()

        assert stats == {
            "exported": 0,
            "skipped": 0,
            "errors": 0,
            "recovered_full": 0,
            "orphan_ids": [],
        }
