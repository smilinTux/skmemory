"""Repeating time intentions (interval_s), SKMEM-P2-06."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skmemory.intention_tick import run_tick
from skmemory.intentions import Intention, IntentionStore, transition

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
WEEK = 7 * 86400


def _rep(**kw):
    return Intention(id="wk", kind="time", title="weekly", due_at=T0, interval_s=WEEK, **kw)


def test_fire_rearms_by_one_interval():
    out = transition(_rep(), "fire", now=T0 + timedelta(minutes=5))
    assert out.state == "armed"
    assert out.due_at == T0 + timedelta(seconds=WEEK)
    assert out.fires == 1


def test_long_outage_skips_missed_intervals():
    now = T0 + timedelta(seconds=WEEK * 5 + 3600)
    out = transition(_rep(), "fire", now=now)
    assert out.due_at == T0 + timedelta(seconds=WEEK * 6)
    assert out.due_at > now
    assert out.fires == 1


def test_exactly_on_boundary_moves_strictly_past_now():
    now = T0 + timedelta(seconds=WEEK)
    out = transition(_rep(), "fire", now=now)
    assert out.due_at == T0 + timedelta(seconds=WEEK * 2)


def test_non_repeating_unchanged():
    i = Intention(id="x", kind="time", title="t", due_at=T0)
    out = transition(i, "fire", now=T0)
    assert out.state == "fired" and out.due_at == T0


def test_max_fires_stops_repeat():
    out = transition(_rep(max_fires=1), "fire", now=T0)
    assert out.state == "fired"


def test_tick_fires_once_then_waits(tmp_path):
    s = IntentionStore(tmp_path / "mem")
    s.save(_rep())
    now = T0 + timedelta(seconds=WEEK * 3)
    sent = []
    kw = dict(now=now, deliver=lambda t: sent.append(t) or True, any_host=True)
    assert run_tick(s, **kw).fired == ["wk"]
    assert run_tick(s, **kw).fired == []
    assert len(sent) == 1
    later = now + timedelta(seconds=WEEK)
    assert run_tick(s, now=later, deliver=lambda t: True, any_host=True).fired == ["wk"]
    assert s.get("wk").fires == 2
    s.close()
