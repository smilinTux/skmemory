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
    assert rl.arm_liveness(recs, {}, {})["semantic"]["state"] == "ok"  # one blip
    recs += [_rec(semantic=("timeout", 0)) for _ in range(2)]
    assert rl.arm_liveness(recs, {}, {})["semantic"]["state"] == "degraded"  # last 3 bad
    recs = [_rec(semantic=("ok", 2)) for _ in range(30)]
    recs[15] = _rec(semantic=("failed", 0))
    recs[16] = _rec(semantic=("failed", 0))
    recs[17] = _rec(semantic=("failed", 0))
    recs[18] = _rec(semantic=("failed", 0))
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


def test_file_modes_and_lock_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "MAX_BYTES", 100)
    for i in range(10):
        rl.record(tmp_path / "logs", {"i": i, "pad": "x" * 40})
        rl.flush()
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "logs" / rl.LOG_NAME).stat().st_mode & 0o777 == 0o600


def test_legacy_path_labelled_unobserved(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "off")
    h = _store(tmp_path).health()
    assert h["retrieval"]["observed"] is False and "unobserved" in h["retrieval"]["note"]


def _fused_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    st = _store(tmp_path)
    st.snapshot(title="alpha", content="zebra giraffe", layer=MemoryLayer.SHORT)
    return st


def test_measurement_block_writes_no_record_normal_search_unchanged(tmp_path, monkeypatch):
    st = _fused_store(tmp_path, monkeypatch)
    st.search("zebra")
    n_normal = len(_lines(tmp_path))
    assert n_normal >= 1
    with rl.measurement():
        st.search("zebra", record_access=False)
        st.search_fused("zebra", limit=5, record_access=False)
    assert len(_lines(tmp_path)) == n_normal
    st.search("zebra")
    assert len(_lines(tmp_path)) == 2 * n_normal


def test_record_access_false_alone_still_logs(tmp_path, monkeypatch):
    st = _fused_store(tmp_path, monkeypatch)
    st.search("zebra", record_access=False)
    assert _lines(tmp_path)


def test_concurrent_normal_search_logs_during_measurement(tmp_path, monkeypatch):
    st = _fused_store(tmp_path, monkeypatch)
    entered, done = threading.Event(), threading.Event()
    out = {}

    def bench():
        with rl.measurement():
            entered.set()
            assert done.wait(10)
            st.search("zebra", record_access=False)

    t = threading.Thread(target=bench)
    t.start()
    assert entered.wait(10)
    st.search("giraffe")  # other thread, measurement active in bench thread
    out["during"] = len(_lines(tmp_path))
    done.set()
    t.join(10)
    assert out["during"] >= 1
    assert len(_lines(tmp_path)) == out["during"]  # bench's own search wrote nothing

    # a nested normal search from a fresh thread spawned INSIDE a measurement also logs
    with rl.measurement():
        th = threading.Thread(target=lambda: st.search("zebra"))
        th.start()
        th.join(10)
    assert len(_lines(tmp_path)) > out["during"]


def test_measurement_resets_after_exception():
    try:
        with rl.measurement():
            raise RuntimeError
    except RuntimeError:
        pass
    assert rl._measuring.get() is False


def test_live_bench_candidate_and_arm_diff_write_no_records(tmp_path, monkeypatch):
    from skmemory.eval import live_bench as lb

    st = _fused_store(tmp_path, monkeypatch)
    arms = lb.build_arms(None, None, None, st, {})
    arms[next(k for k in arms if k.startswith("candidate_"))]("zebra")
    assert _lines(tmp_path) == []


def test_build_record_carries_embed_endpoint_index_never_a_url():
    report = FusionResult(
        ids=[], arms=[ArmOutcome(name="semantic", status="ok", ids=[], elapsed_s=0.1)]
    )
    report.embed_ep = 1
    rec = rl.build_record("q", report)
    assert rec["embed_ep"] == 1
    assert "http" not in json.dumps(rec)


def test_build_record_omits_embed_ep_when_unknown():
    report = FusionResult(ids=[], arms=[])
    assert "embed_ep" not in rl.build_record("q", report)


def test_build_record_carries_embed_ep_inflight_as_its_own_field():
    report = FusionResult(
        ids=[], arms=[ArmOutcome(name="semantic", status="timeout", ids=[], elapsed_s=4.0)]
    )
    report.embed_ep_inflight = 0
    rec = rl.build_record("q", report)
    assert rec["embed_ep_inflight"] == 0
    assert "embed_ep" not in rec


def test_build_record_late_arm_can_carry_both_status_and_embed_ep():
    report = FusionResult(
        ids=[], arms=[ArmOutcome(name="semantic", status="timeout", ids=[], elapsed_s=4.0)]
    )
    report.embed_ep = 1
    report.embed_ep_inflight = 1
    rec = rl.build_record("q", report)
    assert rec["arms"]["semantic"]["status"] == "timeout"
    assert rec["embed_ep"] == 1 and rec["embed_ep_inflight"] == 1
