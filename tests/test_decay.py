"""SKMEM-P0-03: importance written at save, decay channels, search ordering."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skmemory import decay
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.eval import fixtures as fx
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.store import MemoryStore

CHANNELS = ("none", "recency", "reinforcement", "affect_weighted")


def _fixture_scores(channel):
    d = fx.load_decay()
    as_of = decay.parse_ts(d.as_of)
    items = [it.model_dump() for it in d.items]
    scores = {
        it["id"]: decay.decay_score(
            channel,
            as_of=as_of,
            created_at=it["created_at"],
            last_accessed=it["last_accessed"],
            access_count=it["access_count"],
            intensity=it["intensity"],
            kind=it["kind"],
            fact_half_life_days=d.channels.fact_half_life_days,
            gist_half_life_days=d.channels.gist_half_life_days,
            affect_floor=d.channels.affect_floor,
        )
        for it in items
    }
    return items, scores


@pytest.mark.parametrize("channel", CHANNELS)
def test_fixture_order_and_scores_match_formulas(channel):
    d = fx.load_decay()
    items, scores = _fixture_scores(channel)
    assert decay.order_by_score(items, scores) == getattr(d.expected_order, channel)
    for i, want in d.expected_scores[channel].items():
        assert scores[i] == pytest.approx(want, abs=1e-6)


@pytest.mark.parametrize("channel", CHANNELS)
def test_d2_above_d1_every_channel(channel):
    items, scores = _fixture_scores(channel)
    order = decay.order_by_score(items, scores)
    assert order.index("d2") < order.index("d1")


def test_affect_floor_lifts_d3_and_ties_hold():
    _, aff = _fixture_scores("affect_weighted")
    _, rein = _fixture_scores("reinforcement")
    assert rein["d1"] == rein["d3"]
    assert aff["d3"] == pytest.approx(0.15 * 1.95)
    assert aff["d3"] > aff["d1"]


def test_unknown_channel_raises():
    with pytest.raises(ValueError):
        decay.decay_score("bogus", as_of=datetime.now(timezone.utc), created_at=None)


@pytest.mark.parametrize(
    "raw,want",
    [
        (None, None),
        ("1", "affect_weighted"),
        ("on", "affect_weighted"),
        ("0", None),
        ("off", None),
        ("recency", "recency"),
        ("none", None),
        ("garbage", None),
    ],
)
def test_configured_channel(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("SKMEMORY_DECAY", raising=False)
    else:
        monkeypatch.setenv("SKMEMORY_DECAY", raw)
    assert decay.configured_channel() == want


# ------------------------------------------------------------- importance


@pytest.fixture
def backend(tmp_path):
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def store(backend):
    return MemoryStore(primary=backend)


def _imp(backend, mid):
    return (
        backend._get_conn().execute("SELECT importance FROM memories WHERE id = ?", (mid,))
    ).fetchone()["importance"]


def test_importance_set_from_layer_and_intensity(store, backend):
    lo = store.snapshot(title="calm note", content="plain calm body", layer=MemoryLayer.SHORT)
    hi = store.snapshot(
        title="big moment",
        content="a very intense body",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=9.0),
    )
    lt = store.snapshot(title="durable", content="long term body", layer=MemoryLayer.LONG)
    assert _imp(backend, lo.id) == pytest.approx(0.3)
    assert _imp(backend, hi.id) == pytest.approx(0.3 + 0.3 * 0.9)
    assert _imp(backend, lt.id) == pytest.approx(0.7)


def test_importance_not_in_flat_file(store, backend):
    m = store.snapshot(
        title="flat check",
        content="flat body",
        emotional=EmotionalSnapshot(intensity=8.0),
    )
    path = backend._find_file(m.id)
    raw = json.loads(path.read_text())
    assert "importance" not in raw
    assert "importance" not in (raw.get("metadata") or {})


def test_resave_keeps_reinforced_importance(store, backend):
    m = store.snapshot(title="keep me", content="keep body")
    store.recall(m.id)
    raised = _imp(backend, m.id)
    assert raised == pytest.approx(0.3 + 0.02 * 0.7)
    backend.save(m)
    assert _imp(backend, m.id) == pytest.approx(raised)


def test_reinforcement_raises_importance_capped(store, backend):
    m = store.snapshot(title="cap", content="cap body", layer=MemoryLayer.LONG)
    conn = backend._get_conn()
    conn.execute("UPDATE memories SET importance = 0.99 WHERE id = ?", (m.id,))
    conn.commit()
    store.recall(m.id)
    assert _imp(backend, m.id) == pytest.approx(0.99 + 0.02 * 0.01)
    assert _imp(backend, m.id) < 1.0


# ------------------------------------------------------------- search order


def _two(store, backend):
    """Two equal-relevance memories; the second is old and unreinforced."""
    fresh = store.snapshot(title="zebra herd fresh", content="zebra stripes herd grazing")
    old = store.snapshot(title="zebra herd stale", content="zebra stripes herd resting")
    past = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    conn = backend._get_conn()
    conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (past, old.id))
    conn.commit()
    return fresh, old


def test_rerank_prefers_fresh_on_near_tie():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    old = Memory(
        id="a", title="a", content="a", created_at=(now - timedelta(days=400)).isoformat()
    )
    new = Memory(id="b", title="b", content="b", created_at=(now - timedelta(days=1)).isoformat())
    out = decay.rerank([old, new], {"a": {}, "b": {}}, "reinforcement", as_of=now)
    assert [m.id for m in out] == ["b", "a"]
    assert [m.id for m in decay.rerank([old, new], {}, "none", as_of=now)] == ["a", "b"]


def test_rerank_never_drops_or_adds():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ms = [
        Memory(id=str(i), title=str(i), content=str(i), created_at=now.isoformat())
        for i in range(6)
    ]
    out = decay.rerank(ms, {}, "affect_weighted", as_of=now)
    assert sorted(m.id for m in out) == sorted(m.id for m in ms)


def test_search_off_leaves_order_untouched(store, backend, monkeypatch):
    _two(store, backend)
    monkeypatch.delenv("SKMEMORY_DECAY", raising=False)
    off = [m.id for m in store.search("zebra stripes")]
    monkeypatch.setenv("SKMEMORY_DECAY", "on")
    on = [m.id for m in store.search("zebra stripes")]
    assert sorted(off) == sorted(on) and len(on) == 2
    # the stale (400 day old, unreinforced) memory is never ranked above fresh when on
    fresh_title = {m.id: m.title for m in store.search("zebra stripes")}
    assert "fresh" in fresh_title[on[0]]


def test_diminishing_returns_never_saturate(store, backend):
    m = store.snapshot(title="many hits", content="many hits body")
    vals = []
    for _ in range(40):
        backend.bump_access([m.id])
        vals.append(_imp(backend, m.id))
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))
    assert vals[-1] < 1.0
    assert vals[1] - vals[0] > vals[-1] - vals[-2]


def _legacy(backend, mid, imp=0.5, count=0):
    conn = backend._get_conn()
    conn.execute(
        "UPDATE memories SET importance = ?, access_count = ? WHERE id = ?", (imp, count, mid)
    )
    conn.commit()


def test_reindex_recomputes_legacy_but_keeps_real_state(store, backend):
    legacy = store.snapshot(
        title="legacy", content="legacy body", emotional=EmotionalSnapshot(intensity=10.0)
    )
    used = store.snapshot(
        title="used", content="used body", emotional=EmotionalSnapshot(intensity=10.0)
    )
    _legacy(backend, legacy.id)
    _legacy(backend, used.id, imp=0.5, count=3)
    backend.reindex(force=True)
    assert _imp(backend, legacy.id) == pytest.approx(0.6)
    assert _imp(backend, used.id) == pytest.approx(0.5)


def test_backfill_importance_counts_and_no_flat_write(store, backend):
    a = store.snapshot(title="a", content="a body", emotional=EmotionalSnapshot(intensity=10.0))
    b = store.snapshot(title="b", content="b body", layer=MemoryLayer.MID)
    c = store.snapshot(title="c", content="c body", emotional=EmotionalSnapshot(intensity=10.0))
    for m in (a, b, c):
        _legacy(backend, m.id)
    _legacy(backend, c.id, imp=0.5, count=2)
    before = {m.id: backend._find_file(m.id).read_bytes() for m in (a, b, c)}
    res = backend.backfill_importance(batch_size=1)
    assert res == {"total": 3, "scanned": 2, "updated": 1, "unchanged": 1}
    assert _imp(backend, a.id) == pytest.approx(0.6)
    assert _imp(backend, b.id) == pytest.approx(0.5)
    assert _imp(backend, c.id) == pytest.approx(0.5)
    assert before == {m.id: backend._find_file(m.id).read_bytes() for m in (a, b, c)}
    assert backend.backfill_importance()["updated"] == 0
