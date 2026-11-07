"""Validity-window helpers for as_of retrieval (SKMEM-P3-03).

One definition of "valid at T" shared by every arm and by the hydrated-row
safety net, so the SQL, Cypher and Python forms cannot drift apart:

    coalesce(valid_from, created_at) <= T  AND  (valid_to IS NULL OR valid_to > T)

* A v1 row has no ``valid_from``: its ``created_at`` stands in.
* ``valid_to`` null (or empty, or unparseable) means the fact is still open.
* The upper bound is exclusive: a fact closed AT T is already closed at T.
* ``as_of=None`` means no temporal filter at all (see ``MemoryStore.search``).
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or None if it cannot."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def normalize_as_of(value: str | datetime | None) -> str | None:
    """Canonical UTC ISO string (the format memories stamp), or None for no filter.

    A naive time or a bare date is read as UTC (a date is its 00:00). Raises
    ValueError on anything unparseable so a typo is an error, not an unfiltered
    search.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    dt = parse_ts(value)
    if dt is None:
        raise ValueError(f"as_of is not an ISO-8601 timestamp: {value!r}")
    return dt.isoformat()


def valid_at(mem: object, as_of: str | datetime) -> bool:
    """Whether ``mem`` is valid at ``as_of`` (same rule as the SQL/Cypher forms)."""
    t = parse_ts(as_of)
    if t is None:
        return True
    start = parse_ts(getattr(mem, "valid_from", None)) or parse_ts(
        getattr(mem, "created_at", None)
    )
    if start is not None and start > t:
        return False
    end = parse_ts(getattr(mem, "valid_to", None))
    return not (end is not None and end <= t)
