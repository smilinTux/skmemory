"""Failure procedures as warnings (SKMEM-P4-05).

A ``kind="procedure"`` memory with ``outcome="failure"`` records a way that did
NOT work. It is driven by fields only: stored content is never edited.

* Index line: :func:`index_title` prefixes ``WARNING:`` to the displayed title.
* Ranking rule (:func:`order_procedures`): for a how-to query, a failure
  procedure never ranks above a successful procedure in the same result list.
  It is reordered, never dropped, and never filtered as low importance. If the
  query asks about failure (``should I``, ``fail``, ``avoid``, ``went wrong``,
  ...), or is not a how-to query, order is left exactly as retrieved.

Runs after the reader filter, so it only reorders what the reader may see.
"""

from __future__ import annotations

import re

WARNING_PREFIX = "WARNING: "

_HOWTO = re.compile(
    r"\b(how\s+(do|can|should|to|would)\b|steps?\s+(to|for)\b|procedure|runbook|"
    r"instructions?\s+(to|for)|way\s+to|recover|restore|fix)",
    re.I,
)
_FAILURE_ASK = re.compile(
    r"\b(fail\w*|warning|avoid|mistake|went\s+wrong|broke\w*|should\s+i|shouldn'?t|"
    r"don'?t|do\s+not|why\s+did|didn'?t\s+work|not\s+work\w*|risk\w*|danger\w*)\b",
    re.I,
)


def is_failure_procedure(mem) -> bool:
    return getattr(mem, "kind", None) == "procedure" and getattr(mem, "outcome", None) == "failure"


def index_title(mem_or_kind, title: str | None = None, outcome: str | None = None) -> str:
    """Title for an index line. Accepts a memory, or ``(kind, title, outcome)``."""
    if title is None:
        mem = mem_or_kind
        title, kind, outcome = mem.title, mem.kind, mem.outcome
    else:
        kind = mem_or_kind
    if kind == "procedure" and outcome == "failure" and not title.startswith(WARNING_PREFIX):
        return WARNING_PREFIX + title
    return title


def is_howto_query(query: str) -> bool:
    return bool(_HOWTO.search(query or "")) and not _FAILURE_ASK.search(query or "")


def order_procedures(memories: list, query: str) -> list:
    """Procedure slots are refilled successes first, then failures (stable)."""
    if not is_howto_query(query):
        return memories
    slots = [i for i, m in enumerate(memories) if getattr(m, "kind", None) == "procedure"]
    procs = [memories[i] for i in slots]
    ordered = [m for m in procs if not is_failure_procedure(m)] + [
        m for m in procs if is_failure_procedure(m)
    ]
    out = list(memories)
    for i, m in zip(slots, ordered, strict=True):
        out[i] = m
    return out
