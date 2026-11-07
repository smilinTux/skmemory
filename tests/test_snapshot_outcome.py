"""snapshot() accepts outcome (SKMEM-P4-04): lessons are kind=procedure, outcome=lesson."""

from __future__ import annotations

from skmemory.backends.file_backend import FileBackend
from skmemory.store import MemoryStore


def test_snapshot_stores_kind_and_outcome(tmp_path):
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path)))
    mem = store.snapshot(
        "lesson: check the lock",
        "Check the lock before patching.",
        kind="procedure",
        outcome="lesson",
        metadata={"cited_ids": ["abc"]},
    )
    got = store.recall(mem.id)
    assert (got.kind, got.outcome) == ("procedure", "lesson")
    assert got.metadata["cited_ids"] == ["abc"]


def test_snapshot_outcome_defaults_to_none(tmp_path):
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path)))
    mem = store.snapshot("t", "plain content here")
    assert store.recall(mem.id).outcome is None
