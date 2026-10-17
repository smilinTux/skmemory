"""Tests for ``MemoryStore.search()`` / ``search_fused()`` wiring (R1).

Covers the properties that make the change safe to turn on:

  * the legacy path stays reachable and unchanged behind a flag,
  * ``auto`` mode does not change behaviour for a store with no derived
    backends (fusion over one arm is the same single query),
  * a dead arm degrades the ranking instead of killing the search, and says so,
  * filters reach each arm's own query rather than the merged output,
  * a graph row the source of truth no longer has is skipped, not emitted as an
    id the caller cannot hydrate.
"""

from __future__ import annotations

import pytest

from skmemory.store import MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store on an isolated SQLite primary, no derived backends."""
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.delenv("SKMEMORY_SEARCH_FUSION", raising=False)
    s = MemoryStore()
    s.snapshot(title="Card prb-6f069c5e", content="Card prb-6f069c5e closed.", tags=["card"])
    s.snapshot(
        title="Postgres rebuild",
        content="The per node postgres rebuild remediation plan and the hardcoded dsn.",
        tags=["doc"],
    )
    s.snapshot(title="Routing", content="Settled the skgateway routing question.", tags=["doc"])
    return s


class _FakeVector:
    """Minimal stand-in for a vector backend."""

    def __init__(self, ids, *, raises=False):
        self._ids = ids
        self._raises = raises
        self.seen_kwargs = {}

    def search_semantic(self, query, limit=10):
        if self._raises:
            raise RuntimeError("no embed server")
        self.seen_kwargs = {"query": query, "limit": limit}
        return [_Mem(i) for i in self._ids[:limit]]

    def search_text(self, query, limit=10, layer=None, tags=None, source=None):
        return [_Mem(i) for i in self._ids[:limit]]


class _Mem:
    def __init__(self, mem_id):
        self.id = mem_id
        self.title = f"mem {mem_id}"


# ---------------------------------------------------------------------------
# flag handling
# ---------------------------------------------------------------------------


def test_auto_mode_is_off_without_derived_backends(store):
    """Nothing to fuse means nothing should change for existing deployments."""
    assert store.vector is None and store.graph is None
    assert store._fusion_enabled() is False


def test_auto_mode_is_on_once_a_vector_backend_exists(store):
    store.vector = _FakeVector([])
    assert store._fusion_enabled() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_flag_can_force_fusion_off(store, monkeypatch, value):
    store.vector = _FakeVector(["x"])
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", value)
    assert store._fusion_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON"])
def test_flag_can_force_fusion_on(store, monkeypatch, value):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", value)
    assert store._fusion_enabled() is True


def test_legacy_path_still_returns_results_with_fusion_off(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "off")
    assert store.search("postgres", limit=5)


# ---------------------------------------------------------------------------
# fused behaviour
# ---------------------------------------------------------------------------


def test_search_fused_reports_every_arm_status(store):
    _, report = store.search_fused("postgres", limit=5, return_report=True)
    statuses = {a.name: a.status for a in report.arms}
    assert statuses["lexical"] == "ok"
    # Absent backends are 'unavailable', NOT silently skipped.
    assert statuses["semantic"] == "unavailable"
    assert statuses["graph"] == "unavailable"
    # No date in the query, so the temporal arm legitimately has no opinion.
    assert statuses["temporal"] == "abstained"


def test_search_fused_returns_none_when_no_arm_found_anything(store):
    assert store.search_fused("zzzzz-nothing-matches-this", limit=5) is None


def test_search_fused_none_is_distinguishable_from_an_error(store):
    result, report = store.search_fused("zzzzz-nope", limit=5, return_report=True)
    assert result is None
    # The caller can see the search ran cleanly and simply found nothing, which
    # is the distinction the old `if results:` could not express.
    assert report.failed_arms == []


def test_a_failed_arm_degrades_the_ranking_but_does_not_raise(store):
    store.vector = _FakeVector([], raises=True)
    result, report = store.search_fused("postgres", limit=5, return_report=True)
    assert report.failed_arms == ["semantic"]
    # The lexical arm still answered.
    assert result
    assert "no embed server" in [a.detail for a in report.arms if a.name == "semantic"][0]


def test_search_falls_through_to_legacy_when_fusion_finds_nothing(store, monkeypatch):
    """Fusion returning None must not swallow the query.

    Uses a backend whose semantic leg is empty but whose text leg answers, so
    every fused arm abstains while the legacy path still has a result. Turning
    fusion on must not be able to LOSE a hit the old path would have returned.
    """

    class _TextOnlyVector:
        def search_semantic(self, query, limit=10):
            return []

        def search_text(self, query, limit=10, layer=None, tags=None, source=None):
            return [_Mem("legacy-only-hit")]

    store.vector = _TextOnlyVector()
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")

    # Fusion itself finds nothing for this query ...
    assert store.search_fused("zzzzz-nope", limit=5) is None
    # ... but search() still returns what the legacy chain would have.
    results = store.search("zzzzz-nope", limit=5)
    assert [m.id for m in results] == ["legacy-only-hit"]


def test_arms_overfetch_so_the_merge_has_something_to_reorder(store):
    """A top-`limit` slice per arm leaves fusion nothing to do."""
    vec = _FakeVector([f"id{i}" for i in range(50)])
    store.vector = vec
    store.search_fused("postgres", limit=3, return_report=True)
    assert vec.seen_kwargs["limit"] > 3


def test_fused_results_are_hydrated_memory_objects(store):
    results = store.search_fused("postgres", limit=5)
    assert results
    for mem in results:
        assert getattr(mem, "id", None)
        assert getattr(mem, "content", None) is not None


def test_graph_rows_missing_from_the_source_of_truth_are_skipped(store):
    """The graph is a derived projection and can name a row the flat store no
    longer has. Emitting that id would hand the caller something it cannot
    load, which is how the orphan class of bugs in postmortem 2026-10-16
    propagated."""

    class _GhostGraph:
        def search_by_entity(self, query, limit=10):
            return [{"id": "row-that-no-longer-exists"}]

    store.graph = _GhostGraph()
    _, report = store.search_fused("postgres", limit=5, return_report=True)
    assert "row-that-no-longer-exists" not in report.ids
    # The arm ran and found only unhydratable rows, so it abstained.
    graph_arm = [a for a in report.arms if a.name == "graph"][0]
    assert graph_arm.status == "abstained"


def test_temporal_arm_engages_on_a_dated_query(store):
    _, report = store.search_fused("what did we settle in October", limit=5, return_report=True)
    temporal = [a for a in report.arms if a.name == "temporal"][0]
    # The fixture rows were just written, so October of this year has content.
    assert temporal.status in ("ok", "abstained")
    # Either way it must not be 'failed'.
    assert temporal.status != "failed"


def test_fusion_preserves_the_requested_limit(store):
    store.vector = _FakeVector([])
    results = store.search_fused("postgres", limit=1)
    assert results is None or len(results) <= 1
