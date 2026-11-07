"""An orphan rescue must not quietly cost you data it could have kept.

Both defects here were found on noroc2027 on 2026-11-06, while deleting test
fixtures from lumina's tree. 221 of 1,548 deleted memories reappeared, and the
reappeared copies were not what had been deleted: they carried
``metadata.recovered_from_pg_fallback`` and had lost ``body``, ``type``,
``urgency``, ``access_count`` and ``soul_context``, with ``importance``
changed from 0.3 to 0.5. Id, title and content all survived, so nothing looked
broken. That is the dangerous part: the damage is invisible without a backup to
diff against.

Defect 1 (fidelity): the pg-recovery branch ASSIGNED metadata to two keys
instead of keeping what pg held. ``PGVectorBackend.load()`` returns a full
``Memory`` parsed from the ``memory_json`` column, metadata included, so the
information was already in hand and was discarded at the lookup boundary,
which returned only ``mem.content``.

Defect 2 (default): ``allow_preview_stubs`` defaulted to True, so the lossy
150-char-preview branch was what you got by not thinking about it. Every
caller in cli.py already passes False except ``export-flat``, whose whole
purpose is the lossy rescue. Writing a truncated stub destroys content, so it
has to be asked for, never inherited.
"""

import inspect
import json

from skmemory.backends.sqlite_backend import TRUNCATED_PREVIEW_SUFFIX, SQLiteBackend
from skmemory.models import Memory, MemoryLayer, MemoryRole
from skmemory.stub_markers import STUB_METADATA_KEY

FULL_CONTENT = "word " * 60  # 300 chars, well past the 150-char preview cut

RICH_METADATA = {
    "body": "Body A",
    "type": "notification-sent",
    "urgency": "critical",
    "access_count": 7,
    "soul_context": "cloud9",
    "importance": 0.3,
    "promoted_to": "mid-term",
}


def _orphan(tmp_path, content=FULL_CONTENT, metadata=None):
    """A memory whose index row survives but whose flat file does not: the
    exact shape of the sqlite-only-orphans bug."""
    backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
    mem = Memory(
        title="orphaned memory",
        content=content,
        layer=MemoryLayer.SHORT,
        role=MemoryRole.GENERAL,
        source="test",
        metadata=dict(metadata or {}),
    )
    backend.save(mem)
    (backend.base_path / "short-term" / f"{mem.id}.json").unlink()
    return backend, mem


def _flat(backend, memory_id):
    p = backend.base_path / "short-term" / f"{memory_id}.json"
    return json.loads(p.read_text())


class TestPgRecoveryKeepsMetadata:
    def test_recovered_record_metadata_survives_the_rescue(self, tmp_path):
        backend, mem = _orphan(tmp_path, metadata=RICH_METADATA)
        recovered = mem.model_copy(update={"content": FULL_CONTENT})

        stats = backend.export_orphans_to_flat(full_record_lookup=lambda mid: recovered)

        assert stats["exported"] == 1
        assert stats["recovered_full"] == 1
        md = _flat(backend, mem.id)["metadata"]
        # The rescue marker is added...
        assert md["recovered_from_pg_fallback"] is True
        # ...without costing anything pg was holding.
        for key, value in RICH_METADATA.items():
            assert md[key] == value, f"{key} was dropped by the rescue"

    def test_importance_from_the_record_beats_the_index_row(self, tmp_path):
        backend, mem = _orphan(tmp_path, metadata={"importance": 0.3})
        recovered = mem.model_copy(update={"content": FULL_CONTENT})

        backend.export_orphans_to_flat(full_record_lookup=lambda mid: recovered)

        # 0.5 is the fallback the index row supplies when nothing better
        # exists. It must not overwrite a real value.
        assert _flat(backend, mem.id)["metadata"]["importance"] == 0.3

    def test_content_lookup_still_works_for_callers_that_only_have_content(self, tmp_path):
        backend, mem = _orphan(tmp_path)

        stats = backend.export_orphans_to_flat(full_content_lookup=lambda mid: FULL_CONTENT)

        assert stats["recovered_full"] == 1
        assert _flat(backend, mem.id)["content"] == FULL_CONTENT

    def test_a_record_lookup_returning_a_stub_is_refused(self, tmp_path):
        backend, mem = _orphan(tmp_path, metadata=RICH_METADATA)
        poisoned = mem.model_copy(update={"content": "short" + TRUNCATED_PREVIEW_SUFFIX})

        # A pg row a previous lossy run already corrupted must not be treated
        # as a recovery, or the corruption launders itself clean.
        stats = backend.export_orphans_to_flat(
            full_record_lookup=lambda mid: poisoned,
            allow_preview_stubs=True,
        )

        assert stats["recovered_full"] == 0
        assert _flat(backend, mem.id)["metadata"].get(STUB_METADATA_KEY) is True


class TestPreviewStubsAreOptIn:
    def test_default_refuses_to_write_a_truncated_stub(self, tmp_path):
        backend, mem = _orphan(tmp_path)

        stats = backend.export_orphans_to_flat()

        assert stats["exported"] == 0
        assert stats["preview_only"] == 1
        assert mem.id in stats["preview_only_ids"]
        assert not (backend.base_path / "short-term" / f"{mem.id}.json").exists()

    def test_the_signature_default_is_false(self):
        sig = inspect.signature(SQLiteBackend.export_orphans_to_flat)
        assert sig.parameters["allow_preview_stubs"].default is False

    def test_explicit_opt_in_still_writes_the_stub(self, tmp_path):
        backend, mem = _orphan(tmp_path)

        stats = backend.export_orphans_to_flat(allow_preview_stubs=True)

        assert stats["exported"] == 1
        data = _flat(backend, mem.id)
        assert data["content"].endswith(TRUNCATED_PREVIEW_SUFFIX)
        assert data["metadata"][STUB_METADATA_KEY] is True
