"""Three more ways health stayed green while something was broken.

All three are the postmortem's defect class, found by auditing the code AFTER
the embedder probe landed, and all three were confirmed against the real
implementation before being fixed:

  * a wired graph backend reporting ok:false left the top-level status "ok",
    because `status["graph"]` was set and then never read when computing the
    verdict. The vector backend got this fix; the graph never did.
  * an ABSENT tier directory was skipped rather than reported, so a layer that
    never synced onto a node produced zero index rows and zero files, which is
    indistinguishable from an empty tier. Deleting `long-term/` outright
    yielded `in_sync: True`.
  * the sync-failure table had no readers at all. `snapshot()` writes a row
    every time derived-store indexing fails, and nothing ever read it back, so
    a memory whose searchability was lost was discoverable only by hand.
"""

from __future__ import annotations

import shutil

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore


class _DeadGraph:
    """Only what health() asks of a graph backend."""

    def __init__(self, ok: bool = False):
        self._ok = ok

    def health_check(self) -> dict:
        return {"ok": self._ok} if self._ok else {"ok": False, "error": "graph is down"}

    # health() is the subject here; the write path must not explode.
    def index_memory(self, *a, **kw):
        return True

    def remove_memory(self, *a, **kw):
        return True


def _store(tmp_path, **kw) -> MemoryStore:
    backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
    store = MemoryStore(primary=backend, **kw)
    store.snapshot(title="hi", content="hello world, all good here")
    return store


# ---------------------------------------------------------------------------
# Graph backend verdict
# ---------------------------------------------------------------------------


class TestGraphVerdictCounts:
    def test_a_dead_graph_degrades_the_store(self, tmp_path):
        status = _store(tmp_path, graph=_DeadGraph(ok=False)).health()
        assert status["graph_ok"] is False
        assert status["status"] == "degraded"

    def test_a_live_graph_leaves_it_ok(self, tmp_path):
        status = _store(tmp_path, graph=_DeadGraph(ok=True)).health()
        assert status["graph_ok"] is True
        assert status["status"] == "ok"

    def test_graph_ok_is_none_without_a_graph_backend(self, tmp_path):
        """Not wired is not applicable, never healthy."""
        status = _store(tmp_path).health()
        assert status["graph_ok"] is None
        assert status["status"] == "ok"


# ---------------------------------------------------------------------------
# Missing tier directory
# ---------------------------------------------------------------------------


class TestMissingTierIsNotAnEmptyTier:
    def test_a_deleted_tier_is_reported_and_degrades(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        shutil.rmtree(backend.base_path / "long-term")

        drift = backend.drift_check()
        assert drift["missing_tiers"] == ["long-term"]
        assert drift["in_sync"] is False, "a whole tier vanished and drift said in_sync"

        status = store.health()
        assert status["missing_tiers"] == ["long-term"]
        assert status["status"] == "degraded"

    def test_an_empty_tier_is_still_fine(self, tmp_path):
        """The distinction the old code could not make."""
        store = _store(tmp_path)
        drift = store.primary.drift_check()
        assert drift["missing_tiers"] == []
        assert drift["in_sync"] is True
        assert store.health()["status"] == "ok"

    def test_every_tier_missing_is_reported(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")
        for tier in ("short-term", "mid-term", "long-term"):
            shutil.rmtree(backend.base_path / tier, ignore_errors=True)

        status = store.health()
        assert set(status["missing_tiers"]) == {"short-term", "mid-term", "long-term"}
        assert status["status"] in {"degraded", "red"}

    def test_by_layer_marks_presence(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        MemoryStore(primary=backend).snapshot(title="hi", content="hello world here")
        shutil.rmtree(backend.base_path / "mid-term")

        by_layer = backend.drift_check()["by_layer"]
        assert by_layer["mid-term"]["present"] is False
        assert by_layer["short-term"]["present"] is True


# ---------------------------------------------------------------------------
# The sync-failure table nobody read
# ---------------------------------------------------------------------------


class TestUnindexedIsSurfaced:
    def test_a_recorded_sync_failure_degrades_the_store(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        backend.record_sync_failure("some-memory-id", "pgvector", "embedding unavailable")

        status = store.health()
        assert status["unindexed"] == 1
        assert status["status"] == "degraded"
        assert status["unindexed_detail"]["by_backend"] == {"pgvector": 1}

    def test_a_clean_store_reports_zero(self, tmp_path):
        status = _store(tmp_path).health()
        assert status["unindexed"] == 0
        assert "unindexed_detail" not in status
        assert status["status"] == "ok"

    def test_failures_are_counted_per_backend(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        backend.record_sync_failure("a", "pgvector", "boom")
        backend.record_sync_failure("b", "pgvector", "boom")
        backend.record_sync_failure("c", "skgraph", "boom")

        detail = store.health()["unindexed_detail"]
        assert detail["count"] == 3
        assert detail["by_backend"] == {"pgvector": 2, "skgraph": 1}

    def test_a_cleared_failure_stops_counting(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        backend.record_sync_failure("a", "pgvector", "boom")
        assert store.health()["unindexed"] == 1

        backend.clear_sync_failure("a", "pgvector")
        assert store.health()["unindexed"] == 0

    def test_a_primary_without_the_method_reports_none_not_zero(self, tmp_path):
        """Undetectable is not healthy. Same rule as pg_only."""

        class Bare:
            def health_check(self):
                return {"ok": True}

        status = MemoryStore(primary=Bare()).health()
        assert status["unindexed"] is None
        assert status["status"] == "ok"


@pytest.mark.parametrize("key", ["graph_ok", "missing_tiers", "unindexed"])
def test_the_new_signals_are_always_present(tmp_path, key):
    """A caller must never have to dig into a backend-specific shape."""
    assert key in _store(tmp_path).health()
