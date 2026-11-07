"""SKMEMORY_ENGINE selection and the rust shim (SKMEM-R-08)."""

from __future__ import annotations

import json
import os

import pytest

from skmemory import engine
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import MemoryLayer
from skmemory.store import MemoryStore


def test_default_is_python(monkeypatch):
    monkeypatch.delenv(engine.ENV, raising=False)
    assert engine.selected() == "python"
    monkeypatch.setenv(engine.ENV, "")
    assert engine.selected() == "python"
    monkeypatch.setenv(engine.ENV, " Python ")
    assert engine.selected() == "python"


def test_unknown_value_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv(engine.ENV, "go")
    with pytest.raises(engine.EngineConfigError, match="not a known engine"):
        engine.selected()
    store = MemoryStore(primary=SQLiteBackend(str(tmp_path)), use_sqlite=True)
    with pytest.raises(engine.EngineConfigError):
        store.search("anything")


def test_rust_without_wheel_fails_fast_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv(engine.ENV, "rust")
    monkeypatch.setitem(__import__("sys").modules, "skmem_core", None)
    store = MemoryStore(primary=SQLiteBackend(str(tmp_path)), use_sqlite=True)
    with pytest.raises(engine.EngineConfigError, match="skmem_core is not importable"):
        store.search("anything")
    with pytest.raises(engine.EngineConfigError):
        store.recall("x")


def test_python_engine_never_imports_rust(monkeypatch, tmp_path):
    monkeypatch.delenv(engine.ENV, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "skmem_core", None)
    store = MemoryStore(primary=SQLiteBackend(str(tmp_path)), use_sqlite=True)
    m = store.snapshot("t", "python path content")
    assert store.recall(m.id).id == m.id


def test_non_string_reader_is_public():
    assert engine.rust_reader(None) is None
    assert engine.rust_reader("@work:x") == "@work:x"
    for bad in (123, 1.5, ["@chef"], {"a": 1}, b"@chef", True):
        assert engine.rust_reader(bad) == "@public"


def test_audience_config_defaults_and_is_not_overridden(monkeypatch):
    monkeypatch.delenv(engine.AUDIENCE_ENV, raising=False)
    monkeypatch.setattr(engine, "_set_by_shim", None)
    engine.ensure_audience_config()
    assert os.environ[engine.AUDIENCE_ENV] == str(engine.packaged_audience_config())
    assert engine.packaged_audience_config().is_file()
    # a path the shim set follows Python's default when it changes
    monkeypatch.setattr("skmemory.audience._DEFAULT_CONFIG_PATH", "/other/aud.json")
    engine.ensure_audience_config()
    assert os.environ[engine.AUDIENCE_ENV] == "/other/aud.json"
    monkeypatch.setenv(engine.AUDIENCE_ENV, "/operator/choice.json")
    engine.ensure_audience_config()
    assert os.environ[engine.AUDIENCE_ENV] == "/operator/choice.json"


# -- rust leg (needs the skmem_core wheel) ---------------------------------------------


@pytest.fixture
def rust_store(monkeypatch, tmp_path):
    pytest.importorskip("skmem_core")
    monkeypatch.setenv(engine.ENV, "rust")
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    monkeypatch.delenv("SKMEMORY_READER", raising=False)
    store = MemoryStore(primary=SQLiteBackend(str(tmp_path / "memory")), use_sqlite=True)
    assert store._rust_engine() is not None, "rust engine not engaged"
    return store


def test_rust_round_trip_and_side_effects_once(rust_store):
    calls = {"vector": 0, "graph": 0}

    class Vec:
        def save(self, memory):
            calls["vector"] += 1

    class Graph:
        def index_memory(self, memory):
            calls["graph"] += 1

    rust_store.vector, rust_store.graph = Vec(), Graph()
    m = rust_store.snapshot("rust title", "a rust engine memory about pelicans", tags=["bird"])
    assert calls == {"vector": 1, "graph": 1}
    assert m.integrity_hash and m.verify_integrity()
    assert rust_store.recall(m.id).title == "rust title"
    assert [x.id for x in rust_store.search("pelicans")] == [m.id]
    assert [x.id for x in rust_store.list_memories(layer=MemoryLayer.SHORT)] == [m.id]
    assert rust_store.history(m.id)[0]["id"] == m.id
    assert rust_store.reader_can_see(m.id)
    wal = (rust_store.primary.base_path / "wal" / "write_log.jsonl").read_text().splitlines()
    assert [json.loads(x)["status"] for x in wal if m.id in x] == ["pending", "done"]


def test_rust_duplicate_content_writes_nothing_twice(rust_store):
    a = rust_store.snapshot("one", "identical content for dedup")
    b = rust_store.snapshot("two", "identical content for dedup")
    assert a.id == b.id


def test_rust_decompose_and_triage_route_to_python(rust_store):
    long = "sentence about memory. " * 80
    parent = rust_store.snapshot("doc", long)
    assert parent.id
    fact = rust_store.snapshot("f", "the sky is blue today", kind="fact")
    assert fact.kind == "fact" or getattr(fact.kind, "value", None) == "fact"


def test_rust_search_logs_retrieval_in_python_shape(rust_store):
    from skmemory import retrieval_log

    m = rust_store.snapshot("t", "zebra crossing logged content")
    rust_store.search("zebra")
    retrieval_log.flush() if hasattr(retrieval_log, "flush") else None
    recs = retrieval_log.read_window(rust_store._retrieval_log_dir())
    assert recs, "no retrieval record written"
    rec = recs[-1]
    assert set(rec) >= {"ts", "q", "qlen", "results", "elapsed_ms", "arms", "fused"}
    assert all(set(a) == {"status", "hits", "ms"} for a in rec["arms"].values())
    assert "zebra" not in json.dumps(rec)
    assert m.id


def test_rust_reader_ceiling_hides_private_memory(rust_store, monkeypatch):
    m = rust_store.snapshot("secret", "private ceiling probe content", metadata={})
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    assert rust_store.recall(m.id) is None
    assert rust_store.search("ceiling probe") == []
    assert rust_store.history(m.id) == []
    assert not rust_store.reader_can_see(m.id)
