"""Intention model, code-owned lifecycle, flat-file store and supersession sweep (SKMEM-P2-01)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from skmemory.eval import fixtures as fx
from skmemory.intentions import (
    Intention,
    IntentionStore,
    transition,
)

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


def test_reindex_skips_conflict_copies_and_canonical_wins(tmp_path):
    store = IntentionStore(tmp_path)
    store.save(Intention(id="a1", kind="time", title="canonical", state="done"))
    conflict = Intention(id="a1", kind="time", title="conflict", state="pending")
    (tmp_path / "intentions" / "a1.sync-conflict-20261101-120000-ABCDEFG.json").write_text(
        conflict.model_dump_json()
    )
    assert store.reindex() == 1
    assert store.last_reindex_report["conflicts"] == [
        "a1.sync-conflict-20261101-120000-ABCDEFG.json"
    ]
    got = store.list()
    assert [(i.id, i.title, i.state) for i in got] == [("a1", "canonical", "done")]


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
