"""SKMEM-P1-07: pinned fusion, graph arm off, strict temporal, concurrent arms.

Hermetic: no network, no Postgres, no embed endpoint.
"""

from __future__ import annotations

import threading
import time

import pytest

from skmemory.fusion import (
    ArmGuard,
    ArmOutcome,
    arm_executor,
    fuse,
    fusion_strategy,
    has_time_intent,
    parse_time_window,
    pinned_rrf,
    run_arms_concurrent,
)
from skmemory.store import MemoryStore


def _arm(name, ids):
    return ArmOutcome(name=name, ids=tuple(ids))


# ---------------------------------------------------------------- fusion


def test_pinned_head_is_the_lexical_top_three_in_lexical_order():
    arms = {
        "lexical": ["l1", "l2", "l3", "l4", "l5"],
        "semantic": ["v1", "v2", "l5", "l2"],
        "temporal": ["t1", "l1"],
    }
    ranked = [d for d, _, _ in pinned_rrf(arms)]
    assert ranked[:3] == ["l1", "l2", "l3"]
    # the fill comes from every live arm, nothing is dropped or duplicated
    assert sorted(ranked) == sorted({d for ids in arms.values() for d in ids})
    assert len(ranked) == len(set(ranked))


def test_the_traced_failure_interleave_pushes_gold_out_pinned_keeps_it():
    """Gold at lexical rank 3 was pushed past 5 by other arms' top picks."""
    arms = {
        "lexical": ["x1", "x2", "gold", "x4", "x5"],
        "semantic": ["v1", "v2", "v3"],
        "graph": ["g1", "g2", "g3"],
        "temporal": ["t1", "t2", "t3"],
    }
    old = fuse([_arm(n, i) for n, i in arms.items()], limit=10, strategy="interleave").ids
    new = fuse([_arm(n, i) for n, i in arms.items()], limit=10, strategy="pinned_rrf").ids
    assert old.index("gold") >= 5
    assert new.index("gold") < 5


def test_pins_the_first_lexical_arm_that_contributed():
    arms = [_arm("lexical", []), _arm("lexical_local", ["a", "b", "c", "d"]), _arm("x", ["z"])]
    assert fuse(arms, limit=5, strategy="pinned_rrf").ids[:3] == ["a", "b", "c"]


def test_pin_arm_order_is_caller_controlled():
    arms = [_arm("lexical", ["like1", "like2"]), _arm("lexical_local", ["fts1", "fts2"])]
    res = fuse(arms, limit=4, strategy="pinned_rrf", pin_arms=("lexical_local", "lexical"))
    assert res.ids[:2] == ["fts1", "fts2"]


def test_no_lexical_arm_degrades_to_plain_rrf():
    arms = {"semantic": ["a", "b"], "temporal": ["b", "c"]}
    assert [d for d, _, _ in pinned_rrf(arms)][0] == "b"


def test_strategy_env_selects_the_old_behaviour(monkeypatch):
    monkeypatch.delenv("SKMEMORY_FUSION_STRATEGY", raising=False)
    assert fusion_strategy() == "pinned_rrf"
    monkeypatch.setenv("SKMEMORY_FUSION_STRATEGY", "interleave")
    assert fusion_strategy() == "interleave"
    monkeypatch.setenv("SKMEMORY_FUSION_STRATEGY", "rrf")
    assert fusion_strategy() == "rrf"
    monkeypatch.setenv("SKMEMORY_FUSION_STRATEGY", "garbage")
    assert fusion_strategy() == "pinned_rrf"


# ---------------------------------------------------------------- temporal


@pytest.mark.parametrize(
    "query",
    [
        "Postmortem 2026-10-16 sqlite orphans",
        "Q3 2026 roadmap",
        "Release 2025 notes",
        "Incident 2024-03 review",
        "June 5 notes",
        "the rollout may halt",
        "Top 10 of 2026 list",
    ],
)
def test_a_date_or_number_inside_a_title_is_not_time_intent(query):
    assert not has_time_intent(query)


@pytest.mark.parametrize(
    "query",
    [
        "what did we decide in June",
        "since 2025-10 outages",
        "on may 5 the deploy",
        "decisions in 2026",
        "yesterday's outage",
        "what changed in the last 3 days",
        "two months ago review queue",
        "anything from last tuesday",
    ],
)
def test_real_time_intent_still_fires(query):
    assert has_time_intent(query)


def test_non_strict_parse_is_unchanged():
    assert parse_time_window("Q3 2026 roadmap") is not None
    assert parse_time_window("Q3 2026 roadmap", strict=True) is None


# ---------------------------------------------------------------- store arms


class _Mem:
    def __init__(self, mem_id, title=None):
        self.id = mem_id
        self.title = title or f"mem {mem_id}"
        self.content = ""
        self.context_tag = None
        self.tags = []


class _Vec:
    def __init__(self, ids=(), delay=0.0, raises=False):
        self.ids, self.delay, self.raises = list(ids), delay, raises
        self.threads = []

    def search_semantic(self, query, limit=10, **_):
        self.threads.append(threading.get_ident())
        time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("boom")
        return [_Mem(i) for i in self.ids[:limit]]


class _Graph:
    def __init__(self):
        self.calls = 0
        self.lineage_calls = 0

    def search_by_entity_tokens(self, query, limit=10, **_):
        self.calls += 1
        return [{"id": "g1"}]

    def health_check(self):
        return {"ok": True, "backend": "stub-graph"}

    def get_lineage(self, memory_id):
        self.lineage_calls += 1
        return [{"id": memory_id}]

    def index_memory(self, memory):
        return None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.setenv("SKMEMORY_HOME", str(tmp_path))
    for var in (
        "SKMEMORY_SEARCH_FUSION",
        "SKMEMORY_GRAPH_ARM",
        "SKMEMORY_FUSION_STRATEGY",
        "SKMEMORY_SEARCH_DEADLINE_S",
    ):
        monkeypatch.delenv(var, raising=False)
    s = MemoryStore()
    s.snapshot(title="Card prb-6f069c5e", content="Card prb-6f069c5e closed.", tags=["card"])
    s.snapshot(title="Routing", content="Settled the skgateway routing question.", tags=["doc"])
    return s


def _arms(store, query):
    _, report = store.search_fused(query, limit=5, return_report=True)
    return {a.name: a for a in report.arms}


def test_graph_arm_is_off_by_default_and_never_called(store):
    store.graph = _Graph()
    arms = _arms(store, "skgateway routing")
    assert arms["graph"].status == "disabled"
    assert "SKMEMORY_GRAPH_ARM" in arms["graph"].detail
    assert store.graph.calls == 0


def test_graph_arm_opt_in(store, monkeypatch):
    store.graph = _Graph()
    monkeypatch.setenv("SKMEMORY_GRAPH_ARM", "on")
    arms = _arms(store, "skgateway routing")
    assert arms["graph"].status != "disabled"
    assert store.graph.calls == 1


def test_health_reports_graph_arm_disabled_by_config_not_dead(store):
    store.graph = _Graph()
    h = store.health()
    assert h["arms"]["graph"]["state"] == "disabled"
    assert "config" in h["arms"]["graph"]["reason"]
    assert h["graph"]["ok"] is True and h["graph"]["search_arm"] == "disabled"
    assert h["graph_ok"] is True
    assert h["status"] != "red"


def test_graph_tools_still_work_with_the_arm_off(store):
    store.graph = _Graph()
    assert store.graph_lineage("m1") == [{"id": "m1"}]
    assert store.graph.lineage_calls == 1


def test_auto_fusion_does_not_count_a_disabled_graph_arm(store, monkeypatch):
    store.graph = _Graph()
    assert store._fusion_enabled() is False
    monkeypatch.setenv("SKMEMORY_GRAPH_ARM", "on")
    assert store._fusion_enabled() is True


def test_temporal_arm_abstains_on_a_dated_title_and_fires_on_intent(store):
    assert _arms(store, "Postmortem 2026-10-16 sqlite orphans")["temporal"].status == "abstained"
    # the window is real intent here, so the arm RAN (no memory is that old: empty is fine)
    assert _arms(store, "what happened in 2019")["temporal"].status in ("ok", "abstained")


def test_arms_run_concurrently_not_serially(store):
    store.vector = _Vec(["a"], delay=0.4)
    store.graph = _Graph()
    start = time.monotonic()
    _arms(store, "skgateway routing")
    assert time.monotonic() - start < 0.4 + 0.3
    assert threading.get_ident() not in store.vector.threads  # ran on a worker


def test_a_slow_arm_is_reported_timeout_not_empty_and_the_rest_still_answer(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.2")
    store.vector = _Vec(["a"], delay=1.5)
    start = time.monotonic()
    mems, report = store.search_fused("skgateway routing", limit=5, return_report=True)
    elapsed = time.monotonic() - start
    arms = {a.name: a for a in report.arms}
    assert arms["semantic"].status == "timeout"
    assert "deadline" in arms["semantic"].detail
    assert report.timed_out_arms == ["semantic"]
    assert arms["semantic"].status != "abstained"
    assert elapsed < 1.0, "the slowest arm must not dominate"
    assert mems and any("skgateway" in m.content for m in mems)


def test_a_failed_arm_is_still_failed(store):
    store.vector = _Vec(raises=True)
    assert _arms(store, "skgateway routing")["semantic"].status == "failed"


def test_run_arms_concurrent_classifies_everything():
    ex = arm_executor()
    out = run_arms_concurrent(
        [
            ("ok", lambda: ["a"], None),
            ("empty", lambda: [], None),
            ("off", lambda: ["x"], "configured off"),
            ("none", None, None),
            ("slow", lambda: time.sleep(1.0) or ["z"], None),
        ],
        deadline_s=0.2,
        executor=ex,
    )
    assert [o.status for o in out] == ["ok", "abstained", "disabled", "unavailable", "timeout"]


def test_record_access_and_context_tag_semantics_are_unchanged(store):
    """record_access still counts only returned hits; context_tag still filters."""
    calls = []
    store._reinforce = lambda ids, debounce_s=0: calls.append(list(ids))
    store.search("skgateway routing", limit=3, record_access=False)
    assert calls == []
    got = store.search("skgateway routing", limit=3, record_access=True)
    assert calls and calls[0] == [m.id for m in got]
    assert store.search("skgateway routing", limit=3, context_tag="@nobody-has-this") == []


# ---------------------------------------------------------------- thread safety


def test_sqlite_primary_gives_each_worker_thread_its_own_connection(store):
    main_conn = store.primary._get_conn()
    seen = {}

    def work():
        seen["conn"] = store.primary._get_conn()
        seen["again"] = store.primary._get_conn()

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn
    assert seen["again"] is seen["conn"]
    assert store.primary._get_conn() is main_conn


def test_concurrent_searches_on_one_store_do_not_misuse_sqlite(store):
    errors = []

    def hammer():
        try:
            for _ in range(15):
                assert store.search("skgateway routing", limit=3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_sqlite_reset_invalidates_worker_connections(store):
    seen = []

    def grab():
        seen.append(store.primary._get_conn())

    t = threading.Thread(target=grab)
    t.start()
    t.join()
    store.primary._reset_conn()
    t = threading.Thread(target=grab)
    t.start()
    t.join()
    assert seen[0] is not seen[1]


def test_live_bench_marks_each_connection_read_only_once():
    from skmemory.eval.live_bench import _set_session_read_only

    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql):
            self.sql.append(sql)

    owner = object()
    a, b = Conn(), Conn()
    for conn in (a, b, a, b):  # two connections interleaved, as two arm threads would
        assert _set_session_read_only(owner, conn) is conn
    assert len(a.sql) == 1 and len(b.sql) == 1
    assert "READ ONLY" in a.sql[0]


def test_fill_defaults_to_interleave_and_rrf_is_selectable(monkeypatch):
    # after the pinned head, interleave puts every arm's champion first; RRF
    # puts the doc two arms agree on first
    arms = {
        "lexical": ["l1", "l2", "l3", "a", "b"],
        "semantic": ["champ", "a", "x"],
        "temporal": ["t1", "a"],
    }
    monkeypatch.delenv("SKMEMORY_FUSION_FILL", raising=False)
    assert [d for d, _, _ in pinned_rrf(arms)][3] in ("champ", "t1")
    monkeypatch.setenv("SKMEMORY_FUSION_FILL", "rrf")
    assert [d for d, _, _ in pinned_rrf(arms)][3] == "a"


# ---------------------------------------------------------------- deadline is a real bound


class _Hung:
    """Every backend call blocks far past the deadline."""

    def __init__(self, hold=3.0):
        self.hold = hold
        self.calls = 0

    def _block(self):
        self.calls += 1
        time.sleep(self.hold)
        return []

    def search_semantic(self, *a, **k):
        return self._block()

    def search_lexical(self, *a, **k):
        return self._block()

    def search_text(self, *a, **k):
        return self._block()


def test_all_arms_hung_returns_local_results_within_the_deadline_and_says_degraded(
    store, monkeypatch
):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.5")
    hung = _Hung()
    store.vector = hung
    # lexical arm on the pg-like backend hangs too (search_lexical), and so does
    # every legacy search_text; only local SQLite works.
    t0 = time.monotonic()
    got = store.search("skgateway routing", limit=3)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5 + 0.6, f"search took {elapsed:.2f}s, the deadline must bound it"
    assert store.last_search_degraded() == "arms timed out"
    assert any("skgateway" in m.content for m in got), "local lexical still answers"
    assert hung.calls <= 2, "the legacy synchronous pg path must not be taken"


def test_hung_everything_and_no_local_hit_is_empty_but_flagged_degraded(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.4")
    store.vector = _Hung()
    t0 = time.monotonic()
    got = store.search("zzzqqq nothinghere", limit=3)
    assert time.monotonic() - t0 < 1.2
    assert got == [] and store.last_search_degraded() == "arms timed out"


def test_a_healthy_search_is_not_flagged_degraded(store):
    store.search("skgateway routing", limit=3)
    assert store.last_search_degraded() is None


def test_mcp_memory_search_reports_degraded(store, monkeypatch):
    import asyncio
    import json

    from skmemory import mcp_server

    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.4")
    store.vector = _Hung()
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    out = asyncio.run(mcp_server.call_tool("memory_search", {"query": "skgateway routing"}))
    text = out[0].text
    assert json.loads(text)["degraded"] == "arms timed out"


def test_cli_search_prints_degraded_to_stderr(store, monkeypatch):
    from click.testing import CliRunner

    from skmemory.cli import cli

    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.4")
    store.vector = _Hung()
    res = CliRunner(mix_stderr=False).invoke(cli, ["search", "skgateway routing"], obj={"store": store})
    assert "degraded: arms timed out" in res.stderr


# ---------------------------------------------------------------- one hung arm cannot eat the pool


def test_arm_guard_caps_inflight_and_cools_down():
    guard = ArmGuard(max_inflight=2, cooldown_s=0.3)
    started = []
    release = threading.Event()

    def hang():
        started.append(1)
        release.wait(5)
        return ["x"]

    def run():
        return run_arms_concurrent(
            [("slow", hang, None), ("fast", lambda: ["a"], None)],
            deadline_s=0.15,
            executor=arm_executor(),
            guard=guard,
        )

    first = run()
    assert first[0].status == "timeout" and first[1].status == "ok"
    # in cooldown: not called again, still reported timeout, and fast arm is unaffected
    second = run()
    assert second[0].status == "timeout" and "cooling down" in second[0].detail
    assert second[1].status == "ok" and len(started) == 1
    time.sleep(0.35)
    third = run()  # cooldown over: probed again (2nd in flight)
    assert len(started) == 2 and third[0].status == "timeout"
    time.sleep(0.35)
    fourth = run()  # cap of 2 in flight reached: refused without a call
    assert len(started) == 2 and "still running" in fourth[0].detail
    release.set()
    time.sleep(0.2)
    assert run()[0].status in ("ok", "timeout")  # released: admitted again


def test_a_hung_arm_does_not_starve_other_searches(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.2")
    store.vector = _Hung(hold=4.0)
    for _ in range(8):  # more searches than pool workers would allow without a cap
        store.search("skgateway routing", limit=3)
    t0 = time.monotonic()
    assert store.search("skgateway routing", limit=3)
    assert time.monotonic() - t0 < 1.0


def test_month_day_question_is_time_intent():
    assert has_time_intent("what did we decide june 5")
    assert not has_time_intent("June 5 notes")
