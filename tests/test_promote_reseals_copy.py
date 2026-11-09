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


def test_a_tampered_source_is_never_laundered_into_a_clean_copy():
    src = Memory(title="t", content="c")
    src.seal()
    src.content = "edited after sealing"
    copy = src.promote(MemoryLayer.MID)
    assert not copy.verify_integrity()
    assert src.id in copy.metadata["integrity_warning"]


def test_store_promote_of_a_tampered_file_stays_flagged(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    original = store.snapshot("Digest", "microtubules tubulin")
    path = next((tmp_path / "mem").rglob(f"{original.id}.json"))
    record = json.loads(path.read_text())
    record["content"] = "edited on disk"
    path.write_text(json.dumps(record))
    copy = store.promote(original.id, MemoryLayer.MID)
    loaded = store.primary.load(copy.id)
    assert loaded is not None and not loaded.verify_integrity()


def test_store_promote_writes_a_copy_that_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    original = store.snapshot("Digest", "microtubules tubulin")
    copy = store.promote(original.id, MemoryLayer.MID)
    loaded = store.primary.load(copy.id)
    assert loaded is not None and loaded.verify_integrity()
