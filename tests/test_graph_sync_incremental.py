"""AGE ``sync_all`` must be incremental, new-first and time-bounded.

Incident 2026-10-27 (noroc2027): ``skmemory-sync@lumina`` was killed at its 3h
``TimeoutStartSec`` on every run for days. ``sync_all`` re-indexed all ~48k
flat memories every pass, each one 5 to 20 sequential Cypher round trips, and
every ``MATCH (m:Memory {id: ...})`` / ``MERGE (n:Tag {name: ...})`` was a
sequential scan because the graph had no property index (66-106 ms per Memory
lookup on 57k nodes). The unit never finished, so the cheap SQLite reindex
before it only ran once per 3h kill and ~300 flat-only memories sat unindexed.

The unit tests here run without a database. The e2e tests use a throwaway
graph and are skipped when skmem-pg is unreachable.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from skmemory.backends.age_backend import (
    GRAPH_SYNC_INDEX_VERSION,
    AGEGraphBackend,
    graph_sync_hash,
)
from skmemory.models import Memory, MemoryLayer


def _write(base: Path, tier: str, mem: Memory) -> Path:
    tier_dir = base / tier
    tier_dir.mkdir(parents=True, exist_ok=True)
    path = tier_dir / f"{mem.id}.json"
    path.write_text(json.dumps(mem.model_dump(mode="json")))
    return path


def _mem(title: str = "m", **kw) -> Memory:
    return Memory(id=str(uuid.uuid4()), title=title, content=f"content of {title}", **kw)


class _FakeGraph:
    """Stands in for the graph: remembers the hash each id was indexed at."""

    def __init__(self) -> None:
        self.hashes: dict[str, str | None] = {}
        self.calls: list[str] = []
        self.delay = 0.0
        self.state_available = True

    def bind(self) -> AGEGraphBackend:
        backend = object.__new__(AGEGraphBackend)
        backend.graph = "test_graph"
        backend.ensure_property_indexes = lambda: []
        backend.graph_sync_state = lambda: dict(self.hashes) if self.state_available else None

        def index_memory(memory, sync_hash=None):
            if self.delay:
                time.sleep(self.delay)
            self.calls.append(memory.id)
            self.hashes[memory.id] = sync_hash
            return True

        backend.index_memory = index_memory
        return backend


@pytest.fixture
def graph():
    return _FakeGraph()


class TestIncremental:
    def test_second_pass_indexes_nothing(self, tmp_path, graph):
        for i in range(5):
            _write(tmp_path, "short-term", _mem(f"m{i}"))
        backend = graph.bind()

        first = backend.sync_all(tmp_path, "t", budget_s=0)
        assert (first["indexed"], first["skipped"]) == (5, 0)

        graph.calls.clear()
        second = backend.sync_all(tmp_path, "t", budget_s=0)
        assert graph.calls == []
        assert (second["indexed"], second["skipped"], second["deferred"]) == (0, 5, 0)

    def test_changed_file_is_reindexed(self, tmp_path, graph):
        mem = _mem("before")
        path = _write(tmp_path, "mid-term", mem)
        backend = graph.bind()
        backend.sync_all(tmp_path, "t", budget_s=0)

        changed = mem.model_copy(update={"content": "after the edit"})
        path.write_text(json.dumps(changed.model_dump(mode="json")))
        graph.calls.clear()
        result = backend.sync_all(tmp_path, "t", budget_s=0)
        assert graph.calls == [mem.id]
        assert result["indexed"] == 1

    def test_node_without_hash_is_reindexed_once(self, tmp_path, graph):
        """A node written by a live save() (no hash) is re-read from flat once."""
        mem = _mem("live")
        _write(tmp_path, "short-term", mem)
        graph.hashes[mem.id] = None
        backend = graph.bind()
        assert backend.sync_all(tmp_path, "t", budget_s=0)["indexed"] == 1
        assert backend.sync_all(tmp_path, "t", budget_s=0)["indexed"] == 0

    def test_hash_covers_index_version(self):
        payload = b'{"id": "x"}'
        assert graph_sync_hash(payload) == graph_sync_hash(payload)
        assert graph_sync_hash(payload) != graph_sync_hash(payload + b" ")
        assert GRAPH_SYNC_INDEX_VERSION  # folded into the digest

    def test_full_ignores_stored_hashes(self, tmp_path, graph):
        _write(tmp_path, "short-term", _mem("a"))
        backend = graph.bind()
        backend.sync_all(tmp_path, "t", budget_s=0)
        graph.calls.clear()
        assert backend.sync_all(tmp_path, "t", budget_s=0, full=True)["indexed"] == 1

    def test_full_from_env(self, tmp_path, graph, monkeypatch):
        _write(tmp_path, "short-term", _mem("a"))
        backend = graph.bind()
        backend.sync_all(tmp_path, "t", budget_s=0)
        monkeypatch.setenv("SKMEMORY_GRAPH_SYNC_FULL", "1")
        assert backend.sync_all(tmp_path, "t", budget_s=0)["indexed"] == 1

    def test_unreadable_state_indexes_everything(self, tmp_path, graph):
        """No state is never read as 'all done'."""
        for i in range(3):
            _write(tmp_path, "short-term", _mem(f"m{i}"))
        backend = graph.bind()
        backend.sync_all(tmp_path, "t", budget_s=0)
        graph.state_available = False
        assert backend.sync_all(tmp_path, "t", budget_s=0)["indexed"] == 3


class TestBudget:
    def test_budget_defers_and_next_pass_resumes(self, tmp_path, graph):
        for i in range(6):
            _write(tmp_path, "short-term", _mem(f"m{i}"))
        graph.delay = 0.05
        backend = graph.bind()

        first = backend.sync_all(tmp_path, "t", budget_s=0.12)
        assert first["deferred"] > 0
        assert first["indexed"] + first["deferred"] == 6

        graph.delay = 0.0
        second = backend.sync_all(tmp_path, "t", budget_s=0)
        assert second["indexed"] == first["deferred"]
        assert second["skipped"] == first["indexed"]
        assert second["deferred"] == 0

    def test_unknown_memories_go_before_changed_ones(self, tmp_path, graph):
        old = [_mem(f"old{i}") for i in range(3)]
        paths = [_write(tmp_path, "long-term", m) for m in old]
        backend = graph.bind()
        backend.sync_all(tmp_path, "t", budget_s=0)
        for m, p in zip(old, paths, strict=True):  # every known memory changes
            p.write_text(
                json.dumps(m.model_copy(update={"content": "edited"}).model_dump(mode="json"))
            )
        new = _mem("brand-new")
        _write(tmp_path, "short-term", new)

        graph.calls.clear()
        backend.sync_all(tmp_path, "t", budget_s=0)
        assert graph.calls[0] == new.id
        assert sorted(graph.calls[1:]) == sorted(m.id for m in old)

    def test_budget_env_default(self, monkeypatch):
        from skmemory.backends.age_backend import _graph_sync_budget_s

        monkeypatch.delenv("SKMEMORY_GRAPH_SYNC_BUDGET_S", raising=False)
        assert _graph_sync_budget_s() == 900.0
        monkeypatch.setenv("SKMEMORY_GRAPH_SYNC_BUDGET_S", "30")
        assert _graph_sync_budget_s() == 30.0
        monkeypatch.setenv("SKMEMORY_GRAPH_SYNC_BUDGET_S", "junk")
        assert _graph_sync_budget_s() == 900.0


class TestCliSurfacesDeferred:
    def test_quiet_sync_reports_a_deferred_backlog(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from skmemory.cli import cli

        base = tmp_path / "lumina"
        (base / "memory").mkdir(parents=True)
        paths = {"base": base, "config": base / "config", "memory": base / "memory"}
        store = MagicMock()
        store.export_orphans_to_flat.return_value = {"exported": 0, "errors": 0}
        store.reindex.return_value = 1
        store.reconcile_sync_failures.return_value = {}
        with (
            patch("skmemory.agents.get_agent_paths", return_value=paths),
            patch("skmemory.backends.age_backend.AGEGraphBackend") as age_cls,
        ):
            age = age_cls.return_value
            age.ensure_graph.return_value = False
            age.sync_all.return_value = {
                "indexed": 0,
                "errors": 0,
                "skipped": 10,
                "deferred": 7,
            }
            result = CliRunner().invoke(cli, ["sync", "--quiet", "--graph"], obj={"store": store})
        assert result.exit_code == 0, result.output
        assert "graph_deferred=7" in result.output
        # The unattended path must never be allowed to write preview stubs.
        store.export_orphans_to_flat.assert_called_once_with(allow_preview_stubs=False)
        assert store.reindex.call_args.kwargs.get("allow_preview_stubs") is False


# ═══════════════════════════════════════════════════════════
# E2E against a throwaway graph (skipped without skmem-pg)
# ═══════════════════════════════════════════════════════════

DSN = os.environ.get("SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory")
GRAPH = f"skmemory_synctest_{os.getpid()}"


def _pg_ok() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=3)
        conn.cursor().execute("LOAD 'age'; SET search_path = ag_catalog, public;")
        conn.close()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _pg_ok(), reason="skmem-pg unreachable")


@pytest.fixture
def live(monkeypatch):
    import psycopg

    monkeypatch.setenv("SKMEMORY_AGENT", "lumina")
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "lumina")
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
    cur.execute("SELECT ag_catalog.create_graph(%s)", (GRAPH,))
    backend = AGEGraphBackend(dsn=DSN, graph=GRAPH)
    try:
        yield backend, cur
    finally:
        if backend._conn_obj is not None:
            backend._conn_obj.close()
        cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (GRAPH,))
        conn.close()


@requires_pg
class TestLiveGraph:
    def test_property_indexes_are_created_once(self, live):
        backend, cur = live
        created = backend.ensure_property_indexes()
        assert sorted(created) == sorted(
            f"{label}_properties_gin" for label in ("Memory", "Tag", "Entity", "Source")
        )
        assert backend.ensure_property_indexes() == []
        cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s", (GRAPH,))
        assert cur.fetchone()[0] >= 4

    def test_second_sync_skips_and_edges_are_batched(self, live, tmp_path):
        backend, _cur = live
        mem = _mem("Batched Edges", tags=["alpha", "beta", "alpha"], layer=MemoryLayer.SHORT)
        mem = mem.model_copy(
            update={"content": "Lumina Queen met Chef David at Sovereign Station today."}
        )
        _write(tmp_path, "short-term", mem)

        first = backend.sync_all(tmp_path, "t", budget_s=0)
        assert (first["indexed"], first["errors"]) == (1, 0)
        tags = backend._cypher(
            "MATCH (m:Memory {id: $id})-[:TAGGED_WITH]->(t:Tag) RETURN t.name",
            {"id": mem.id},
        )
        assert sorted(backend._agtype(r[0]) for r in tags) == ["alpha", "beta"]
        assert backend._cypher(
            "MATCH (m:Memory {id: $id})-[:MENTIONS]->(e:Entity) RETURN e.name", {"id": mem.id}
        )

        second = backend.sync_all(tmp_path, "t", budget_s=0)
        assert (second["indexed"], second["skipped"]) == (0, 1)

    def test_live_save_clears_hash_so_sync_rereads_flat(self, live, tmp_path):
        backend, _cur = live
        mem = _mem("Live Save")
        _write(tmp_path, "short-term", mem)
        backend.sync_all(tmp_path, "t", budget_s=0)
        assert backend.graph_sync_state()[mem.id] is not None
        backend.index_memory(mem)
        assert backend.graph_sync_state()[mem.id] is None
        assert backend.sync_all(tmp_path, "t", budget_s=0)["indexed"] == 1
