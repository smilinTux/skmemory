"""Tests for the forget/redaction cascade (CR-8.3).

A forget request must fan a memory's deletion out across ALL of skmemory's
stores and return a verification :class:`ForgetReport` naming, per store, what
was found and removed (and any error). The four stores the cascade covers:

  1. flat JSON files  (source of truth, per tier)
  2. index.db SQLite  (rebuilt local index)
  3. ChromaDB         (derived vector store)
  4. skmem-pg         (derived Postgres cache)

The flat / index.db / chroma legs are exercised live against a seeded fixture
agent in a temp home. The skmem-pg leg is validated WITHOUT a live Postgres:
the cascade builds a delete PLAN (SQL + params) that a test asserts on, and only
best-effort-executes when a live pgvector backend is wired. This mirrors the
repo's structural-test convention (plan objects, not a live DB).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.forget import ForgetReport, StorePurge
from skmemory.models import MemoryLayer
from skmemory.store import MemoryStore


class _RecordingChromaBackend:
    """Stand-in for SKChromaBackend: records remove() ids.

    Named with "Chroma" so the store labels its cascade leg ``chroma``; has the
    ``save``/``remove`` surface MemoryStore touches on write/forget.
    """

    def __init__(self) -> None:
        self.removed: list[str] = []

    def save(self, memory) -> str:  # noqa: ANN001
        return memory.id

    def remove(self, memory_id: str) -> bool:
        self.removed.append(memory_id)
        return True


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[MemoryStore, dict[str, str], _RecordingChromaBackend]:
    """A fixture agent with planted memories across all three tiers.

    Uses a real SQLiteBackend rooted in a temp home, so both the flat JSON
    files AND the index.db rows exist for each seed. A recording chroma stand-in
    is wired as the vector store. Returns the store, an id-by-tier map, and the
    chroma stand-in.
    """
    base = tmp_path / "memory"
    primary = SQLiteBackend(base_path=str(base))
    chroma = _RecordingChromaBackend()
    store = MemoryStore(primary=primary, vector=chroma)

    ids: dict[str, str] = {}
    for tier, layer in (
        ("short", MemoryLayer.SHORT),
        ("mid", MemoryLayer.MID),
        ("long", MemoryLayer.LONG),
    ):
        mem = store.snapshot(title=f"{tier} seed", content=f"content for {tier}", layer=layer)
        ids[tier] = mem.id
    return store, ids, chroma


def _flat_exists(primary: SQLiteBackend, memory_id: str) -> bool:
    return primary._find_file(memory_id) is not None


def _index_has(primary: SQLiteBackend, memory_id: str) -> bool:
    row = (
        primary._get_conn().execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
    )
    return row is not None


# --- skmem-pg delete PLAN (no live DB) --------------------------------------


def test_pg_build_forget_plan_sql_and_params() -> None:
    """The skmem-pg leg is a parsed-SQL plan, assertable without a live DB.

    Two agent-scoped DELETEs: the memory's own row, then its child/chunk rows
    (memory_json.parent_id), matching PGVectorBackend.remove()'s cascade.
    """
    plan = PGVectorBackend.build_forget_plan("mem-123", "lumina")

    assert [sql for sql, _ in plan.statements] == [
        "DELETE FROM memories WHERE id=%s AND agent=%s",
        "DELETE FROM memories WHERE memory_json->>'parent_id'=%s AND agent=%s",
    ]
    assert [params for _, params in plan.statements] == [
        ("mem-123", "lumina"),
        ("mem-123", "lumina"),
    ]
    # Every statement is agent-scoped: no cross-agent blast radius.
    assert all("agent=%s" in sql for sql, _ in plan.statements)


def test_pg_remove_executes_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove() runs exactly the plan's statements (single SQL source)."""
    executed: list[tuple[str, tuple]] = []

    class _Cur:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

    class _Conn:
        def cursor(self):
            return _Cur()

    pg = PGVectorBackend(agent="lumina")
    monkeypatch.setattr(pg, "_connection", lambda: _Conn())

    assert pg.remove("mem-123") is True
    assert executed == [(sql, params) for sql, params in pg.forget_plan("mem-123").statements]


# --- full cascade against the seeded fixture --------------------------------


def test_forget_cascade_report_shape(seeded) -> None:
    store, ids, chroma = seeded
    report = store.forget_cascade(ids["mid"])

    assert isinstance(report, ForgetReport)
    assert report.memory_id == ids["mid"]
    stores = {s.store: s for s in report.stores}
    # All four stores accounted for.
    assert {"flat", "index_db", "chroma", "skmem_pg"} <= set(stores)
    for purge in report.stores:
        assert isinstance(purge, StorePurge)


def test_forget_cascade_purges_every_live_store(seeded) -> None:
    store, ids, chroma = seeded
    primary: SQLiteBackend = store.primary  # type: ignore[assignment]
    target = ids["mid"]

    # Pre-conditions: present in flat + index, siblings untouched later.
    assert _flat_exists(primary, target)
    assert _index_has(primary, target)

    report = store.forget_cascade(target)
    stores = {s.store: s for s in report.stores}

    # 1) flat JSON gone
    assert not _flat_exists(primary, target)
    assert stores["flat"].found == 1
    assert stores["flat"].removed == 1

    # 2) index.db row gone (verified by re-query, not assumed)
    assert not _index_has(primary, target)
    assert stores["index_db"].found == 1
    assert stores["index_db"].removed == 1

    # 3) chroma vector removed
    assert target in chroma.removed
    assert stores["chroma"].removed == 1

    # 4) skmem-pg leg reports a plan even with no live DB, and did not execute.
    pg = stores["skmem_pg"]
    assert pg.plan == [
        "DELETE FROM memories WHERE id=%s AND agent=%s",
        "DELETE FROM memories WHERE memory_json->>'parent_id'=%s AND agent=%s",
    ]
    assert pg.attached is False  # no wired pgvector -> fail-safe, plan-only
    assert pg.error is None

    # No error on any attached store; the whole cascade is clean.
    assert report.ok
    assert report.deleted is True

    # Siblings survive: the cascade is surgical, not a wipe.
    for other in ("short", "long"):
        assert _flat_exists(primary, ids[other])
        assert _index_has(primary, ids[other])


def test_forget_cascade_missing_memory_is_not_deleted(seeded) -> None:
    store, _ids, _chroma = seeded
    report = store.forget_cascade("does-not-exist")
    stores = {s.store: s for s in report.stores}
    assert report.deleted is False
    assert stores["flat"].found == 0
    assert stores["flat"].removed == 0
    assert stores["index_db"].found == 0
    # Plan is still emitted for skmem-pg (idempotent delete is harmless).
    assert stores["skmem_pg"].plan is not None


def test_forget_bool_api_delegates_to_cascade(seeded) -> None:
    """The legacy forget() -> bool stays a thin shim over the cascade."""
    store, ids, _chroma = seeded
    assert store.forget(ids["short"]) is True
    assert store.forget("nope") is False


def test_forget_cascade_with_live_pgvector_executes_plan(tmp_path: Path) -> None:
    """When a pgvector backend IS wired, the skmem_pg leg executes the plan."""

    class _Cur:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    pg = PGVectorBackend(agent="lumina")
    pg._connection = lambda: _Conn()  # type: ignore[method-assign]

    primary = SQLiteBackend(base_path=str(tmp_path / "memory"))
    store = MemoryStore(primary=primary, vector=pg)
    mem = store.snapshot(title="t", content="c", layer=MemoryLayer.SHORT)

    report = store.forget_cascade(mem.id)
    pg_leg = {s.store: s for s in report.stores}["skmem_pg"]
    assert pg_leg.attached is True
    assert pg_leg.removed == 1
    assert pg_leg.plan is not None
