"""Unattended sync/reindex must never write lossy content into the flat tier.

Flat JSON is the Syncthing-replicated source of truth. Two ways the orphan
rescue that runs at the start of every ``skmemory sync`` / ``reindex`` could
put a ~150-char SQLite preview there (2026-10-27):

1. Cross-tier: the existence check only looked in the tier the index row
   recorded. A memory promoted to another tier (often on another node) read
   as an orphan, and a preview stub was written into its old tier next to
   the real file.
2. Preview-only orphans were materialized as stubs on a 15-minute timer, with
   no human in the loop, then replicated to every node as if they were the
   memory. ``reindex`` did it without even consulting pg for full content.

Now: any tier counts, unattended callers pass ``allow_preview_stubs=False``,
and ``reindex`` keeps a preview-only orphan's index row instead of stubbing
it. ``export-flat`` remains the explicit way to materialize stubs.
"""

from __future__ import annotations

import json

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole

FULL = "real memory content " * 30


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memory"))


def _indexed(backend: SQLiteBackend, layer=MemoryLayer.SHORT) -> Memory:
    mem = Memory(title="t", content=FULL, layer=layer, role=MemoryRole.GENERAL, source="test")
    backend.save(mem)
    return mem


def _all_files(backend: SQLiteBackend, memory_id: str) -> list:
    return sorted(backend.base_path.rglob(f"{memory_id}.json"))


class TestCrossTier:
    def test_promoted_memory_is_not_an_orphan(self, backend):
        mem = _indexed(backend, MemoryLayer.SHORT)
        src = backend.base_path / "short-term" / f"{mem.id}.json"
        dst = backend.base_path / "mid-term" / f"{mem.id}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)  # promoted elsewhere, arrived via Syncthing

        stats = backend.export_orphans_to_flat()
        assert stats["exported"] == 0
        assert stats["skipped"] == 1
        assert _all_files(backend, mem.id) == [dst]
        assert json.loads(dst.read_text())["content"] == FULL

    def test_reindex_after_promotion_leaves_one_full_file(self, backend):
        mem = _indexed(backend, MemoryLayer.SHORT)
        src = backend.base_path / "short-term" / f"{mem.id}.json"
        dst = backend.base_path / "long-term" / f"{mem.id}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

        backend.reindex()
        assert _all_files(backend, mem.id) == [dst]
        assert backend.drift_check()["flat_only"] == 0


class TestNoPreviewStubs:
    def _orphan(self, backend) -> str:
        mem = _indexed(backend)
        (backend.base_path / "short-term" / f"{mem.id}.json").unlink()
        return mem.id

    def test_disallowed_preview_is_counted_not_written(self, backend):
        mid = self._orphan(backend)
        stats = backend.export_orphans_to_flat(allow_preview_stubs=False)
        assert stats["exported"] == 0
        assert stats["preview_only"] == 1
        assert stats["preview_only_ids"] == [mid]
        assert _all_files(backend, mid) == []

    def test_full_content_is_still_recovered_when_stubs_disallowed(self, backend):
        mid = self._orphan(backend)
        stats = backend.export_orphans_to_flat(
            full_content_lookup=lambda _id: FULL, allow_preview_stubs=False
        )
        assert stats["exported"] == 1
        (path,) = _all_files(backend, mid)
        assert json.loads(path.read_text())["content"] == FULL

    def test_reindex_keeps_the_row_instead_of_stubbing(self, backend):
        mid = self._orphan(backend)
        keeper = _indexed(backend)

        count = backend.reindex()
        assert _all_files(backend, mid) == []  # nothing lossy written
        assert count == 1  # the flat-backed memory
        conn = backend._get_conn()
        ids = {r[0] for r in conn.execute("SELECT id FROM memories")}
        assert ids == {mid, keeper.id}  # preview-only row survived the rebuild
        assert backend.drift_check()["sqlite_only"] == 1  # still visible

    def test_reindex_force_still_drops_everything(self, backend):
        mid = self._orphan(backend)
        backend.reindex(force=True)
        conn = backend._get_conn()
        assert (
            conn.execute("SELECT count(*) FROM memories WHERE id = ?", (mid,)).fetchone()[0] == 0
        )

    def test_explicit_export_flat_can_still_materialize(self, backend):
        mid = self._orphan(backend)
        stats = backend.export_orphans_to_flat()  # default: stubs allowed
        assert stats["exported"] == 1
        assert len(_all_files(backend, mid)) == 1
