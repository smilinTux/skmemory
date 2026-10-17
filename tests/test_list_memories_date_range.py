"""TDD for list_memories(created_after=..., created_before=...).

Card: skmemory synthesis-silent-empty-on-historical-dates, 2026-10-17.
`synthesize_daily`/`synthesize_weekly`/`synthesize_dreams` used to fetch
the N MOST RECENT memories (`list_memories(limit=500)` / `limit=1000`)
and then filter by date CLIENT-SIDE. In a large store, any historical
date whose matching memories fall outside that most-recent window
silently returns zero results -- not an error, not a warning, just an
empty narrative that looks exactly like "nothing happened that day."

These tests prove the fix at the backend level with a small `limit`
standing in for "a store much larger than limit": a date-range filter
applied BEFORE `limit` truncates must find an old memory that a
recency-only query of the same `limit` would miss entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory
from skmemory.store import MemoryStore


def _mem(title: str, created_at: str) -> Memory:
    return Memory(title=title, content=f"content for {title}", created_at=created_at)


@pytest.fixture(params=["file", "sqlite"])
def backend(request, tmp_path: Path):
    if request.param == "file":
        return FileBackend(base_path=str(tmp_path / "memories"))
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


def _seed(backend, old_count: int, recent_count: int):
    """old_count memories on 2026-01-05, recent_count memories "today"
    (2026-10-17), named so a test can assert on which set came back."""
    for i in range(old_count):
        backend.save(_mem(f"old-{i}", f"2026-01-05T0{i % 9}:00:00+00:00"))
    for i in range(recent_count):
        backend.save(_mem(f"recent-{i}", f"2026-10-17T0{i % 9}:00:00+00:00"))


def test_recency_only_query_can_miss_old_memories_entirely(backend):
    """Baseline, documenting the ORIGINAL failure shape (not a fix test):
    with no date bound and a limit smaller than recent_count, the historical
    memories never come back at all -- this is exactly why client-side date
    filtering after a limited fetch silently returns empty."""
    _seed(backend, old_count=2, recent_count=5)
    results = backend.list_memories(limit=5)
    assert all(m.title.startswith("recent-") for m in results)
    assert not any(m.title.startswith("old-") for m in results)


def test_created_after_and_before_scope_the_query_before_limit(backend):
    """The fix: a date-bounded query finds the old memories even though
    they would never survive a recency-only fetch at this limit."""
    _seed(backend, old_count=2, recent_count=5)
    results = backend.list_memories(
        limit=5,
        created_after="2026-01-05T00:00:00+00:00",
        created_before="2026-01-06T00:00:00+00:00",
    )
    titles = {m.title for m in results}
    assert titles == {"old-0", "old-1"}


def test_created_after_only_is_an_open_upper_bound(backend):
    _seed(backend, old_count=2, recent_count=3)
    results = backend.list_memories(limit=100, created_after="2026-10-17T00:00:00+00:00")
    titles = {m.title for m in results}
    assert titles == {"recent-0", "recent-1", "recent-2"}


def test_created_before_only_is_an_open_lower_bound(backend):
    _seed(backend, old_count=2, recent_count=3)
    results = backend.list_memories(limit=100, created_before="2026-10-17T00:00:00+00:00")
    titles = {m.title for m in results}
    assert titles == {"old-0", "old-1"}


def test_no_date_bound_preserves_existing_recency_behavior(backend):
    """Backward compatibility: calling without the new params must behave
    exactly as before -- newest `limit` results, nothing more."""
    _seed(backend, old_count=2, recent_count=3)
    results = backend.list_memories(limit=3)
    assert len(results) == 3
    assert all(m.title.startswith("recent-") for m in results)


def test_source_filter_scopes_before_limit(backend):
    """source filtering must also happen BEFORE limit truncates, same
    reasoning as created_after/created_before: a caller scoping to one
    source in a store dominated by other sources must not rely on a
    client-side filter after an unscoped fetch."""
    for i in range(2):
        backend.save(
            Memory(
                title=f"dream-{i}",
                content="dream content",
                source="dreaming-engine",
                created_at=f"2026-10-17T0{i}:00:00+00:00",
            )
        )
    for i in range(5):
        backend.save(
            Memory(
                title=f"manual-{i}",
                content="manual content",
                source="manual",
                created_at=f"2026-10-17T1{i}:00:00+00:00",
            )
        )
    results = backend.list_memories(limit=3, source="dreaming-engine")
    titles = {m.title for m in results}
    assert titles == {"dream-0", "dream-1"}


def test_store_list_memories_passes_through_date_bounds(tmp_path):
    """MemoryStore.list_memories must forward created_after/created_before
    to the primary backend, not just layer/tags/limit."""
    be = FileBackend(base_path=str(tmp_path / "memories"))
    store = MemoryStore(primary=be)
    _seed(be, old_count=1, recent_count=1)
    results = store.list_memories(
        limit=10,
        created_after="2026-01-05T00:00:00+00:00",
        created_before="2026-01-06T00:00:00+00:00",
    )
    assert [m.title for m in results] == ["old-0"]
