"""A deliberately forgotten memory must stay forgotten, fleet-wide.

`MemoryStore.forget_cascade()` writes a tombstone and says why in its own
docstring: "Records a durable tombstone (see :mod:`skmemory.tombstones`) so a
later reconcile refuses to resurrect the memory from a stale flat copy."
`export_orphans_to_flat()` never read one. It already has the identical guard
for archived memories (`_archived_stems`), so the shape was there; only the
tombstone check was missing.

Found on noroc2027, 2026-11-06, deleting 7,675 test fixtures from lumina's
tree. 2,387 of them came back. The loop is a one-node delete in a replicated
store, and it sustains itself:

  1. The flat file is deleted on node A. The flat store is the Syncthing
     source of truth, so the deletion propagates to node B.
  2. Node B's own index.db still holds the row, so the memory is now an
     ORPHAN on B (present in the index, no flat file). Verified on the .41
     laptop: all 40 probe ids still in its index.db, 18 of 40 flat files
     already gone.
  3. B's next sync runs this method, finds the content in B's own local
     skmem-pg, and writes the flat file back.
  4. That file syncs to A, which is where it looks like a resurrection.

Deleting from one node cannot win that race, and no amount of care on the
deleting node helps, because the re-export happens somewhere else. The
tombstones live under the memory dir, so they replicate the same way the
memories do: honouring them here is what makes a delete hold everywhere.

This is also the general form of the 2026-10-16 SEV-1, where one laptop
materialized 23,525 lossy stubs locally and Syncthing carried them fleet-wide.
"""

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole
from skmemory.tombstones import write_tombstone

FULL_CONTENT = "word " * 60


def _orphan(backend: SQLiteBackend, content: str = FULL_CONTENT) -> str:
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


class TestTombstonedOrphansAreNotResurrected:
    def test_tombstoned_orphan_is_not_rewritten_from_pg(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory_id = _orphan(backend)
        recovered = Memory(
            id=memory_id,
            title="orphaned memory",
            content=FULL_CONTENT,
            layer=MemoryLayer.SHORT,
            role=MemoryRole.GENERAL,
            source="test",
        )
        write_tombstone(backend.base_path, memory_id, reason="forget")

        stats = backend.export_orphans_to_flat(full_record_lookup=lambda mid: recovered)

        assert stats["exported"] == 0
        assert stats["tombstoned"] == 1
        assert not (backend.base_path / "short-term" / f"{memory_id}.json").exists()

    def test_tombstoned_orphan_is_not_written_as_a_stub_either(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory_id = _orphan(backend)
        write_tombstone(backend.base_path, memory_id, reason="forget")

        # Even the explicitly opt-in lossy path must not bring it back.
        stats = backend.export_orphans_to_flat(allow_preview_stubs=True)

        assert stats["exported"] == 0
        assert stats["tombstoned"] == 1
        assert not (backend.base_path / "short-term" / f"{memory_id}.json").exists()

    def test_an_untombstoned_orphan_is_still_recovered(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        keep = _orphan(backend)
        forgotten = _orphan(backend)
        write_tombstone(backend.base_path, forgotten, reason="forget")

        stats = backend.export_orphans_to_flat(allow_preview_stubs=True)

        # The guard must be surgical: one id was forgotten, the other was not.
        assert stats["tombstoned"] == 1
        assert stats["exported"] == 1
        assert (backend.base_path / "short-term" / f"{keep}.json").exists()
        assert not (backend.base_path / "short-term" / f"{forgotten}.json").exists()

    def test_shortform_tombstone_also_blocks_the_export(self, tmp_path):
        """Flat files are written under a 12-char shortform stem in places, and
        tombstones are keyed by file stem, so a shortform tombstone has to
        block the full id too or the guard is bypassable by naming."""
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        memory_id = _orphan(backend)
        short = memory_id[:12].replace("-", "")
        write_tombstone(backend.base_path, short, reason="forget")

        stats = backend.export_orphans_to_flat(allow_preview_stubs=True)

        assert stats["exported"] == 0
        assert stats["tombstoned"] == 1

    def test_no_tombstone_dir_is_not_an_error(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        _orphan(backend)

        stats = backend.export_orphans_to_flat(allow_preview_stubs=True)

        assert stats["exported"] == 1
        assert stats["tombstoned"] == 0
