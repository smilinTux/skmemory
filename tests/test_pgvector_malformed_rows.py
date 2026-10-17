"""One malformed row must not kill an entire search (and must not be silent).

Measured on the live store 2026-10-17: 17 of lumina's 20,299 rows and 13 of
jarvis's 856 have a ``memory_json`` with no ``title``. ``Memory`` requires
``title`` and ``content``, so the previous unguarded
``[Memory.model_validate_json(...) for r in rows]`` raised ValidationError and
destroyed the whole result set whenever a query's top-k touched one of them.

These tests need no database: they drive the row-parsing seam directly with
rows shaped exactly as psycopg returns them.
"""

from __future__ import annotations

import json

from skmemory.backends.pgvector_backend import PGVectorBackend


def _backend() -> PGVectorBackend:
    """A backend instance without touching __init__ (no DSN, no profile, no DB).

    ``_rows_to_memories`` only needs ``agent`` and the seen-set, so bypassing
    construction keeps this a pure unit test of the parsing seam.
    """
    backend = object.__new__(PGVectorBackend)
    backend.agent = "test-agent"
    backend._unparseable_seen = set()
    return backend


def _row(payload: dict) -> tuple[str]:
    return (json.dumps(payload),)


GOOD = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": "a good memory",
    "content": "it has every required field",
    "layer": "short-term",
    "role": "general",
    "tags": [],
    "source": "test",
    "created_at": "2026-10-17T00:00:00+00:00",
}


def _good(idx: int) -> dict:
    out = dict(GOOD)
    out["id"] = f"{idx:08d}-1111-1111-1111-111111111111"
    out["title"] = f"good memory {idx}"
    return out


def test_a_good_batch_parses_completely():
    rows = [_row(_good(i)) for i in range(5)]
    assert len(_backend()._rows_to_memories(rows)) == 5


def test_one_row_missing_title_does_not_lose_the_others():
    """The whole point: 1 bad row out of 20,000 must not deny service."""
    broken = {k: v for k, v in _good(99).items() if k != "title"}
    rows = [_row(_good(0)), _row(broken), _row(_good(1))]

    parsed = _backend()._rows_to_memories(rows)

    assert len(parsed) == 2
    assert [m.title for m in parsed] == ["good memory 0", "good memory 1"]


def test_one_row_missing_content_is_skipped_too():
    broken = {k: v for k, v in _good(98).items() if k != "content"}
    parsed = _backend()._rows_to_memories([_row(_good(0)), _row(broken)])
    assert len(parsed) == 1


def test_the_skip_is_recorded_not_silent(caplog):
    """A quiet skip would just trade a loud failure for a silent one."""
    broken = {k: v for k, v in _good(97).items() if k != "title"}
    backend = _backend()

    with caplog.at_level("WARNING"):
        backend._rows_to_memories([_row(broken)])

    assert backend._unparseable_seen, "the skipped row id was not recorded"
    assert any("unparseable" in rec.message.lower() for rec in caplog.records), (
        "nothing was logged for the skipped row"
    )


def test_the_recorded_id_identifies_the_offending_row():
    broken = {k: v for k, v in _good(96).items() if k != "title"}
    backend = _backend()
    backend._rows_to_memories([_row(broken)])
    assert broken["id"] in backend._unparseable_seen


def test_an_unidentifiable_row_is_still_skipped_rather_than_raising():
    """Garbage with no id at all must not raise either."""
    backend = _backend()
    assert backend._rows_to_memories([("{not json at all",)]) == []
    assert backend._unparseable_seen == {"<unknown>"}


def test_an_all_bad_batch_returns_empty_rather_than_raising():
    broken = {k: v for k, v in _good(95).items() if k != "title"}
    assert _backend()._rows_to_memories([_row(broken), _row(broken)]) == []


def test_count_unparseable_reports_minus_one_when_it_cannot_ask():
    """A failed probe must be distinguishable from a genuine zero.

    Returning 0 on an unreachable database would read as "nothing wrong",
    which is exactly the measurement-looks-like-success defect this guards.
    """
    backend = object.__new__(PGVectorBackend)
    backend.agent = "test-agent"
    backend._unparseable_seen = set()

    def _boom():
        raise RuntimeError("no database here")

    backend._connection = _boom
    assert backend.count_unparseable() == -1
