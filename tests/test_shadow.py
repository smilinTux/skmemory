"""SKMEMORY_SHADOW=rust shadow-read parity (SKMEM-R-11). Hermetic: a fake skmem_core."""

from __future__ import annotations

import json
import sys
import threading
import time
import types

import pytest

from skmemory import engine, shadow
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore

SECRET = "zebracorn unmistakable secret phrase"


class FakeEngine:
    calls: list[dict] = []
    opened: list[dict] = []
    mode = "same"  # same | other | raise | hang
    release = threading.Event()

    def __init__(self, memory_dir, audience_config=None, fts=True, **kw):
        FakeEngine.opened.append({"dir": str(memory_dir), **kw})

    def search_report(self, query, limit=10, **kw):
        FakeEngine.calls.append({"query": query, "limit": limit, **kw})
        if FakeEngine.mode == "raise":
            raise RuntimeError(f"boom while searching {query}")
        if FakeEngine.mode == "hang":
            FakeEngine.release.wait(10)
        ids = FakeEngine.ids
        return {
            "results": [{"id": i} for i in ids],
            "report": {"arms": [{"name": "lexical_local", "status": "ok", "ids": ids}]},
        }


FakeEngine.ids = []


@pytest.fixture
def env(monkeypatch, tmp_path):
    FakeEngine.calls, FakeEngine.opened, FakeEngine.mode = [], [], "same"
    FakeEngine.release = threading.Event()
    mod = types.ModuleType("skmem_core")
    mod.Engine = FakeEngine
    monkeypatch.setitem(sys.modules, "skmem_core", mod)
    monkeypatch.delenv(engine.ENV, raising=False)
    monkeypatch.setenv(shadow.ENV, "rust")
    monkeypatch.setattr(shadow._Engines, "_cache", {})
    mem = tmp_path / "agent" / "memory"
    store = MemoryStore(primary=SQLiteBackend(str(mem)), use_sqlite=True)
    m = store.snapshot("t", f"{SECRET} content")
    return store, m, tmp_path / "agent" / "logs" / shadow.LOG_NAME


def rows(path, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            got = [json.loads(x) for x in path.read_text().splitlines() if x]
            if len(got) >= n:
                return got
        time.sleep(0.02)
    return []


def test_off_by_default(env, monkeypatch):
    store, m, log = env
    monkeypatch.delenv(shadow.ENV)
    assert [x.id for x in store.search(SECRET)] == [m.id]
    time.sleep(0.2)
    assert not log.exists() and FakeEngine.calls == []


def test_matching_engines_log_overlap_without_content(env):
    store, m, log = env
    FakeEngine.ids = [m.id]
    served = store.search(SECRET, reader="@public", tags=None)
    (row,) = rows(log)
    assert row["overlap"] == 1.0 and row["py_ids"] == row["rs_ids"] == [m.id]
    assert row["py_ms"] is not None and row["rs_ms"] is not None
    assert "error" not in row and row["rs_arms"] == {"lexical_local": "ok"}
    text = log.read_text()
    assert SECRET not in text and "zebracorn" not in text
    assert set(row) >= {"q", "qlen", "py_ids", "rs_ids", "overlap", "py_ms", "rs_ms"}
    assert [x.id for x in served] == row["py_ids"]


def test_rust_side_is_read_only_and_reader_identical(env):
    store, m, log = env
    FakeEngine.ids = [m.id]
    store.search(SECRET, reader="@work:acme", record_access=True)
    rows(log)
    call = FakeEngine.calls[0]
    assert call["record_access"] is False
    assert call["reader"] == "@work:acme"
    assert FakeEngine.opened[0]["read_only"] is True


def test_divergence_is_recorded_with_ids(env):
    store, m, log = env
    FakeEngine.ids = ["other-1", m.id]
    store.search(SECRET)
    (row,) = rows(log)
    assert row["rs_ids"] == ["other-1", m.id]
    s = shadow.summarize([row])
    assert s["divergences"] == 1 and s["divergent"][0]["rs_ids"] == ["other-1", m.id]
    assert s["mean_top5_overlap"] == row["overlap"] < 1.0


def test_missing_skmem_core_is_swallowed(env, monkeypatch):
    store, m, log = env
    monkeypatch.setitem(sys.modules, "skmem_core", None)
    assert [x.id for x in store.search(SECRET)] == [m.id]
    (row,) = rows(log)
    assert row["error"] == "skmem_core_missing" and row["overlap"] is None


def test_rust_error_is_swallowed_and_never_logs_query_text(env):
    store, m, log = env
    FakeEngine.mode = "raise"
    assert [x.id for x in store.search(SECRET)] == [m.id]
    (row,) = rows(log)
    assert row["error"] == "RuntimeError"
    assert SECRET not in log.read_text()


def test_hung_rust_is_bounded(env, monkeypatch):
    store, m, log = env
    monkeypatch.setenv(shadow.TIMEOUT_ENV, "0.3")
    FakeEngine.mode = "hang"
    t0 = time.perf_counter()
    assert [x.id for x in store.search(SECRET)] == [m.id]
    assert time.perf_counter() - t0 < 2.0
    (row,) = rows(log)
    assert row["error"] == "timeout"
    FakeEngine.release.set()
    time.sleep(0.2)
    assert len(rows(log)) == 1  # the late Rust result does not write a second row


def test_rust_engine_selected_means_no_shadow(env, monkeypatch):
    store, m, log = env
    monkeypatch.setenv(engine.ENV, "python")
    assert shadow.enabled()
    monkeypatch.setenv(engine.ENV, "rust")
    assert not shadow.enabled()


def test_summarize_percentiles_and_report():
    base = {"q": "a", "py_ids": ["1"], "rs_ids": ["1"], "overlap": 1.0, "k": 5}
    rs = [dict(base, py_ms=10.0 + i, rs_ms=5.0 + i) for i in range(20)]
    rs.append({"q": "e", "error": "timeout", "overlap": None})
    s = shadow.summarize(rs)
    assert s["rows"] == 21 and s["compared"] == 20 and s["errors"] == {"timeout": 1}
    assert s["mean_top5_overlap"] == 1.0 and s["divergences"] == 0
    assert s["rust_ms"]["p95"] < s["python_ms"]["p95"]
    assert "mean top-5 overlap 1.0000" in shadow.format_report(s)


def test_overlap_edges():
    assert shadow.overlap([], []) == 1.0
    assert shadow.overlap(["a"], []) == 0.0
    assert shadow.overlap(list("abcdef"), list("abcdeg")) == 1.0
