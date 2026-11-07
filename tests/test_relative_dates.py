"""Relative dates resolve against event_at, never the wall clock.

Parity cases are copied from skwhisper tests/test_relative_dates.py (the
resolver in skmemory/dates.py is a port). The MCP tests cover memory_save_session.
"""

import asyncio
import json
from datetime import datetime, timezone

from skmemory import mcp_server
from skmemory.dates import event_date, parse_event_at, resolve_relative_dates

# Friday 2026-03-13, far from any plausible "now".
EVENT = datetime(2026, 3, 13, 15, 0, tzinfo=timezone.utc)


def test_day_words():
    out = resolve_relative_dates("Shipped it yesterday and will test tomorrow.", EVENT)
    assert out == "Shipped it yesterday (2026-03-12) and will test tomorrow (2026-03-14)."


def test_week_and_month():
    out = resolve_relative_dates("Last week we met; next month we ship.", EVENT)
    assert "Last week (week of 2026-03-02)" in out
    assert "next month (2026-04)" in out


def test_weekdays():
    assert "last Monday (2026-03-09)" in resolve_relative_dates("Met last Monday.", EVENT)
    assert "next Monday (2026-03-16)" in resolve_relative_dates("See you next Monday.", EVENT)
    assert "on Tuesday (2026-03-10)" in resolve_relative_dates("They called on Tuesday.", EVENT)
    assert "on Tuesday (2026-03-17)" in resolve_relative_dates(
        "The demo will be on Tuesday.", EVENT
    )


def test_idempotent_and_case_preserved():
    once = resolve_relative_dates("Yesterday it broke.", EVENT)
    assert once == "Yesterday (2026-03-12) it broke."
    assert resolve_relative_dates(once, EVENT) == once


def test_no_event_leaves_text_alone():
    assert resolve_relative_dates("yesterday", None) == "yesterday"


def test_event_date_keeps_own_offset():
    from datetime import date

    west = parse_event_at("2026-03-13T23:30:00-08:00")  # 07:30 UTC next day
    assert event_date(west) == date(2026, 3, 13)
    assert "yesterday (2026-03-12)" in resolve_relative_dates("yesterday", west)
    east = parse_event_at("2026-03-14T00:10:00+09:00")  # 15:10 UTC previous day
    assert event_date(east) == date(2026, 3, 14)
    assert "yesterday (2026-03-13)" in resolve_relative_dates("yesterday", east)


def test_naive_timestamp_uses_its_own_date():

    assert event_date(parse_event_at("2026-03-13T23:30:00")).isoformat() == "2026-03-13"


def test_guards_leave_code_urls_quotes_alone():
    for t in [
        "run `echo yesterday` now",
        "```\nsleep until tomorrow\n```",
        "see https://x.io/yesterday/last-week",
        'he said "see you tomorrow" then left',
        "he said \u201csee you tomorrow\u201d then left",
    ]:
        assert resolve_relative_dates(t, EVENT) == t, t
    assert "tomorrow (2026-03-14)" in resolve_relative_dates('"q" and tomorrow', EVENT)


def test_possessive_left_alone():
    t = "Reviewed last week's PR and yesterday\u2019s fix."
    assert resolve_relative_dates(t, EVENT) == t


def test_existing_parenthetical_date_not_doubled():
    for t in ["fixed yesterday (March 12) ok", "fixed yesterday (2026-03-12) ok"]:
        assert resolve_relative_dates(t, EVENT) == t


def test_should_is_not_future():
    assert "on Tuesday (2026-03-10)" in resolve_relative_dates(
        "It should have run on Tuesday.", EVENT
    )


class _Mem:
    id = "m1"
    title = "t"


class _FakeStore:
    def __init__(self):
        self.calls = []

    def snapshot(self, **kw):
        self.calls.append(kw)
        return _Mem()


SESSION = (
    "We decided to ship the gateway change yesterday after a long review of the "
    "rollout plan and the follow up work. " * 3
)


def _save(monkeypatch, **args):
    store = _FakeStore()
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    out = asyncio.run(mcp_server.call_tool("memory_save_session", args))
    return store, json.loads(out[0].text)


def test_save_session_resolves_against_event_at(monkeypatch):
    store, out = _save(monkeypatch, conversation=SESSION, event_at="2026-03-13T23:30:00-08:00")
    assert out["saved"] >= 1
    for kw in store.calls:
        assert "yesterday (2026-03-12)" in kw["content"]
        assert kw["event_at"] == "2026-03-13T23:30:00-08:00"


def test_save_session_without_event_at_is_unchanged(monkeypatch):
    store, out = _save(monkeypatch, conversation=SESSION)
    assert out["saved"] >= 1
    for kw in store.calls:
        assert "(2026" not in kw["content"]
        assert "event_at" not in kw


def test_save_session_invalid_event_at_is_unchanged(monkeypatch):
    store, _ = _save(monkeypatch, conversation=SESSION, event_at="not a date")
    for kw in store.calls:
        assert "(2026" not in kw["content"]
        assert "event_at" not in kw


def test_snapshot_sets_event_at(tmp_path, monkeypatch):
    from skmemory.backends.sqlite_backend import SQLiteBackend
    from skmemory.store import MemoryStore

    s = MemoryStore(primary=SQLiteBackend(base_path=str(tmp_path)))
    m = s.snapshot("t", "a unique body for event at", event_at="2026-03-13T15:00:00+00:00")
    assert m.event_at == "2026-03-13T15:00:00+00:00"
    m2 = s.snapshot("t2", "another unique body")
    assert m2.event_at == m2.created_at


def test_weekday_phrase_with_extra_whitespace():
    assert "last  Monday (2026-03-09)" in resolve_relative_dates("Met last  Monday.", EVENT)
    assert "next\nMonday (2026-03-16)" in resolve_relative_dates("See you next\nMonday.", EVENT)
    assert "on \t Tuesday (2026-03-10)" in resolve_relative_dates(
        "They called on \t Tuesday.", EVENT
    )


def test_save_session_resolver_failure_still_saves_original(monkeypatch):
    import skmemory.dates as dates

    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(dates, "resolve_relative_dates", boom)
    store, out = _save(monkeypatch, conversation=SESSION, event_at="2026-03-13T15:00:00Z")
    assert out["saved"] == out["extracted"] >= 1
    for kw in store.calls:
        assert "(2026" not in kw["content"]


def _real_store(tmp_path):
    from skmemory.backends.sqlite_backend import SQLiteBackend
    from skmemory.store import MemoryStore

    return MemoryStore(primary=SQLiteBackend(base_path=str(tmp_path)))


def test_event_at_carried_through_ingest_path(tmp_path):
    s = _real_store(tmp_path)
    body = "Alpha beta gamma delta. " * 70  # >= 1200, <= 10000
    assert 1200 <= len(body) <= 10000
    ev = "2026-03-13T15:00:00+00:00"
    parent = s.snapshot("long", body, event_at=ev)
    assert parent.event_at == ev
    kids = [s.primary.load(i) for i in parent.metadata["chunk_memory_ids"]]
    assert kids and all(k.event_at == ev for k in kids)


def test_event_at_carried_through_split_path(tmp_path):
    s = _real_store(tmp_path)
    s.content_overflow_strategy = "split"
    s.decompose_min_length = 10**9
    s.max_content_length = 500
    body = "Alpha beta gamma delta. " * 100
    ev = "2026-03-13T15:00:00+00:00"
    parent = s.snapshot("huge", body, event_at=ev)
    assert parent.event_at == ev
    kids = [s.primary.load(i) for i in parent.metadata["split_children"]]
    assert len(kids) > 1 and all(k.event_at == ev for k in kids)
