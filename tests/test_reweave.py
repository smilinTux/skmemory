"""Tests for the reweave backward pass (store._reweave_backward).

When a NEW memory is stored with ``reweave=True``, the store runs a bounded
backward pass that re-links the top-K most-related OLDER memories back to the
new one, keeping the association graph coherent. These tests use an isolated
FileBackend under ``tmp_path`` — they never touch the live memory store.
"""

from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.store import MemoryStore


def _make_store(tmp_path: Path, **kwargs) -> MemoryStore:
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend, **kwargs)


@pytest.fixture
def reweave_store(tmp_path: Path) -> MemoryStore:
    """Store with the backward pass enabled (small top-K for tight bounds)."""
    return _make_store(tmp_path, reweave=True, reweave_top_k=3)


class TestReweaveDisabledByDefault:
    """The backward pass must be opt-in — zero behaviour change otherwise."""

    def test_off_by_default_no_backlink(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)  # reweave defaults to False
        assert store.reweave is False

        old = store.snapshot(title="Cloud protocol notes", content="alpha content")
        new = store.snapshot(
            title="Cloud protocol follow-up",
            content="beta content",
            related_ids=[old.id],
        )

        reloaded_old = store.recall(old.id)
        assert new.id not in reloaded_old.related_ids


class TestReweaveExplicitLinks:
    """Explicit related_ids get a reciprocal back-reference."""

    def test_explicit_related_id_gets_backlink(self, reweave_store: MemoryStore) -> None:
        old = reweave_store.snapshot(title="Origin", content="the origin memory")
        new = reweave_store.snapshot(
            title="Sequel",
            content="the sequel memory",
            related_ids=[old.id],
        )

        reloaded_old = reweave_store.recall(old.id)
        assert new.id in reloaded_old.related_ids
        # Older memory's integrity hash must be resealed (not left stale).
        assert reloaded_old.verify_integrity()


class TestReweaveTopicalNeighbours:
    """Topical neighbours (shared words) are discovered and linked both ways."""

    def test_topical_neighbour_backlinked_and_symmetric(self, reweave_store: MemoryStore) -> None:
        old = reweave_store.snapshot(
            title="Postgres pgvector tuning",
            content="notes about pgvector hnsw indexes",
            tags=["postgres"],
        )
        new = reweave_store.snapshot(
            title="Postgres pgvector migration",
            content="migrating the pgvector column",
            tags=["postgres"],
        )

        # Older memory points back to the new one...
        reloaded_old = reweave_store.recall(old.id)
        assert new.id in reloaded_old.related_ids
        # ...and the new memory is kept symmetric with the discovered neighbour.
        reloaded_new = reweave_store.recall(new.id)
        assert old.id in reloaded_new.related_ids


class TestReweaveBounds:
    """The pass is bounded: never touches more than top_k older memories."""

    def test_capped_at_top_k(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, reweave=True, reweave_top_k=2)
        olds = [
            store.snapshot(title=f"Widget audit {i}", content=f"widget audit body {i}")
            for i in range(5)
        ]
        new = store.snapshot(title="Widget audit summary", content="widget audit rollup")

        backlinked = [o.id for o in (store.recall(o.id) for o in olds) if new.id in o.related_ids]
        assert len(backlinked) == 2

    def test_top_k_zero_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, reweave=True, reweave_top_k=0)
        old = store.snapshot(title="Gamma", content="gamma body")
        new = store.snapshot(title="Gamma two", content="gamma body two", related_ids=[old.id])
        assert new.id not in store.recall(old.id).related_ids


class TestReweaveGuards:
    """Older-only, chunk-skip, and idempotency guards."""

    def test_chunk_fragments_are_skipped(self, reweave_store: MemoryStore) -> None:
        # A decomposition/split fragment must never be a reweave target.
        chunk = reweave_store.snapshot(
            title="Vessel registry chunk",
            content="vessel registry fragment",
            tags=["content-chunk"],
        )
        new = reweave_store.snapshot(
            title="Vessel registry note",
            content="vessel registry claim",
        )
        assert new.id not in reweave_store.recall(chunk.id).related_ids

    def test_newer_memories_are_not_relinked(self, reweave_store: MemoryStore) -> None:
        # A topical neighbour created AFTER the driver must be left untouched.
        from skmemory.models import Memory

        first = reweave_store.snapshot(title="Timeline signal", content="timeline signal body")

        # Hand-craft a NEWER neighbour straight into the backend so it never
        # ran its own (symmetric) backward pass.
        newer = Memory(title="Timeline signal later", content="timeline signal later body")
        newer.seal()
        reweave_store.primary.save(newer)
        assert newer.created_at > first.created_at

        relinked = reweave_store._reweave_backward(first)
        assert newer.id not in relinked
        assert first.id not in reweave_store.recall(newer.id).related_ids

    def test_idempotent_repeated_pass(self, reweave_store: MemoryStore) -> None:
        old = reweave_store.snapshot(title="Repeat base", content="repeat base body")
        new = reweave_store.snapshot(
            title="Repeat follow", content="repeat follow body", related_ids=[old.id]
        )

        # First pass already ran inside snapshot(); a second explicit pass must
        # be a no-op and not duplicate the back-reference.
        relinked_again = reweave_store._reweave_backward(new)
        assert relinked_again == []
        reloaded_old = reweave_store.recall(old.id)
        assert reloaded_old.related_ids.count(new.id) == 1
