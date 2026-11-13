"""The save path re-seals an API edit of a sealed field, and nothing else.

A writer that loads a sealed memory, changes title/content/emotional and calls
``backend.save`` is a legitimate writer: the seal it carries is the verified one
from disk, and it simply forgot to refresh it. Save keeps the seal true so such a
writer can never produce a false "tampered" (2026-11-07..13 on lumina).

What must stay detectable: an edit made to the file itself (Syncthing, an editor,
a script), a memory whose on-disk predecessor already fails, an unsealed memory,
and a replacement that arrives carrying a different hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer


@pytest.fixture(params=["sqlite", "file"])
def backend(request, tmp_path: Path):
    cls = SQLiteBackend if request.param == "sqlite" else FileBackend
    return cls(base_path=tmp_path / "memories")


def _sealed(backend, title="title", content="original body text") -> Memory:
    m = Memory(
        title=title,
        content=content,
        layer=MemoryLayer.MID,
        emotional=EmotionalSnapshot(intensity=3.0, valence=0.4, labels=["joy"]),
    )
    m.seal()
    backend.save(m)
    return m


def _stored(backend, memory_id: str) -> Memory:
    loaded = backend.load(memory_id)
    assert loaded is not None
    return loaded


class TestApiEditsStayVerifiable:
    def test_content_edit_is_resealed(self, backend):
        m = _sealed(backend)
        edited = _stored(backend, m.id)
        edited.content = "edited by a legitimate writer"
        backend.save(edited)
        assert _stored(backend, m.id).verify_integrity()
        assert _stored(backend, m.id).content == "edited by a legitimate writer"

    def test_title_edit_is_resealed(self, backend):
        m = _sealed(backend)
        edited = _stored(backend, m.id)
        edited.title = "a new title"
        backend.save(edited)
        assert _stored(backend, m.id).verify_integrity()

    def test_emotional_edit_is_resealed(self, backend):
        m = _sealed(backend)
        edited = _stored(backend, m.id)
        edited.emotional.valence = -0.5
        backend.save(edited)
        assert _stored(backend, m.id).verify_integrity()

    def test_unrelated_edit_keeps_the_same_hash(self, backend):
        m = _sealed(backend)
        before = _stored(backend, m.id).integrity_hash
        edited = _stored(backend, m.id)
        edited.tags = ["x"]
        backend.save(edited)
        assert _stored(backend, m.id).integrity_hash == before

    def test_a_reseal_is_logged(self, backend, caplog):
        m = _sealed(backend)
        edited = _stored(backend, m.id)
        edited.content = "edited"
        with caplog.at_level("WARNING", logger="skmemory.backends.base"):
            backend.save(edited)
        assert any(m.id in r.getMessage() and "content" in r.getMessage() for r in caplog.records)


class TestEditsOutsideTheApiStayDetectable:
    def test_direct_file_edit_is_flagged(self, backend):
        m = _sealed(backend)
        path = backend._find_file(m.id)
        data = json.loads(path.read_text())
        data["content"] = "changed behind the store's back"
        path.write_text(json.dumps(data))
        assert not _stored(backend, m.id).verify_integrity()

    def test_save_does_not_launder_an_already_failing_predecessor(self, backend):
        m = _sealed(backend)
        path = backend._find_file(m.id)
        data = json.loads(path.read_text())
        data["content"] = "changed behind the store's back"
        path.write_text(json.dumps(data))
        tampered = _stored(backend, m.id)
        tampered.tags = ["touched-by-an-unrelated-writer"]
        backend.save(tampered)
        assert not _stored(backend, m.id).verify_integrity()

    def test_new_memory_with_a_wrong_hash_is_not_resealed(self, backend):
        m = Memory(title="t", content="c")
        m.seal()
        m.content = "c, altered after sealing and never stored before"
        backend.save(m)
        assert not _stored(backend, m.id).verify_integrity()

    def test_replacement_carrying_a_different_hash_is_not_resealed(self, backend):
        m = _sealed(backend)
        replacement = _stored(backend, m.id)
        replacement.content = "an import with its own, different seal"
        replacement.integrity_hash = "0" * 64
        backend.save(replacement)
        assert not _stored(backend, m.id).verify_integrity()

    def test_unsealed_memory_stays_unsealed(self, backend):
        m = Memory(title="t", content="c")
        backend.save(m)
        edited = _stored(backend, m.id)
        edited.content = "edited"
        backend.save(edited)
        assert _stored(backend, m.id).integrity_hash == ""
