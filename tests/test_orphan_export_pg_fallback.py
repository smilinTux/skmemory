"""export_orphans_to_flat() must try a full-content fallback before writing
a lossy ~150-char SQLite preview stub.

Card: skmemory sqlite-only-orphans (2026-10-16), recovery follow-up. The
laptop's 23,525 SQLite-only orphans were materialized with only their
content_preview, discarding the real content even though .158's skmem-pg
(the configured vector/pg backend) still held it in full for many of them.
export_orphans_to_flat() had no way to consult that backend at all.

This closes the gap: an optional ``full_content_lookup`` callable is tried
FIRST for each orphan id. Only when it is absent, raises, returns nothing,
or itself only holds a previously-truncated stub (a pg row a prior reconcile
run already corrupted from the same lossy preview) does the method fall
back to the old lossy-preview behavior -- and that fallback path must now
log a WARNING per id, so a silent full-fleet loss is never invisible again.
"""

import logging

import pytest

from skmemory.backends.sqlite_backend import TRUNCATED_PREVIEW_SUFFIX, SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


def _make_orphan(backend: SQLiteBackend, content: str) -> str:
    """Save a memory, then delete its flat file to simulate a SQLite-only
    orphan (index row survives, flat file does not -- the exact shape of
    the sqlite-only-orphans bug)."""
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


def _load_flat(backend: SQLiteBackend, memory_id: str) -> dict:
    import json

    path = backend.base_path / "short-term" / f"{memory_id}.json"
    return json.loads(path.read_text())


FULL_CONTENT = "word " * 60  # 300 chars, well past the 150-char preview cut


class TestFullContentFallbackRecovers:
    def test_lookup_hit_writes_full_content_not_preview(self, backend):
        memory_id = _make_orphan(backend, FULL_CONTENT)

        def lookup(mid):
            assert mid == memory_id
            return FULL_CONTENT

        stats = backend.export_orphans_to_flat(full_content_lookup=lookup)

        assert stats["exported"] == 1
        assert stats["recovered_full"] == 1
        data = _load_flat(backend, memory_id)
        assert data["content"] == FULL_CONTENT
        assert not data["content"].endswith(TRUNCATED_PREVIEW_SUFFIX)
        assert "recovered_from_sqlite_preview" not in data["metadata"]
        assert data["metadata"]["recovered_from_pg_fallback"] is True

    def test_lookup_hit_shorter_than_preview_is_rejected(self, backend):
        """A lookup result shorter than the SQLite preview itself cannot be
        the full content (the preview is a prefix of the real thing) --
        treat it as untrustworthy and fall back."""
        memory_id = _make_orphan(backend, FULL_CONTENT)

        stats = backend.export_orphans_to_flat(full_content_lookup=lambda mid: "x")

        assert stats["recovered_full"] == 0
        data = _load_flat(backend, memory_id)
        assert data["content"].endswith(TRUNCATED_PREVIEW_SUFFIX)
        assert data["metadata"]["recovered_from_sqlite_preview"] is True


class TestFallbackWhenLookupUnavailable:
    def test_no_lookup_behaves_as_before(self, backend):
        memory_id = _make_orphan(backend, FULL_CONTENT)

        stats = backend.export_orphans_to_flat()

        assert stats["exported"] == 1
        assert stats["recovered_full"] == 0
        data = _load_flat(backend, memory_id)
        assert data["content"].endswith(TRUNCATED_PREVIEW_SUFFIX)
        assert data["metadata"]["recovered_from_sqlite_preview"] is True

    def test_lookup_returning_none_falls_back_and_warns(self, backend, caplog):
        memory_id = _make_orphan(backend, FULL_CONTENT)

        with caplog.at_level(logging.WARNING, logger="skmemory.sqlite_backend"):
            stats = backend.export_orphans_to_flat(full_content_lookup=lambda mid: None)

        assert stats["recovered_full"] == 0
        data = _load_flat(backend, memory_id)
        assert data["metadata"]["recovered_from_sqlite_preview"] is True
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(memory_id in r.getMessage() for r in warnings), (
            "expected a WARNING naming the orphan id when falling back to the lossy preview"
        )

    def test_lookup_raising_falls_back_and_warns_without_crashing(self, backend, caplog):
        memory_id = _make_orphan(backend, FULL_CONTENT)

        def boom(mid):
            raise ConnectionError("pg unreachable")

        with caplog.at_level(logging.WARNING, logger="skmemory.sqlite_backend"):
            stats = backend.export_orphans_to_flat(full_content_lookup=boom)

        assert stats["errors"] == 0
        assert stats["recovered_full"] == 0
        data = _load_flat(backend, memory_id)
        assert data["metadata"]["recovered_from_sqlite_preview"] is True
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an unreachable fallback source must still log loudly, not crash silently"


class TestAlreadyCorruptedPgRowIsNotTrustedAsFull:
    def test_lookup_returning_a_prior_stub_is_rejected_not_laundered(self, backend, caplog):
        """Reproduces the 2026-10-16 recovery finding: a reconcile run that
        ran before the safety pause can have already copied the SAME
        truncated stub into the pg row. That stub starts with the real
        preview text and is even longer than it (suffix appended), so a
        naive prefix/length check would wrongly call it 'recovered'. It
        must be detected (it ends with the known loss marker) and rejected,
        falling back to the ordinary lossy-preview path with a warning --
        never silently re-stamped as though it were genuinely recovered.
        """
        memory_id = _make_orphan(backend, FULL_CONTENT)
        already_corrupted_pg_value = FULL_CONTENT[:150] + TRUNCATED_PREVIEW_SUFFIX

        with caplog.at_level(logging.WARNING, logger="skmemory.sqlite_backend"):
            stats = backend.export_orphans_to_flat(
                full_content_lookup=lambda mid: already_corrupted_pg_value
            )

        assert stats["recovered_full"] == 0
        data = _load_flat(backend, memory_id)
        assert data["metadata"]["recovered_from_sqlite_preview"] is True
        assert data["content"].endswith(TRUNCATED_PREVIEW_SUFFIX)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(memory_id in r.getMessage() for r in warnings)
