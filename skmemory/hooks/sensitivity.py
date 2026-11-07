# SPDX-License-Identifier: GPL-3.0-or-later
"""One source of truth for which memory tags are sensitive (SKMEM-P1-15).

Stdlib only and living in ``skmemory/hooks/`` on purpose: ``user_prompt_fast.py``
loads ``query_slice`` (and so this file) by path under ``python -S``, so it must
not import the skmemory package. Matching is by STEM, a case-insensitive substring of the tag, so plurals
and variants ("Medical", "legal-docs", "family-private-2") are covered.

Two tiers:

``SENSITIVE_STEMS``   kept out of every OWNER auto-inject path (session ritual,
                      ``memory_context``, ``perform_ritual``, the prompt hook).
                      The owner still pulls them explicitly with
                      ``memory_search`` / ``memory_recall``.
``OWNER_ONLY_STEMS``  never visible to any declared non-owner reader, whatever
                      the memory's ``context_tag`` says (enforced in
                      ``audience.ReaderScope.allows``). The other sensitive
                      stems keep ``context_tag`` semantics: a ``@work:acme``
                      row tagged ``medical`` stays visible to that client.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterable

# A superset of eval.harvest.SENSITIVE_TAGS / SENSITIVE_TAG_SUBSTRINGS (pinned by
# tests/test_query_slice.py).
SENSITIVE_STEMS = (
    "medical",
    "health",
    "legal",
    "sacred",
    "private",
    "personal",
    "financ",
    "family-private",
    "grief",
    "therap",
    "sarah",
    "bereave",
    "diagnos",
    "medication",
    "nootropic",
)

OWNER_ONLY_STEMS = ("sacred", "grief", "family-private", "personal-history")

# Common look-alikes folded to ASCII before matching (Cyrillic and Greek).
_HOMOGLYPHS = str.maketrans(
    {
        "\u0430": "a",  # Cyrillic a
        "\u0435": "e",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0443": "y",
        "\u0445": "x",
        "\u0456": "i",
        "\u0455": "s",
        "\u0458": "j",
        "\u03bf": "o",  # Greek omicron
        "\u03b1": "a",
        "\u03b5": "e",
        "\u03c1": "p",
    }
)
_SEPARATORS = re.compile(r"[-_\s.]+")


def normalize(text: object) -> str:
    """NFKC, casefold, fold homoglyphs, then drop ``- _ . whitespace``.

    So ``Family_Private``, ``family private``, ``familyprivate``, fullwidth
    ``sacred`` and Cyrillic-a ``sacred`` all compare equal to the plain stem.
    """
    t = unicodedata.normalize("NFKC", str(text)).casefold().translate(_HOMOGLYPHS)
    return _SEPARATORS.sub("", t)


_NORM_SENSITIVE = tuple(normalize(x) for x in SENSITIVE_STEMS)
_NORM_OWNER_ONLY = tuple(normalize(x) for x in OWNER_ONLY_STEMS)


def _hit(tags: Iterable[str] | None, stems: tuple[str, ...]) -> bool:
    if tags is None:
        return False
    if isinstance(tags, str):
        tags = [tags]
    return any(s in n for n in (normalize(t) for t in tags) for s in stems)


def is_sensitive(tags: Iterable[str] | None) -> bool:
    """True when any tag contains a sensitive stem (after normalization)."""
    return _hit(tags, _NORM_SENSITIVE)


def is_owner_only(tags: Iterable[str] | None) -> bool:
    """True when any tag contains an owner-only stem (after normalization)."""
    return _hit(tags, _NORM_OWNER_ONLY)


def _csv_sensitive(csv: object) -> int:
    return int(is_sensitive(str(csv or "").split(",")))


def register_sql(conn: sqlite3.Connection) -> None:
    """Install ``sk_sensitive(tags_csv)`` on a connection (needed by ``sql_not_sensitive``)."""
    conn.create_function("sk_sensitive", 1, _csv_sensitive, deterministic=True)


def sql_not_sensitive(column: str = "tags") -> tuple[list[str], list[str]]:
    """WHERE clauses and params excluding rows whose CSV ``column`` is sensitive.

    SQL cannot normalize homoglyphs, so this calls the Python function
    registered by :func:`register_sql`: the caller MUST register it on the
    connection first.
    """
    return ([f"sk_sensitive({column}) = 0"], [])
