"""Source tiers and the retrieval occupancy cap (SKMEM-P3-06).

Provenance decides how much of a result page a memory may occupy. Content that
a web fetch or the agent itself produced can be steered by an attacker (a page
that lexically matches a query), so at most :data:`CAP_FRACTION` of the returned
slots go to those tiers WHEN higher-tier hits exist to fill the rest.

The cap is an occupancy limit, never a filter. If the pool holds no higher-tier
hit, or too few, capped hits backfill the remaining slots in rank order: the
only correct answer is never hidden because of where it came from.

Legacy rows
-----------
A row written before schema v2 reports ``schema_version`` 1 and carries the
schema DEFAULT ``self-generated``, which is a placeholder, not a claim. Those
rows are NOT capped: real user memories predate tiers, and labelling them all
untrusted would suppress them. The single exception is a v1 row whose ``source``
names a known generator (skwhisper digests write flat v1 JSON): it is treated
as ``self-generated`` here, because that provenance is known, not assumed.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .models import SourceTier

CAPPED_TIERS: frozenset[str] = frozenset({"web", "self-generated"})
CAP_FRACTION = 0.4

#: Source label -> tier, for write paths that do not pass an explicit tier.
_USER_SOURCES = frozenset(
    {"manual", "mcp", "cli", "seed", "telegram", "notion", "convo", "hook", "shared"}
)
_TOOL_SOURCES = frozenset({"tool", "tool-output", "command", "shell", "task-pack"})
_GENERATED_SOURCES = frozenset(
    {
        "skwhisper",
        "journal-synthesis",
        "dreaming-engine",
        "consolidation",
        "claude-code-hook",
        "mcp:memory_save_session",
        "mcp:memory_pre_compact",
        "observation",
    }
)


def tier_for_source(source: str | None) -> SourceTier:
    """Tier for a ``source`` label. Unknown labels are ``self-generated``."""
    s = (source or "").strip().lower()
    if s in _GENERATED_SOURCES:
        return "self-generated"
    if s in _TOOL_SOURCES:
        return "tool-output"
    if s in _USER_SOURCES or s.startswith("import"):
        return "user-stated"
    if s.startswith(("web", "http", "fetch", "browser", "search")):
        return "web"
    return "self-generated"


_RANK = {"user-stated": 3, "tool-output": 2, "web": 1, "self-generated": 0}


def tier_for_explicit_store(source: str | None, *, owner: bool = True) -> SourceTier:
    """Tier for an explicit store, decided by TRUSTED ORIGIN, never the label.

    ``owner`` is True only for an owner session (no declared reader, see
    ``audience.resolve_reader(...).unrestricted``): that is the card's
    "explicit memory_store from a session" and is ``user-stated``. Under a
    declared reader the tier is ``self-generated`` whatever the arguments say.
    The ``source`` label is model-supplied and can only LOWER the tier (a web,
    tool or generator label); an unknown label changes nothing and no label
    can raise it.
    """
    base: SourceTier = "user-stated" if owner else "self-generated"
    label = (source or "").strip().lower()
    if label in _GENERATED_SOURCES:
        cand: SourceTier = "self-generated"
    elif label in _TOOL_SOURCES:
        cand = "tool-output"
    elif label.startswith(("web", "http", "fetch", "browser", "search")):
        cand = "web"
    else:
        return base
    return cand if _RANK[cand] < _RANK[base] else base


def effective_tier(mem: Any) -> str | None:
    """The tier retrieval should honour, or None when the row is uncapped.

    None means legacy: schema_version below 2 and no known generator source.
    """
    if int(getattr(mem, "schema_version", 2) or 1) >= 2:
        return getattr(mem, "source_tier", None)
    if str(getattr(mem, "source", "") or "").strip().lower() in _GENERATED_SOURCES:
        return "self-generated"
    return None


def is_capped(mem: Any) -> bool:
    return effective_tier(mem) in CAPPED_TIERS


def apply_tier_cap(ids: Sequence[str], found: Mapping[str, Any], limit: int) -> list[str]:
    """Pick the top ``limit`` ids, capping web/self-generated occupancy.

    ``ids`` is the ranked pool (best first). Capped hits take at most
    ``floor(CAP_FRACTION * limit)`` slots while uncapped hits remain to take
    the rest; unfilled slots are backfilled with the skipped capped hits in rank
    order. Relative rank is preserved in the output. Unknown ids count as
    uncapped.
    """
    if limit <= 0:
        return []
    if os.environ.get("SKMEMORY_TIER_CAP", "on").strip().lower() in {"0", "off", "false", "no"}:
        return list(ids)[:limit]
    cap = math.floor(CAP_FRACTION * limit + 1e-9)
    chosen: list[int] = []
    skipped: list[int] = []
    n_capped = 0
    for i, mid in enumerate(ids):
        if len(chosen) >= limit:
            break
        mem = found.get(mid)
        if mem is not None and is_capped(mem):
            if n_capped < cap:
                n_capped += 1
                chosen.append(i)
            else:
                skipped.append(i)
        else:
            chosen.append(i)
    for i in skipped:
        if len(chosen) >= limit:
            break
        chosen.append(i)
    return [ids[i] for i in sorted(chosen)]
