"""A promoted copy is sealed under its own id. Tmp store only.

The integrity hash covers the id, and promotion mints a new one, so a copy
that inherits its source's hash fails verification on the next fortress run.
On lumina that read as 1181 "tampered" memories (2026-11-09).
"""

from __future__ import annotations

from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


def test_promoted_copy_verifies():
    src = Memory(title="t", content="c")
    src.seal()
    copy = src.promote(MemoryLayer.MID, summary="s")
    assert copy.id != src.id
    assert copy.integrity_hash == copy.compute_integrity_hash()
    assert copy.verify_integrity()
    assert src.verify_integrity()


def test_promoting_an_unsealed_source_still_yields_a_sealed_copy():
    src = Memory(title="t", content="c")
    assert not src.integrity_hash
    copy = src.promote(MemoryLayer.LONG)
    assert copy.integrity_hash == copy.compute_integrity_hash()


def test_store_promote_writes_a_copy_that_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    original = store.snapshot("Digest", "microtubules tubulin")
    copy = store.promote(original.id, MemoryLayer.MID)
    loaded = store.primary.load(copy.id)
    assert loaded is not None and loaded.verify_integrity()
