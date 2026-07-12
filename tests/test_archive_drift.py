"""Archive-awareness of the SQLite drift/orphan machinery.

Regression tests for the "skmemory drift" incident (2026-07-12): the memory
promoter archives old/duplicate memories by moving their flat file into
``memory/archive/`` (and ``archive/deduped/``) but leaves the SQLite index row
in place. drift_check() then counted those rows as ``sqlite_only`` orphans,
producing permanent false-DRIFT reports, and export_orphans_to_flat() would
resurrect them into the active tiers as truncated "content lost" stubs.

Archived memories are intentional cold storage — NOT drift, and NOT orphans.
"""

import json

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


def _mem(title: str) -> Memory:
    return Memory(
        title=title,
        content=f"Full content for {title} that must never be truncated.",
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        source="test",
    )


def _archive(backend: SQLiteBackend, memory_id: str, subdir: str = "archive") -> None:
    """Simulate the promoter: move the flat file out of its tier into archive/.

    Mirrors skcapstone.memory_promoter.archive_old_memories /_archive_deduped,
    which shutil.move the file but (historically) left the index.db row behind.
    """
    src = backend.base_path / "short-term" / f"{memory_id}.json"
    dest_dir = backend.base_path / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    src.rename(dest_dir / src.name)


class TestDriftCheckArchiveAware:
    def test_archived_memory_is_not_drift(self, backend):
        """A row whose flat file moved to archive/ is archived, not sqlite_only."""
        m = _mem("archived-one")
        backend.save(m)
        _archive(backend, m.id)

        drift = backend.drift_check()

        assert drift["sqlite_only"] == 0, "archived memory wrongly counted as orphan"
        assert drift["archived"] == 1
        assert drift["in_sync"] is True

    def test_deduped_archive_is_not_drift(self, backend):
        """archive/deduped/ (the dedup sink) is also recognized as archived."""
        m = _mem("deduped-one")
        backend.save(m)
        _archive(backend, m.id, subdir="archive/deduped")

        drift = backend.drift_check()

        assert drift["sqlite_only"] == 0
        assert drift["archived"] == 1
        assert drift["in_sync"] is True

    def test_truly_missing_still_counts_as_orphan(self, backend):
        """A row with no flat file anywhere (not even archive) is still drift."""
        m = _mem("gone")
        backend.save(m)
        (backend.base_path / "short-term" / f"{m.id}.json").unlink()

        drift = backend.drift_check()

        assert drift["sqlite_only"] == 1
        assert drift["archived"] == 0
        assert drift["in_sync"] is False


class TestPruneArchived:
    def test_prunes_only_archived_rows(self, backend):
        """prune_archived removes archived stale rows, keeps live + orphaned."""
        live = _mem("live")
        archived = _mem("to-archive")
        gone = _mem("gone")
        backend.save(live)
        backend.save(archived)
        backend.save(gone)
        _archive(backend, archived.id)
        (backend.base_path / "short-term" / f"{gone.id}.json").unlink()

        removed = backend.prune_archived()

        assert removed == 1
        drift = backend.drift_check()
        assert drift["archived"] == 0  # archived row now gone from index
        assert drift["sqlite_only"] == 1  # the truly-gone one survives
        # live memory still indexed
        assert backend.load(live.id) is not None

    def test_noop_when_no_archive(self, backend):
        """No archive dir → nothing to prune."""
        backend.save(_mem("only-live"))
        assert backend.prune_archived() == 0


class TestExportOrphansSkipsArchived:
    def test_archived_memory_not_resurrected(self, backend):
        """export_orphans_to_flat must not re-create an archived memory in a tier."""
        m = _mem("archived-two")
        backend.save(m)
        _archive(backend, m.id)

        stats = backend.export_orphans_to_flat()

        tier_file = backend.base_path / "short-term" / f"{m.id}.json"
        assert not tier_file.exists(), "archived memory was resurrected into a tier"
        assert m.id not in stats["orphan_ids"]

    def test_archived_content_never_truncated(self, backend):
        """The archived flat file keeps its full content (no truncated stub)."""
        m = _mem("archived-three")
        backend.save(m)
        _archive(backend, m.id)

        backend.export_orphans_to_flat()

        archived_file = backend.base_path / "archive" / f"{m.id}.json"
        data = json.loads(archived_file.read_text())
        assert "content lost" not in data["content"]
        assert data["content"] == m.content
