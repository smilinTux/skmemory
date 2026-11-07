"""Retrieval log and arm liveness (SKMEM-W-02)."""

from __future__ import annotations

import json
import threading
import time

from skmemory import retrieval_log as rl
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.fusion import ArmOutcome, FusionResult
from skmemory.models import MemoryLayer
from skmemory.store import MemoryStore


def _store(tmp_path):
    return MemoryStore(primary=SQLiteBackend(str(tmp_path / "mem")))


def _lines(tmp_path):
    rl.flush()
    p = tmp_path / "logs" / rl.LOG_NAME
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _rec(**arms):
    return {"arms": {k: {"status": s, "hits": h} for k, (s, h) in arms.items()}}


def test_search_logs_hashes_and_counts_never_query_text(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    st = _store(tmp_path)
    st.snapshot(title="alpha", content="zebra giraffe", layer=MemoryLayer.SHORT)
    st.search("zebra private-query-text")
    recs = _lines(tmp_path)
    assert recs, "a search must produce a record"
    raw = (tmp_path / "logs" / rl.LOG_NAME).read_text()
    assert "zebra" not in raw and "private" not in raw
    assert len(recs[-1]["q"]) == 12 and "arms" in recs[-1]


def test_write_failure_never_fails_a_search(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    st = _store(tmp_path)
    monkeypatch.setattr(rl, "_rotate", lambda p: (_ for _ in ()).throw(OSError("disk")))
    st.search("anything")
    rl.flush()


def test_record_is_nonblocking_and_bounded(tmp_path):
    t0 = time.monotonic()
    for _ in range(3000):
        rl.record(tmp_path / "logs", {"x": 1})
    assert time.monotonic() - t0 < 2.0
    rl.flush(5)


def test_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "MAX_BYTES", 200)
    for i in range(40):
        rl.record(tmp_path / "logs", {"i": i, "pad": "x" * 50})
        rl.flush()
    assert (tmp_path / "logs" / "retrieval.jsonl.1").exists()


def test_window_spans_processes_via_the_file(tmp_path):
    for i in range(5):
        rl.record(tmp_path / "logs", {"i": i})
    assert [r["i"] for r in rl.read_window(tmp_path / "logs")] == [0, 1, 2, 3, 4]


def test_disabled_arm_never_degrades():
    recs = [_rec(graph=("disabled", 0)) for _ in range(100)]
    out = rl.arm_liveness(recs, {"graph": {"state": "disabled"}}, {"graph": False})
    assert out["graph"]["state"] == "disabled"


def test_zero_hits_for_100_is_quiet_when_backend_ok():
    recs = [_rec(semantic=("abstained", 0)) for _ in range(100)]
    out = rl.arm_liveness(recs, {}, {"vector": True})
    assert out["semantic"]["state"] == "quiet"


def test_zero_hits_for_100_with_failed_probe_is_degraded():
    recs = [_rec(semantic=("ok", 0)) for _ in range(100)]
    out = rl.arm_liveness(recs, {}, {"vector": False})
    assert out["semantic"]["state"] == "degraded"


def test_errors_and_timeouts_degrade():
    recs = [_rec(semantic=("ok", 2)) for _ in range(20)] + [_rec(semantic=("timeout", 0))]
    assert rl.arm_liveness(recs, {}, {})["semantic"]["state"] == "degraded"
    recs = [_rec(semantic=("ok", 2)) for _ in range(30)]
    recs[3] = _rec(semantic=("failed", 0))
    recs[4] = _rec(semantic=("failed", 0))
    recs[5] = _rec(semantic=("failed", 0))
    recs[6] = _rec(semantic=("failed", 0))
    assert rl.arm_liveness(recs, {}, {})["semantic"]["state"] == "degraded"


def test_healthy_arm_ok():
    recs = [_rec(lexical=("ok", 3)) for _ in range(50)]
    assert rl.arm_liveness(recs, {}, {})["lexical"]["state"] == "ok"


def test_build_record_fused_distribution():
    rep = FusionResult(
        ids=["a", "b"],
        arms=[ArmOutcome("lexical", ("a", "b"), "ok", elapsed_s=0.01)],
        provenance={"a": {"lexical": 1}, "b": {"lexical": 2}},
    )
    rec = rl.build_record("hello world", rep)
    assert rec["fused"]["n"] == 2 and rec["fused"]["max"] > rec["fused"]["min"]
    assert rec["arms"]["lexical"]["ms"] == 10.0 and rec["qlen"] == 2


def test_health_degrades_on_logged_arm_errors_not_on_disabled_graph(tmp_path):
    st = _store(tmp_path)
    d = tmp_path / "logs"
    for _ in range(10):
        rl.record(d, _rec(graph=("disabled", 0), lexical=("ok", 1)))
    rl.flush()
    h = st.health()
    assert h["retrieval"]["arms"]["graph"]["state"] == "disabled"
    assert h["retrieval"]["arms"]["lexical"]["state"] == "ok"
    for _ in range(10):
        rl.record(d, _rec(lexical=("failed", 0)))
    rl.flush()
    h2 = st.health()
    assert h2["retrieval"]["arms"]["lexical"]["state"] == "degraded"
    assert h2["status"] in ("degraded", "red")


def test_threaded_records_all_land(tmp_path):
    ts = [
        threading.Thread(
            target=lambda: [rl.record(tmp_path / "logs", {"n": 1}) for _ in range(50)]
        )
        for _ in range(4)
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rl.flush(5)
    assert len(rl.read_window(tmp_path / "logs", 500)) == 200
