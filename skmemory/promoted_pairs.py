"""Collapse a promoted memory and its source into one search hit.

Promotion is a copy, not a move (``Memory.promote``): the original stays in its
layer and the copy, with ``parent_id`` pointing at it and
``metadata.parent_relation == "promoted_from"``, lands one layer up. Both match
the same query, so a page showed every promoted memory twice, once per layer.
On lumina that was all 1137 promotions, byte-identical pairs (2026-11-09).

Storage keeps both. Only the result page collapses each pair to the promoted
copy, the higher tier, at the better rank of the two. A source whose copy is
not on the page is kept: nothing is hidden that the page would not show anyway.
"""

from __future__ import annotations

from .models import PARENT_RELATION_KEY, RELATION_PROMOTED_FROM, Memory


def _is_promoted_copy(m: Memory) -> bool:
    # getattr: callers also pass duck-typed hits; one without lineage is no copy.
    parent = getattr(m, "parent_id", None)
    meta = getattr(m, "metadata", None) or {}
    return bool(parent) and meta.get(PARENT_RELATION_KEY) == RELATION_PROMOTED_FROM


def collapse_promoted_pairs(rows: list[Memory]) -> list[Memory]:
    """Return ``rows`` with each promotion chain on the page shown once.

    Args:
        rows: Ranked hits, best first.

    Returns:
        The same ranking, where a source and its promoted copies (short to mid
        to long) share one slot: the highest copy on the page, at the best rank
        any member of the chain had.
    """
    on_page = {m.id: m for m in rows}
    copy_of = {m.parent_id: m for m in rows if _is_promoted_copy(m)}
    if not copy_of:
        return rows

    def base(m: Memory) -> str:
        seen = {m.id}
        while _is_promoted_copy(m) and m.parent_id in on_page and m.parent_id not in seen:
            m = on_page[m.parent_id]
            seen.add(m.id)
        return m.id

    def top(mem_id: str) -> Memory:
        m, seen = on_page[mem_id], {mem_id}
        while m.id in copy_of and copy_of[m.id].id not in seen:
            m = copy_of[m.id]
            seen.add(m.id)
        return m

    out: list[Memory] = []
    done: set[str] = set()
    for m in rows:
        key = base(m)
        if key in done:
            continue
        done.add(key)
        out.append(top(key))
    return out
