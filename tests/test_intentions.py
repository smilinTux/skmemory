"""Intention model, code-owned lifecycle, flat-file store and supersession sweep (SKMEM-P2-01)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skmemory.eval import fixtures as fx
from skmemory.intentions import (
    Intention,
    IntentionStore,
    transition,
)


@pytest.fixture(autouse=True)
def _generous_deadline(monkeypatch):
    # fsynced fires on a loaded box can exceed the 50 ms production deadline; tests that
    # assert deadline behaviour set their own value
    monkeypatch.setenv("SKMEMORY_PREFILTER_DEADLINE_S", "5")


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _fixture_intentions() -> list[Intention]:
    return [Intention(**i.model_dump()) for i in fx.load_intentions()]


@pytest.mark.parametrize(
    "v", fx.load_intention_transitions(), ids=lambda v: f"{v.state}-{v.event}"
)
def test_transition_vectors(v):
    base = _fixture_intentions()[0].model_copy(update={"state": v.state})
    assert transition(base, v.event, now=NOW).state == v.expected


def test_transition_is_pure_and_counts_fires():
    i = Intention(id="x", kind="event", title="t", state="armed", trigger_terms=["a"])
    out = transition(i, "fire", now=NOW)
    assert i.state == "armed" and i.fires == 0
    assert out.fires == 1 and out.last_fired_at == NOW


def test_terminal_states_ignore_every_event():
    for state in ("done", "cancelled", "expired", "invalidated"):
        i = Intention(id="x", kind="time", title="t", state=state)
        for ev in ("arm", "fire", "snooze", "done", "cancel", "expire", "invalidate"):
            out = transition(i, ev, now=NOW)
            assert out.state == state and out.fires == 0


def test_unknown_event_raises():
    i = Intention(id="x", kind="time", title="t", state="armed")
    with pytest.raises(ValueError):
        transition(i, "explode")


def test_snooze_sets_until():
    i = Intention(id="x", kind="time", title="t", state="fired")
    until = datetime(2026, 11, 3, tzinfo=timezone.utc)
    out = transition(i, "snooze", now=NOW, snooze_until=until)
    assert out.state == "armed" and out.snooze_until == until


def test_bad_id_rejected(tmp_path):
    store = IntentionStore(tmp_path)
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            store.save(Intention(id=bad, kind="time", title="t"))


def test_save_writes_flat_file_one_per_intention_and_index(tmp_path):
    store = IntentionStore(tmp_path)
    for i in _fixture_intentions():
        store.save(i)
    files = sorted(p.name for p in (tmp_path / "intentions").glob("*.json"))
    assert len(files) == 12 and "int-001.json" in files
    assert json.loads((tmp_path / "intentions" / "int-004.json").read_text())["kind"] == "event"
    rows = sqlite3.connect(tmp_path / "index.db").execute("select count(*) from intentions")
    assert rows.fetchone()[0] == 12
    assert store.get("int-004").trigger_terms[0] == "rollout ring"
    assert {i.id for i in store.list(state="armed")} == {
        "int-003",
        "int-004",
        "int-005",
        "int-006",
        "int-008",
        "int-011",
        "int-012",
    }


def test_roundtrip_preserves_unknown_fields(tmp_path):
    store = IntentionStore(tmp_path)
    (tmp_path / "intentions").mkdir(exist_ok=True)
    (tmp_path / "intentions" / "n.json").write_text(
        json.dumps({"id": "n", "kind": "time", "title": "t", "from_newer_host": 7})
    )
    got = store.get("n")
    store.save(got)
    assert json.loads((tmp_path / "intentions" / "n.json").read_text())["from_newer_host"] == 7


def test_index_is_rebuildable_from_flat_files(tmp_path):
    store = IntentionStore(tmp_path)
    for i in _fixture_intentions():
        store.save(i)
    store.close()
    (tmp_path / "index.db").unlink()
    store = IntentionStore(tmp_path)
    assert store.list() == []  # index empty until rebuilt
    assert store.reindex() == 12
    assert len(store.list()) == 12


def test_reindex_drops_rows_whose_flat_file_is_gone(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="time", title="t"))
    (tmp_path / "intentions" / "a.json").unlink()
    assert store.reindex() == 0 and store.list() == []


def test_corrupt_flat_file_is_skipped_on_reindex(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="time", title="t"))
    (tmp_path / "intentions" / "bad.json").write_text("{not json")
    assert store.reindex() == 1


def _write_origin(tmp_path, mid, valid_to):
    d = tmp_path / "mid-term"
    d.mkdir(exist_ok=True)
    (d / f"{mid}.json").write_text(json.dumps({"id": mid, "valid_to": valid_to}))


def test_sweep_invalidates_when_origin_valid_to_passed(tmp_path):
    store = IntentionStore(tmp_path)
    for i in _fixture_intentions():
        store.save(i)
    for m in fx.load_corpus():  # flat files as a v2 writer would produce them
        _write_origin(tmp_path, m.id, m.valid_to)
    changed = store.sweep_supersession(now=NOW)
    assert changed == ["int-011"]
    assert store.get("int-011").state == "invalidated"
    expected = {
        i.id: i.expected_state_after_supersession_sweep
        for i in fx.load_intentions()
        if i.expected_state_after_supersession_sweep
    }
    assert {k: store.get(k).state for k in expected} == expected
    assert store.get("int-004").state == "armed"
    assert [i.id for i in store.list(state="invalidated")] == ["int-011"]
    assert store.sweep_supersession(now=NOW) == []  # idempotent


def test_sweep_ignores_future_valid_to_missing_file_and_terminal(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="fut", kind="event", title="t", state="armed", origin_memory_id="m1"))
    store.save(Intention(id="gone", kind="event", title="t", state="armed", origin_memory_id="m2"))
    store.save(Intention(id="dn", kind="event", title="t", state="done", origin_memory_id="m3"))
    _write_origin(tmp_path, "m1", "2027-01-01T00:00:00+00:00")
    _write_origin(tmp_path, "m3", "2026-01-01T00:00:00+00:00")
    assert store.sweep_supersession(now=NOW) == []
    assert store.get("dn").state == "done"


def test_sweep_reads_valid_to_in_metadata(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="event", title="t", state="armed", origin_memory_id="m1"))
    d = tmp_path / "long-term"
    d.mkdir()
    (d / "m1.json").write_text(
        json.dumps({"id": "m1", "metadata": {"valid_to": "2026-10-01T00:00:00Z"}})
    )
    assert store.sweep_supersession(now=NOW) == ["a"]


CONFLICT = "a1.sync-conflict-20261101-120000-ABCDEFG.json"


def test_reindex_merges_conflict_copy_terminal_beats_live(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a1", kind="time", title="canon", state="armed"))
    d = Intention(id="a1", kind="time", title="copy", state="done")
    (tmp_path / "intentions" / CONFLICT).write_text(d.model_dump_json())
    assert store.reindex() == 1
    assert store.last_reindex_report["conflicts"] == [CONFLICT]
    assert [(i.id, i.state) for i in store.list()] == [("a1", "done")]
    assert store.get("a1").state == "done"  # written to the canonical file
    assert not (tmp_path / "intentions" / CONFLICT).exists()


def test_reindex_promotes_conflict_only_file(tmp_path):
    store = IntentionStore(tmp_path)
    c = Intention(id="a1", kind="time", title="only", state="armed")
    (tmp_path / "intentions" / CONFLICT).write_text(c.model_dump_json())
    assert store.reindex() == 1
    assert store.get("a1").title == "only"
    assert not (tmp_path / "intentions" / CONFLICT).exists()


def test_conflict_copy_kept_if_merged_write_fails(tmp_path, monkeypatch):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a1", kind="time", title="canon", state="armed"))
    d = Intention(id="a1", kind="time", title="copy", state="done")
    (tmp_path / "intentions" / CONFLICT).write_text(d.model_dump_json())
    import skmemory.intentions as mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", boom)
    with pytest.raises(OSError):
        store.reindex()
    assert (tmp_path / "intentions" / CONFLICT).exists()


def test_merge_policy_order_and_convergence():
    from itertools import permutations

    from skmemory.intentions import merge_intentions

    def mk(state, fires=0, title="t", last=None):
        return Intention(
            id="x", kind="event", title=title, state=state, fires=fires, last_fired_at=last
        )

    states = ["pending", "armed", "fired", "expired", "invalidated", "cancelled", "done"]
    for lo, hi in zip(states, states[1:], strict=False):
        assert merge_intentions(mk(lo, 9), mk(hi)).state == hi
        assert merge_intentions(mk(hi), mk(lo, 9)).state == hi
    a, b = mk("armed", 1, "A"), mk("armed", 3, "B", NOW)
    assert merge_intentions(a, b) == merge_intentions(b, a)
    assert merge_intentions(a, b).fires == 3
    tie1, tie2 = mk("armed", 1, "A"), mk("armed", 1, "B")  # fully tied: text breaks it
    assert merge_intentions(tie1, tie2) == merge_intentions(tie2, tie1)
    reps = [
        mk("fired", 2, "A"),
        mk("armed", 5, "B"),
        mk("cancelled", 1, "C"),
        mk("pending", 0, "D"),
    ]
    outs = set()
    for perm in permutations(reps):
        r = perm[0]
        for x in perm[1:]:
            r = merge_intentions(r, x)
        outs.add(r.model_dump_json())
    assert len(outs) == 1


def test_two_hosts_converge_through_reindex(tmp_path):
    """Host 1 and host 2 hold the same replicas under swapped canonical/conflict names."""
    reps = [
        Intention(id="a1", kind="time", title="h1", state="armed", fires=1),
        Intention(id="a1", kind="time", title="h2", state="cancelled", fires=0),
    ]
    results = []
    for n, order in enumerate((reps, reps[::-1])):
        base = tmp_path / f"h{n}"
        store = IntentionStore(base)
        (base / "intentions" / "a1.json").write_text(order[0].model_dump_json())
        (base / "intentions" / CONFLICT).write_text(order[1].model_dump_json())
        store.reindex()
        results.append((base / "intentions" / "a1.json").read_text())
        store.close()
    assert json.loads(results[0]) == json.loads(results[1])
    assert json.loads(results[0])["state"] == "cancelled"


def _race(base, event, q):
    IntentionStore(base).apply("a", event, now=NOW)


def test_concurrent_done_and_fire_never_leave_fired(tmp_path):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    bad = 0
    for n in range(12):
        base = tmp_path / f"t{n}"
        store = IntentionStore(base)
        store.save(Intention(id="a", kind="time", title="t", state="armed"))
        store.close()
        ps = [ctx.Process(target=_race, args=(base, ev, None)) for ev in ("fire", "done")]
        for p in ps:
            p.start()
        for p in ps:
            p.join()
        if IntentionStore(base).get("a").state != "done":
            bad += 1
    assert bad == 0


def test_lock_failure_degrades_to_unlocked(tmp_path, monkeypatch):
    import skmemory.intentions as mod

    def boom(*a, **k):
        raise OSError("no flock here")

    monkeypatch.setattr(mod.fcntl, "flock", boom)
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="time", title="t"))
    assert store.apply("a", "arm").state == "armed"


def test_sweep_rereads_flat_file_when_index_is_stale(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="event", title="t", origin_memory_id="m1"))
    _write_origin(tmp_path, "m1", "2026-10-01T00:00:00+00:00")
    # a synced host marked it done; this host's index still says pending
    done = Intention(id="a", kind="event", title="t", origin_memory_id="m1", state="done")
    (tmp_path / "intentions" / "a.json").write_text(done.model_dump_json())
    assert store.list()[0].state == "pending"
    assert store.sweep_supersession(now=NOW) == []
    assert store.get("a").state == "done"


def test_save_cannot_revive_terminal_or_change_state(tmp_path):
    store = IntentionStore(tmp_path)
    done = Intention(id="a", kind="time", title="t", state="done")
    store.save(done)
    with pytest.raises(ValueError):
        store.save(done.model_copy(update={"state": "pending"}))
    assert store.get("a").state == "done"
    store.save(Intention(id="b", kind="time", title="t"))
    with pytest.raises(ValueError):
        store.save(Intention(id="b", kind="time", title="t", state="armed"))


def test_apply_uses_transition_and_terminal_stays_terminal(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="time", title="t"))
    assert store.apply("a", "arm").state == "armed"
    assert store.apply("a", "fire", now=NOW).fires == 1
    assert store.get("a").state == "fired" and store.list(state="fired")[0].id == "a"
    store.apply("a", "done")
    assert store.apply("a", "fire", now=NOW).state == "done"
    assert store.apply("nope", "arm") is None


def test_corrupt_file_does_not_raise_from_get_list_or_sweep(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="event", title="t", origin_memory_id="m1"))
    _write_origin(tmp_path, "m1", "2026-10-01T00:00:00+00:00")
    (tmp_path / "intentions" / "a.json").write_text("{trunc")
    assert store.get("a") is None
    assert store.sweep_supersession(now=NOW) == []
    assert store.unreadable >= 2
    row = sqlite3.connect(tmp_path / "index.db")
    row.execute("update intentions set data='{bad'")
    row.commit()
    assert store.list() == []


def test_reindex_is_one_transaction(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="time", title="t"))
    other = sqlite3.connect(tmp_path / "index.db")
    seen = []
    orig = store._db().executemany

    class Spy:
        def __init__(self, c):
            self.c = c

        def execute(self, *a):
            return self.c.execute(*a)

        def executemany(self, *a):
            seen.append(other.execute("select count(*) from intentions").fetchone()[0])
            return self.c.executemany(*a)

        def __enter__(self):
            return self.c.__enter__()

        def __exit__(self, *a):
            return self.c.__exit__(*a)

    real = store._db()
    store._conn = Spy(real)
    store.reindex()
    assert seen == [1]  # other connections still saw the old row mid-rebuild
    assert orig  # keep reference used


# ---------------------------------------------------------------- prefilter (SKMEM-P2-02)
from skmemory.intentions import prefilter_safe  # noqa: E402


def _loaded_store(tmp_path):
    store = IntentionStore(tmp_path)
    for i in _fixture_intentions():
        store.save(i)
    for m in fx.load_corpus():
        _write_origin(tmp_path, m.id, m.valid_to)
    return store


@pytest.mark.parametrize("p", fx.load_prompts(), ids=lambda p: p.id)
def test_prefilter_prompt_vectors(tmp_path, p):
    store = _loaded_store(tmp_path)
    now = datetime.fromisoformat(p.now)
    store.sweep_supersession(now=now)  # p-10 needs int-011 invalidated first
    got = [i.id for i in store.prefilter(p.text, p.scope, now)]
    if p.id == "p-17":
        # time tick: due time intentions are the time path's job, not the prompt prefilter's
        assert got == []
    else:
        assert sorted(got) == sorted(p.expected_fire), p.text


def test_word_boundary_fusion_not_confusion(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="f", kind="event", title="t", state="armed", trigger_terms=["fusion"]))
    assert store.prefilter("total confusion", {}, NOW) == []
    assert store.prefilter("a defusion", {}, NOW) == []
    assert [i.id for i in store.prefilter("rework the (fusion) weights", {}, NOW)] == ["f"]


def test_nonword_edge_terms_match_without_boundary_guard(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="h", kind="event", title="t", state="armed", trigger_terms=["HOSTS="]))
    assert [i.id for i in store.prefilter('HOSTS="a"', {}, NOW)] == ["h"]
    assert store.prefilter("GHOSTS=1", {}, NOW) == []


def test_blank_term_never_matches_everything(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="b", kind="event", title="t", state="armed", trigger_terms=["", " "]))
    assert store.prefilter("anything", {}, NOW) == []


def test_fire_updates_flat_and_index_and_state(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(
        Intention(
            id="a", kind="event", title="t", state="armed", trigger_terms=["x y"], max_fires=3
        )
    )
    out = store.prefilter("do x y now", {}, NOW)
    assert out[0].fires == 1 and out[0].state == "fired" and out[0].last_fired_at == NOW
    flat = store.get("a")
    assert (flat.fires, flat.state, flat.last_fired_at) == (1, "fired", NOW)
    idx = IntentionStore(tmp_path).list()[0]
    assert (idx.fires, idx.state, idx.last_fired_at) == (1, "fired", NOW)
    # a fired intention under max_fires stays eligible (cooldown 0)
    assert store.prefilter("x y again", {}, NOW)[0].fires == 2


def test_max_fires_and_cooldown_are_enforced_on_flat_truth(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(
        Intention(
            id="a",
            kind="event",
            title="t",
            state="armed",
            trigger_terms=["zz"],
            max_fires=2,
            cooldown_s=60,
        )
    )
    assert store.prefilter("zz", {}, NOW)
    assert store.prefilter("zz", {}, NOW.replace(second=30)) == []  # cooling down
    assert store.prefilter("zz", {}, NOW.replace(minute=5))  # second and last fire
    assert store.prefilter("zz", {}, NOW.replace(minute=30)) == []  # max_fires reached


def test_stale_index_cannot_double_fire(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(
        Intention(
            id="a", kind="event", title="t", state="armed", trigger_terms=["zz"], max_fires=1
        )
    )
    # simulate a stale index row: flat already fired, index still says armed/0
    flat = store.get("a").model_copy(update={"state": "fired", "fires": 1, "last_fired_at": NOW})
    from skmemory.intentions import atomic_write_text

    atomic_write_text(store._path("a"), flat.model_dump_json(indent=2))
    assert store.prefilter("zz", {}, NOW) == []
    assert store.get("a").fires == 1


def test_expired_pending_scope_missing_key_skipped(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="p", kind="event", title="t", state="pending", trigger_terms=["q"]))
    store.save(
        Intention(
            id="e",
            kind="event",
            title="t",
            state="armed",
            trigger_terms=["q"],
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(
        Intention(
            id="s",
            kind="event",
            title="t",
            state="armed",
            trigger_terms=["q"],
            scope={"repo": "r"},
        )
    )
    assert store.prefilter("q", {}, NOW) == []  # scope key absent from the prompt scope
    assert store.prefilter("q", None, NOW) == []
    assert [i.id for i in store.prefilter("q", {"repo": "r", "other": "x"}, NOW)] == ["s"]


def _fire_race(base, out):
    got = IntentionStore(base).prefilter("zz", {}, NOW)
    Path(out).write_text(str(len(got)))


def test_concurrent_prompts_fire_once(tmp_path):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    for n in range(8):
        base = tmp_path / f"t{n}"
        s = IntentionStore(base)
        s.save(
            Intention(
                id="a", kind="event", title="t", state="armed", trigger_terms=["zz"], max_fires=1
            )
        )
        s.close()
        outs = [str(tmp_path / f"o{n}-{k}") for k in range(4)]
        ps = [ctx.Process(target=_fire_race, args=(base, o)) for o in outs]
        for p in ps:
            p.start()
        for p in ps:
            p.join()
        fin = IntentionStore(base)
        assert fin.get("a").fires == 1
        assert fin.list()[0].fires == 1
        assert sum(int(Path(o).read_text()) for o in outs if os.path.exists(o)) <= 1


def test_locked_store_fails_silent_and_fast(tmp_path):
    import fcntl
    import time

    store = IntentionStore(tmp_path)
    store.save(Intention(id="a", kind="event", title="t", state="armed", trigger_terms=["zz"]))
    store._lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(store._lock_dir / "a.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = time.perf_counter()
        assert store.prefilter("zz", {}, NOW) == []
        assert time.perf_counter() - t < 0.5
    finally:
        os.close(fd)
    assert store.get("a").fires == 0


def test_prefilter_safe_never_raises(tmp_path, monkeypatch):
    assert prefilter_safe(tmp_path / "nope" / "x" / "\0bad", "zz", {}, NOW) == []
    s = IntentionStore(tmp_path)
    s.save(Intention(id="a", kind="event", title="t", state="armed", trigger_terms=["zz"]))
    s.close()
    assert [i.id for i in prefilter_safe(tmp_path, "zz", {}, NOW)] == ["a"]
    import skmemory.intentions as mod

    monkeypatch.setattr(mod.IntentionStore, "prefilter", lambda *a, **k: 1 / 0)
    assert prefilter_safe(tmp_path, "zz", {}, NOW) == []
    assert prefilter_safe(tmp_path, None, None, None) == []


def test_old_index_schema_is_migrated_and_backfilled(tmp_path):
    s = IntentionStore(tmp_path)
    s.save(Intention(id="a", kind="event", title="t", state="armed", trigger_terms=["zz"]))
    s.close()
    db = sqlite3.connect(str(tmp_path / "index.db"))
    db.execute("DROP TABLE intentions")
    db.execute(
        "CREATE TABLE intentions (id TEXT PRIMARY KEY, kind TEXT, state TEXT, due_at TEXT,"
        " snooze_until TEXT, expires_at TEXT, origin_memory_id TEXT, channel TEXT,"
        " data TEXT NOT NULL)"
    )
    body = (tmp_path / "intentions" / "a.json").read_text()
    db.execute(
        "INSERT INTO intentions VALUES ('a','event','armed',NULL,NULL,NULL,NULL,'inline',?)",
        (body,),
    )
    db.commit()
    db.close()
    assert [i.id for i in IntentionStore(tmp_path).prefilter("zz", {}, NOW)] == ["a"]


def test_prefilter_5ms_budget_at_1000_intentions(tmp_path):
    import statistics
    import time

    store = IntentionStore(tmp_path)
    for n in range(1000):
        store.save(
            Intention(
                id=f"i{n}",
                kind="event",
                title="t",
                state="armed",
                trigger_terms=[f"term{n} alpha", f"beta{n}", f"gamma delta {n}"],
                scope={"repo": "skmemory"} if n % 2 else {},
                max_fires=5,
            )
        )
    store.close()
    fresh = IntentionStore(tmp_path)  # a hook is a fresh process: include nothing warm but sqlite
    text = "please rework the retrieval fusion weights and review the rollout ring for skap08"
    times = []
    for _ in range(30):
        t = time.perf_counter()
        fresh.prefilter(text, {"repo": "skmemory"}, NOW)
        times.append((time.perf_counter() - t) * 1000)
    med = statistics.median(times)
    print(f"prefilter 1000 intentions: median {med:.2f} ms, max {max(times):.2f} ms")
    assert med < 5.0


def _many(tmp_path, n, term_fmt="term{n} alpha", **kw):
    store = IntentionStore(tmp_path)
    for k in range(n):
        store.save(
            Intention(
                id=f"i{k}", kind="event", title="t", state="armed",
                trigger_terms=[term_fmt.format(n=k)], **kw,
            )
        )  # fmt: skip
    return store


def test_held_locks_total_stays_under_deadline(tmp_path):
    import fcntl
    import time

    store = _many(tmp_path, 200, "zz")
    store._lock_dir.mkdir(parents=True, exist_ok=True)
    fds = []
    for k in range(200):
        fd = os.open(store._lock_dir / f"i{k}.lock", os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds.append(fd)
    try:
        t = time.perf_counter()
        assert prefilter_safe(tmp_path, "zz", {}, NOW) == []
        elapsed = time.perf_counter() - t
        assert elapsed < 0.25, elapsed  # was ~10.9 s with 50 ms per candidate
    finally:
        for fd in fds:
            os.close(fd)
    assert all(store.get(f"i{k}").fires == 0 for k in range(200))


def test_fires_per_prompt_are_capped(tmp_path):
    store = _many(tmp_path, 10, "zz")
    got = store.prefilter("zz", {}, NOW)
    assert len(got) == 3
    assert len(store.prefilter("zz", {}, NOW, max_fires=2)) == 2


def test_env_overrides_deadline_and_cap(tmp_path, monkeypatch):
    store = _many(tmp_path, 10, "zz")
    monkeypatch.setenv("SKMEMORY_PREFILTER_MAX_FIRES", "1")
    assert len(store.prefilter("zz", {}, NOW)) == 1
    monkeypatch.setenv("SKMEMORY_PREFILTER_DEADLINE_S", "0")
    assert store.prefilter("zz", {}, NOW) == []


def test_long_prompt_scans_head_and_tail_only(tmp_path):
    store = IntentionStore(tmp_path)
    for iid, term in (("h", "headterm"), ("t", "tailterm"), ("m", "midterm")):
        store.save(Intention(id=iid, kind="event", title="t", state="armed", trigger_terms=[term]))
    text = "headterm " + "x " * 200_000 + "midterm " + "y " * 200_000 + " tailterm"
    assert sorted(i.id for i in store.prefilter(text, {}, NOW, max_fires=9)) == ["h", "t"]


def test_one_mb_prompt_under_50ms_at_1000_intentions(tmp_path):
    import statistics
    import time

    store = _many(tmp_path, 1000, "term{n} alpha", scope={"repo": "r"}, max_fires=5)
    store.close()
    fresh = IntentionStore(tmp_path)
    text = ("lorem ipsum dolor sit amet confusion " * 30000)[:1_000_000]
    times = []
    for _ in range(15):
        t = time.perf_counter()
        fresh.prefilter(text, {"repo": "r"}, NOW)
        times.append((time.perf_counter() - t) * 1000)
    med = statistics.median(times)
    print(f"1 MB prompt, 1000 intentions: median {med:.2f} ms, max {max(times):.2f} ms")
    assert med < 50.0


def test_matcher_cache_follows_index_changes(tmp_path):
    a = IntentionStore(tmp_path)
    b = IntentionStore(tmp_path)
    assert b.prefilter("late", {}, NOW) == []  # warms b's cache
    a.save(Intention(id="n", kind="event", title="t", state="armed", trigger_terms=["late"]))
    assert [i.id for i in b.prefilter("late", {}, NOW)] == ["n"]  # other process's write seen
    b.apply("n", "cancel")
    assert b.prefilter("late", {}, NOW) == []


def test_overlapping_and_prefix_terms_both_match(tmp_path, monkeypatch):
    store = IntentionStore(tmp_path)
    store.save(
        Intention(id="a", kind="event", title="t", state="armed", trigger_terms=["rollout"])
    )
    store.save(
        Intention(id="b", kind="event", title="t", state="armed", trigger_terms=["rollout ring"])
    )
    store.save(
        Intention(id="c", kind="event", title="t", state="armed", trigger_terms=["ring now"])
    )
    got = sorted(i.id for i in store.prefilter("the rollout ring now", {}, NOW, max_fires=9))
    assert got == ["a", "b", "c"]


def test_cut_inside_a_word_never_matches_partial_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_PREFILTER_MAX_CHARS", "20")
    store = IntentionStore(tmp_path)
    store.save(Intention(id="f", kind="event", title="t", state="armed", trigger_terms=["fusion"]))
    # tail cut lands inside "confusion" leaving "fusion"; head cut leaves "fusi"/"fusion" stub
    assert store.prefilter("a" * 40 + " confusion", {}, NOW) == []
    assert store.prefilter("xxxfusion " + "a" * 40, {}, NOW) == []
    assert [i.id for i in store.prefilter("fusion " + "a" * 40, {}, NOW)] == ["f"]
    assert [i.id for i in store.prefilter("a" * 40 + " fusion", {}, NOW)] == ["f"]


def test_head_cut_stub_does_not_match(tmp_path, monkeypatch):
    monkeypatch.setenv("SKMEMORY_PREFILTER_MAX_CHARS", "40")
    store = IntentionStore(tmp_path)
    store.save(Intention(id="s", kind="event", title="t", state="armed", trigger_terms=["fusi"]))
    text = "a" * 15 + " fusion " + "b" * 60  # head (20 chars) ends exactly inside "fusion"
    assert store.prefilter(text, {}, NOW) == []
