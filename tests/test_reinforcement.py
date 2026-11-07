"""SKMEM-P0-02: search hits and recall bump access_count; promotion reads the column."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.promotion import PromotionEngine
from skmemory.store import MemoryStore


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def store(backend):
    return MemoryStore(primary=backend)


def _row(backend, mid):
    return (
        backend._get_conn()
        .execute("SELECT access_count, last_accessed FROM memories WHERE id = ?", (mid,))
        .fetchone()
    )


def _mem(title="alpha zebra", **kw):
    return Memory(title=title, content=f"{title} content body", layer=MemoryLayer.SHORT, **kw)


def test_recall_bumps_count_and_timestamp(store, backend):
    m = store.primary.load(store.snapshot(title="t", content="recallable body").id)
    assert _row(backend, m.id)["access_count"] == 0
    store.recall(m.id)
    r = _row(backend, m.id)
    assert r["access_count"] == 1 and r["last_accessed"]


def test_search_bumps_only_returned_hits(store, backend):
    a = store.snapshot(title="zebra one", content="zebra stripes")
    b = store.snapshot(title="zebra two", content="zebra herd")
    c = store.snapshot(title="unrelated", content="nothing here")
    got = store.search("zebra", limit=10)
    assert {m.id for m in got} == {a.id, b.id}
    assert _row(backend, a.id)["access_count"] == 1
    assert _row(backend, b.id)["access_count"] == 1
    assert _row(backend, c.id)["access_count"] == 0
    assert _row(backend, a.id)["last_accessed"]


def test_search_uses_one_batched_update(store, backend):
    for i in range(5):
        store.snapshot(title=f"zebra {i}", content=f"zebra number {i}")
    stmts = []
    backend._get_conn().set_trace_callback(stmts.append)
    store.search("zebra", limit=10)
    backend._get_conn().set_trace_callback(None)
    updates = [
        s for s in stmts if s.lstrip().upper().startswith("UPDATE MEMORIES SET ACCESS_COUNT")
    ]
    assert len(updates) == 1


def test_fused_candidates_not_returned_are_not_bumped(store, backend):
    ids = [store.snapshot(title=f"zebra {i}", content=f"zebra number {i}").id for i in range(4)]
    got = store.search_fused("zebra", limit=2)
    returned = {m.id for m in got}
    assert len(returned) == 2
    for i in ids:
        assert _row(backend, i)["access_count"] == (1 if i in returned else 0)


def test_bump_failure_never_fails_search(store, monkeypatch):
    store.snapshot(title="zebra", content="zebra")

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.primary, "bump_access", boom)
    assert len(store.search("zebra")) == 1
    assert store.recall(store.search("zebra")[0].id) is not None


def test_bump_swallows_locked_db_and_restores_timeout(backend):
    m = _mem()
    backend.save(m)
    blocker = sqlite3.connect(str(backend._db_path), timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assert backend.bump_access([m.id]) == 0
    finally:
        blocker.rollback()
        blocker.close()
    timeout = backend._get_conn().execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000
    assert backend.bump_access([m.id]) == 1


def test_primary_without_counters_is_skipped():
    class Bare:
        def search_text(self, q, limit=10):
            return []

    s = MemoryStore.__new__(MemoryStore)
    s.primary = Bare()
    s._reinforce(["x"])  # no bump_access: must be a silent no-op
    assert s.usage_counts(["x"]) is None


def test_promotion_reads_column_not_metadata(store, backend):
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    m = Memory(
        title="old",
        content="old body",
        layer=MemoryLayer.SHORT,
        created_at=old,
        emotional=EmotionalSnapshot(intensity=1.0),
        metadata={"access_count": 99},  # stale metadata must not count
    )
    store.primary.save(m)
    engine = PromotionEngine(store=store)
    assert engine.evaluate(m) is None
    for _ in range(3):
        store.recall(m.id)
    assert engine.evaluate(m) == MemoryLayer.MID


def test_sweep_promotes_after_searches_and_keeps_count(store, backend):
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    m = Memory(
        title="quokka",
        content="quokka body",
        layer=MemoryLayer.SHORT,
        created_at=old,
        emotional=EmotionalSnapshot(intensity=1.0),
    )
    store.primary.save(m)
    for _ in range(3):
        store.search("quokka")
    res = PromotionEngine(store=store).sweep()
    assert res.short_to_mid == 1
    assert _row(backend, m.id)["access_count"] == 3  # promote neither bumps nor resets


def _set_counters(backend, mid):
    c = backend._get_conn()
    c.execute(
        "UPDATE memories SET access_count = 4, importance = 0.7, "
        "last_accessed = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (mid,),
    )
    c.commit()


def _counters(backend, mid):
    r = (
        backend._get_conn()
        .execute(
            "SELECT access_count, importance, last_accessed FROM memories WHERE id = ?", (mid,)
        )
        .fetchone()
    )
    return tuple(r)


def test_reindex_preserves_counters(backend):
    m = _mem()
    backend.save(m)
    _set_counters(backend, m.id)
    assert backend.reindex(force=True) == 1
    assert _counters(backend, m.id) == (4, 0.7, "2026-01-01T00:00:00+00:00")


def test_reindex_new_file_starts_at_zero_and_deleted_row_is_gone(backend):
    a, b = _mem("aaa"), _mem("bbb")
    backend.save(a)
    backend.save(b)
    _set_counters(backend, a.id)
    backend._find_file(b.id).unlink()
    backend.reindex(force=True)
    assert _counters(backend, a.id)[0] == 4
    assert (
        backend._get_conn().execute("SELECT 1 FROM memories WHERE id=?", (b.id,)).fetchone()
        is None
    )


def test_vaulted_reindex_preserves_counters(tmp_path):
    pytest.importorskip("cryptography")
    from skmemory.backends.vaulted_backend import VaultedSQLiteBackend

    vb = VaultedSQLiteBackend("pw", base_path=str(tmp_path / "v"))
    m = _mem()
    vb.save(m)
    _set_counters(vb, m.id)
    assert vb.reindex() == 1
    assert _counters(vb, m.id) == (4, 0.7, "2026-01-01T00:00:00+00:00")
    assert vb.bump_access([m.id]) == 1
    assert _counters(vb, m.id)[0] == 5
