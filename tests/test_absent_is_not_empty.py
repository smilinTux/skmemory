"""Five more places where absence read as emptiness, or failure as success.

All five were found by auditing after the earlier health work, and all five
were confirmed against the running system before being changed.

  1. An AGE graph that does NOT EXIST reported `{"ok": true, "node_count": 0}`,
     because health used the LENIENT `_cypher`, which swallows every failure
     and returns [], so the count fell to 0 and `ok` stayed True. Confirmed:
     `architect_knowledge` was absent from `ag_catalog.ag_graph` entirely and
     its agent reported a healthy, empty graph.

  2. A CORRUPT flat file was indistinguishable from an absent one. `load()`
     and `_row_to_memory()` both swallowed every exception and returned None,
     so a memory that exists on disk and cannot be parsed vanished from
     recall, list and search with no log and no counter, and `recall()` told
     the MCP caller "Memory not found", which is a wrong diagnosis. The pg
     side has counted this since the malformed-row fix; the SOURCE OF TRUTH
     never got the equivalent.

  3. `import_backup` returned only the SUCCESS count, so a 1000-record backup
     with 150 unparseable records printed "Restored 850 memories" and said
     nothing about the 150.

  4. The WAL's `pending_writes()` existed precisely to "detect incomplete
     writes after a crash" and had NO consumer anywhere in the repo. Its unit
     test passes against the class directly, which hid the absence of any
     integration point.

  5. Nothing ever cleared a sync-failure record except the identical snapshot
     path, so a `reconcile` that fixed the memory left the record standing and
     health's `unindexed` only ever climbed. Of 10 real records on one agent, 6
     were already fixed. A number that cries wolf is a number that gets
     ignored, which is how this class of bug survived six months.
"""

from __future__ import annotations

import json

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore


def _store(tmp_path, **kw):
    backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
    store = MemoryStore(primary=backend, **kw)
    store.snapshot(title="hi", content="hello world, all good here")
    return store, backend


# ---------------------------------------------------------------------------
# 2. corrupt is not missing
# ---------------------------------------------------------------------------


class TestCorruptIsNotMissing:
    def _corrupt_one(self, backend) -> str:
        path = next((backend.base_path / "short-term").glob("*.json"))
        path.write_text("{ this is not json", encoding="utf-8")
        return path.stem

    def test_a_corrupt_file_is_counted(self, tmp_path):
        _, backend = _store(tmp_path)
        assert backend.count_unloadable() == 0
        self._corrupt_one(backend)
        assert backend.count_unloadable() == 1

    def test_the_backend_reports_not_ok(self, tmp_path):
        _, backend = _store(tmp_path)
        self._corrupt_one(backend)
        health = backend.health_check()
        assert health["ok"] is False
        assert health["sync"]["unloadable"] == 1

    def test_the_store_degrades(self, tmp_path):
        store, backend = _store(tmp_path)
        self._corrupt_one(backend)
        status = store.health()
        assert status["unloadable"] == 1
        assert status["status"] == "degraded"

    def test_load_still_returns_none_but_logs(self, tmp_path, caplog):
        """None is the right return. Silence was the bug."""
        store, backend = _store(tmp_path)
        mem_id = self._corrupt_one(backend)
        with caplog.at_level("WARNING"):
            assert backend.load(mem_id) is None
        assert any("unloadable flat file" in r.message for r in caplog.records)

    def test_a_clean_store_reports_zero(self, tmp_path):
        store, backend = _store(tmp_path)
        assert backend.count_unloadable() == 0
        assert store.health()["unloadable"] == 0
        assert store.health()["status"] == "ok"

    def test_the_same_bad_file_is_logged_once_per_process(self, tmp_path, caplog):
        store, backend = _store(tmp_path)
        mem_id = self._corrupt_one(backend)
        with caplog.at_level("WARNING"):
            backend.load(mem_id)
            backend.load(mem_id)
            backend.load(mem_id)
        hits = [r for r in caplog.records if "unloadable flat file" in r.message]
        assert len(hits) == 1, "a hot read path must not spam the log"


# ---------------------------------------------------------------------------
# 3. a partial restore must not look complete
# ---------------------------------------------------------------------------


class TestImportReportsSkips:
    def _backup(self, tmp_path, good: int, bad: int):
        mems = []
        for i in range(good):
            mems.append(
                {
                    "id": f"good-{i}",
                    "title": f"Good {i}",
                    "content": "real content here",
                    "layer": "short-term",
                }
            )
        for i in range(bad):
            mems.append({"id": f"bad-{i}"})  # no title, no content
        path = tmp_path / "backup.json"
        path.write_text(json.dumps({"memories": mems}), encoding="utf-8")
        return path

    def test_a_mixed_backup_reports_both_numbers(self, tmp_path):
        _, backend = _store(tmp_path)
        path = self._backup(tmp_path, good=3, bad=2)

        restored = backend.import_backup(str(path))

        assert restored == 3
        report = backend.last_import_report
        assert report["attempted"] == 5
        assert report["restored"] == 3
        assert report["skipped"] == 2
        assert len(report["skipped_detail"]) == 2

    def test_a_clean_backup_reports_no_skips(self, tmp_path):
        _, backend = _store(tmp_path)
        path = self._backup(tmp_path, good=2, bad=0)
        backend.import_backup(str(path))
        assert backend.last_import_report["skipped"] == 0

    def test_the_skipped_detail_names_the_record(self, tmp_path):
        _, backend = _store(tmp_path)
        path = self._backup(tmp_path, good=0, bad=1)
        backend.import_backup(str(path))
        assert "bad-0" in backend.last_import_report["skipped_detail"][0]


# ---------------------------------------------------------------------------
# 4. the WAL's crash recovery finally has a reader
# ---------------------------------------------------------------------------


class TestInterruptedWritesAreSurfaced:
    def test_a_pending_write_degrades_the_store(self, tmp_path):
        store, _ = _store(tmp_path)
        store._wal.log_pending("snapshot", "never-finished", "A title", "short-term")

        status = store.health()
        assert status["interrupted_writes"] == 1
        assert status["status"] == "degraded"
        assert status["interrupted_writes_detail"]["by_op"] == {"snapshot": 1}

    def test_a_completed_write_is_not_counted(self, tmp_path):
        store, _ = _store(tmp_path)
        store._wal.log_pending("snapshot", "finished", "A title", "short-term")
        store._wal.log_done("snapshot", "finished")

        assert store.health()["interrupted_writes"] == 0

    def test_a_failed_write_is_not_counted_as_pending(self, tmp_path):
        """Failed is resolved. Pending-forever is the thing worth seeing."""
        store, _ = _store(tmp_path)
        store._wal.log_pending("snapshot", "broke", "A title", "short-term")
        store._wal.log_failed("snapshot", "broke", "boom")

        assert store.health()["interrupted_writes"] == 0

    def test_a_clean_store_reports_zero_not_none(self, tmp_path):
        store, _ = _store(tmp_path)
        assert store.health()["interrupted_writes"] == 0
        assert "interrupted_writes_detail" not in store.health()


# ---------------------------------------------------------------------------
# 5. a reconcile that fixes a memory must retire its failure record
# ---------------------------------------------------------------------------


class _VectorWith:
    """A vector backend that holds exactly the ids it was given."""

    def __init__(self, ids):
        self._ids = set(ids)

    def health_check(self):
        return {"ok": True, "embed_ok": True}

    def index_memory(self, *a, **kw):
        return True

    def load(self, memory_id):
        return object() if memory_id in self._ids else None


class TestSyncFailuresAreRetired:
    def test_a_record_for_an_indexed_memory_is_cleared(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend, vector=_VectorWith({"now-there"}))
        backend.record_sync_failure("now-there", "skvector", "embedding unavailable")

        assert store.health()["unindexed"] == 1
        report = store.reconcile_sync_failures()

        assert report == {"checked": 1, "cleared": 1, "still_missing": 0, "errors": 0}
        assert store.health()["unindexed"] == 0

    def test_a_record_for_a_still_missing_memory_is_kept(self, tmp_path):
        """The 4 of 10 that were real must survive. Clearing them would be
        the signal lying in the other direction."""
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend, vector=_VectorWith(set()))
        backend.record_sync_failure("still-gone", "skvector", "embedding unavailable")

        report = store.reconcile_sync_failures()

        assert report["cleared"] == 0
        assert report["still_missing"] == 1
        assert store.health()["unindexed"] == 1

    def test_mixed_records_are_sorted_correctly(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend, vector=_VectorWith({"a", "c"}))
        for mid in ("a", "b", "c"):
            backend.record_sync_failure(mid, "skvector", "boom")

        report = store.reconcile_sync_failures()

        assert report["checked"] == 3
        assert report["cleared"] == 2
        assert report["still_missing"] == 1

    def test_a_graph_record_is_checked_against_the_GRAPH(self, tmp_path):
        """Which store the record is about decides which store to ask."""

        class _Graph:
            def health_check(self):
                return {"ok": True}

            def index_memory(self, *a, **kw):
                return True

            def get(self, memory_id):
                return {"id": memory_id} if memory_id == "in-graph" else None

        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend, vector=_VectorWith(set()), graph=_Graph())
        backend.record_sync_failure("in-graph", "skgraph", "graph down")
        backend.record_sync_failure("not-in-graph", "skgraph", "graph down")

        report = store.reconcile_sync_failures()
        assert report["cleared"] == 1
        assert report["still_missing"] == 1

    def test_no_backend_wired_is_a_noop_not_a_crash(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        backend.record_sync_failure("x", "skvector", "boom")

        report = store.reconcile_sync_failures()
        assert report["cleared"] == 0

    def test_a_primary_without_the_methods_returns_zeroes(self):
        class Bare:
            def health_check(self):
                return {"ok": True}

        report = MemoryStore(primary=Bare()).reconcile_sync_failures()
        assert report == {"checked": 0, "cleared": 0, "still_missing": 0, "errors": 0}


@pytest.mark.parametrize("key", ["unloadable", "interrupted_writes"])
def test_the_new_signals_are_always_present(tmp_path, key):
    store, _ = _store(tmp_path)
    assert key in store.health()


class TestTheWalIsNotShared:
    """A store rooted at tmp_path must not log into the LIVE agent WAL.

    `_wal` came from `get_agent_paths()` unconditionally, ignoring where the
    primary backend was actually rooted, so every test that passes
    `base_path=tmp_path` wrote its write-ahead log into the real agent
    directory. Found while writing the tests above: a freshly created store in
    a brand new tmp_path reported someone else's pending write, because the
    WAL it read was the production one with months of real entries in it.

    Same shape as the pytest rows that reached production pg, and
    `pytest_guard` does not cover the WAL.

    For a real agent this resolves to the identical path it always did, since
    the primary's base_path IS <agent>/memory.
    """

    def test_the_wal_lives_under_the_primary_root(self, tmp_path):
        store, backend = _store(tmp_path)
        assert store._wal.path.is_relative_to(backend.base_path)
        assert store._wal.path.name == "write_log.jsonl"

    def test_two_stores_do_not_share_a_wal(self, tmp_path):
        a, _ = _store(tmp_path / "a")
        b, _ = _store(tmp_path / "b")
        assert a._wal.path != b._wal.path

    def test_a_pending_write_in_one_is_invisible_to_the_other(self, tmp_path):
        a, _ = _store(tmp_path / "a")
        b, _ = _store(tmp_path / "b")
        a._wal.log_pending("snapshot", "only-in-a", "T", "short-term")

        assert a.health()["interrupted_writes"] == 1
        assert b.health()["interrupted_writes"] == 0, "the WAL is shared"

    def test_it_does_not_touch_the_real_agent_directory(self, tmp_path):
        import os

        store, _ = _store(tmp_path)
        real = os.path.expanduser("~/.skcapstone/agents")
        assert real not in str(store._wal.path)
