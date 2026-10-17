"""`skmemory health` / MemoryStore.health() must surface drift loudly.

Card: sqlite-only orphans (2026-10-16). 23,525 SQLite-only rows sat
undetected on a laptop for months because nothing turned the drift into a
non-green health signal. drift_check() already counted sqlite_only/flat_only;
this locks in that MemoryStore.health() promotes that into an explicit
top-level ``status`` that goes DEGRADED (or RED, for a pg-only sibling issue)
instead of silently reporting ``ok: true``.
"""

from __future__ import annotations

import json

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole
from skmemory.store import MemoryStore


def _mem(title: str = "Test moment") -> Memory:
    return Memory(
        title=title,
        content=f"Full content for {title}.",
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        source="test",
    )


def _insert_orphan_row(backend: SQLiteBackend, memory: Memory) -> None:
    """Simulate a historical SQLite-only orphan: index row, no flat file."""
    fake_path = backend.base_path / memory.layer.value / f"{memory.id}.json"
    backend._index_memory(memory, fake_path)
    assert not fake_path.exists()


class TestHealthStatusClean:
    def test_in_sync_store_reports_ok(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        status = store.health()
        assert status["status"] == "ok"
        assert status["sqlite_only"] == 0


class TestHealthStatusDegraded:
    def test_sqlite_only_orphan_degrades_status(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        orphan = _mem("Orphan")
        _insert_orphan_row(backend, orphan)

        status = store.health()
        assert status["status"] == "degraded"
        assert status["sqlite_only"] == 1

    def test_health_json_is_serializable(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        _insert_orphan_row(backend, _mem("Orphan"))
        # Must not raise -- this is exactly what `skmemory health` does.
        json.dumps(store.health())
