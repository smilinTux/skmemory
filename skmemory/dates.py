"""Ground relative dates in saved text against an event time.

Port of skwhisper/dates.py (skwhisper PR 11); skmemory must not import
skwhisper, so the resolver is duplicated and kept in parity by
tests/test_relative_dates.py. Appends the resolved ISO date in parentheses
after each relative phrase, keeping the original words. It resolves in the
timestamp's own UTC offset, never reads the wall clock or a file mtime, leaves
code spans, URLs and double-quoted text alone, and is idempotent.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
_WD = "(?:" + "|".join(w.capitalize() for w in _WEEKDAYS) + ")"

# Phrases that put a bare "on Friday" in the future rather than the past.
_FUTURE_RE = re.compile(
    r"\b(will|won't|going to|plans? to|planned|scheduled|due|upcoming|next steps?|"
    r"to be|need to|needs to|expects?|expected|by)\b|'ll\b",
    re.IGNORECASE,
)

# Spans never rewritten: fenced code, inline code, URLs, double-quoted text.
_PROTECTED_RE = re.compile(
    r"```.*?```|`[^`\n]*`|https?://\S+|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d",
    re.DOTALL,
)

_PHRASE_RE = re.compile(
    r"\b(?P<phrase>"
    r"day before yesterday|day after tomorrow|yesterday|tomorrow|today|tonight|last night|"
    r"last week|next week|last month|next month|"
    r"(?:last|next|this|on)\s+" + _WD + r")\b(?!['\u2019]s\b)(?!\s*\([^)\n]*\d[^)\n]*\))",
    re.IGNORECASE,
)


def parse_event_at(value) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime, keeping its own UTC offset.

    The calendar date of a conversation is the date where it happened, so the
    offset is never converted away (23:30-08:00 is still that evening). A naive
    timestamp is read in the host's local zone.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def event_date(event_at: datetime) -> date:
    """Calendar date in the timestamp's own offset."""
    return event_at.date()


def _weekday_date(base, wd: int, mode: str, sentence: str):
    cur = base.weekday()
    if mode == "last":
        delta = -((cur - wd) % 7 or 7)
    elif mode == "next":
        delta = (wd - cur) % 7 or 7
    elif mode == "this":
        delta = wd - cur  # same Monday-Sunday week
    else:  # bare "on <weekday>": tense decides
        if _FUTURE_RE.search(sentence):
            delta = (wd - cur) % 7 or 7
        else:
            delta = -((cur - wd) % 7 or 7)
    return base + timedelta(days=delta)


def _resolve(phrase: str, base, sentence: str) -> str | None:
    p = phrase.lower()
    day = {
        "yesterday": -1,
        "last night": -1,
        "today": 0,
        "tonight": 0,
        "tomorrow": 1,
        "day before yesterday": -2,
        "day after tomorrow": 2,
    }
    if p in day:
        return (base + timedelta(days=day[p])).isoformat()
    monday = base - timedelta(days=base.weekday())
    if p == "last week":
        return f"week of {(monday - timedelta(days=7)).isoformat()}"
    if p == "next week":
        return f"week of {(monday + timedelta(days=7)).isoformat()}"
    if p in ("last month", "next month"):
        step = -1 if p == "last month" else 1
        idx = base.year * 12 + base.month - 1 + step
        return f"{idx // 12:04d}-{idx % 12 + 1:02d}"
    mode, _, name = p.partition(" ")
    return _weekday_date(base, _WEEKDAYS.index(name), mode, sentence).isoformat()


def resolve_relative_dates(text: str, event_at: datetime | None) -> str:
    """Append "(ISO date)" after each relative date phrase, anchored at event_at."""
    if not text or event_at is None:
        return text
    base = event_date(event_at)
    spans = [m.span() for m in _PROTECTED_RE.finditer(text)]
    out, last = [], 0
    for m in _PHRASE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        start = text.rfind(".", 0, m.start()) + 1
        end = text.find(".", m.end())
        sentence = text[start : end if end != -1 else len(text)]
        resolved = _resolve(m.group("phrase"), base, sentence)
        if resolved is None:
            continue
        out.append(text[last : m.end()])
        out.append(f" ({resolved})")
        last = m.end()
    out.append(text[last:])
    return "".join(out)
