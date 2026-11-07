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


def _hit(tags: Iterable[str] | None, stems: tuple[str, ...]) -> bool:
    return any(s in str(t).lower() for t in (tags or ()) for s in stems)


def is_sensitive(tags: Iterable[str] | None) -> bool:
    """True when any tag contains a sensitive stem."""
    return _hit(tags, SENSITIVE_STEMS)


def is_owner_only(tags: Iterable[str] | None) -> bool:
    """True when any tag contains an owner-only stem."""
    return _hit(tags, OWNER_ONLY_STEMS)


def sql_not_sensitive(column: str = "tags") -> tuple[list[str], list[str]]:
    """WHERE clauses and params excluding rows whose CSV ``column`` has a stem."""
    return (
        [f"instr(lower({column}), ?) = 0" for _ in SENSITIVE_STEMS],
        list(SENSITIVE_STEMS),
    )
