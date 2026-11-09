"""Collapse a promoted memory and its source into one search hit.

Promotion is a copy, not a move (``Memory.promote``): the original stays in its
layer and the copy, whose ``parent_id`` points at it, lands one layer up. Both
match the same query, so a page showed every promoted memory twice, once per
layer. On lumina that was all 1137 promotions, byte-identical pairs
(2026-11-09).

Storage keeps both. Only the result page collapses each chain to its highest
open copy, at the best rank any member had. A source whose copy is not on the
page is kept, and a copy whose validity is closed never stands in for an open
source: nothing is hidden that the page would not show anyway.
"""

from __future__ import annotations

from typing import Any

from .models import RELATION_PROMOTED_FROM, parent_relation_of

_LAYER_RANK = {"short-term": 0, "mid-term": 1, "long-term": 2}


def _layer_rank(m: Any) -> int:
    layer = getattr(m, "layer", None)
    return _LAYER_RANK.get(getattr(layer, "value", layer), -1)


def _is_open_promoted_copy(m: Any) -> bool:
    # getattr: callers also pass duck-typed hits; one without lineage is no copy.
    parent = getattr(m, "parent_id", None)
    if not parent or getattr(m, "valid_to", None):
        return False
    # The project's resolver: a bare parent_id from before parent_relation
    # existed is a promotion copy unless it is a chunk.
    return parent_relation_of(parent, getattr(m, "metadata", None)) == RELATION_PROMOTED_FROM


def collapse_promoted_pairs(rows: list[Any]) -> list[Any]:
    """Return ``rows`` with each promotion chain on the page shown once.

    Args:
        rows: Ranked hits, best first.

    Returns:
        The same ranking, where a source and its open promoted copies (short
        to mid to long) share one slot: the highest copy on the page, at the
        best rank any member of the chain had.
    """
    on_page = {m.id: m for m in rows}
    copies: dict[str, list[Any]] = {}
    for m in rows:
        if _is_open_promoted_copy(m):
            copies.setdefault(m.parent_id, []).append(m)
    if not copies:
        return rows

    def base(m: Any) -> str:
        seen = {m.id}
        while _is_open_promoted_copy(m) and m.parent_id in on_page and m.parent_id not in seen:
            m = on_page[m.parent_id]
            seen.add(m.id)
        return m.id

    def top(mem_id: str) -> Any:
        best, frontier, seen = on_page[mem_id], [mem_id], {mem_id}
        while frontier:
            for c in copies.get(frontier.pop(), []):
                if c.id in seen:
                    continue
                seen.add(c.id)
                frontier.append(c.id)
                if _layer_rank(c) > _layer_rank(best):
                    best = c
        return best

    out: list[Any] = []
    done: set[str] = set()
    for m in rows:
        key = base(m)
        if key in done:
            continue
        done.add(key)
        out.append(top(key))
    return out


def finish_page(rows: list[Any], limit: int) -> list[Any]:
    """Collapse promotion chains, then apply the tier cap at ``limit``.

    Every search path ends here (Python, Rust, and the Rust shadow), each from
    a pool over-fetched past ``limit``, so a collapsed pair never shortens the
    page and the two engines are compared on the same final step.
    """
    from .tiers import apply_tier_cap

    rows = collapse_promoted_pairs(rows)
    by_id = {m.id: m for m in rows}
    return [by_id[i] for i in apply_tier_cap([m.id for m in rows], by_id, limit)]
