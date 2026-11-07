"""Decay channels: explicit formulas for age, reinforcement and affect.

Each channel scores one memory at a clock ``as_of``. A higher score ranks
higher; the lowest score is the first prune candidate. Everything here is pure
and deterministic so a reviewer can verify a ranking by hand.

Definitions (days are fractional, ``as_of`` minus the timestamp, floored at 0):

    touch   = the later of created_at and last_accessed
    H       = fact_half_life_days (30) for kind "fact", gist_half_life_days
              (120) for kind "gist"
    f       = 0.5 ** (days(touch) / H)        decay factor
    boost   = 1 + ln(1 + access_count)        reinforcement boost
    i       = emotional intensity on 0-1

    none             score = 1
    recency          score = 0.5 ** (days(created_at) / H)
    reinforcement    score = f * boost
    affect_weighted  score = max(f, affect_floor if i >= 0.5 else 0) * boost * (1 + i)

Ties (scores equal after rounding to 12 places) break by higher access_count,
then by id ascending, so every ordering is total and reproducible.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

CHANNELS = ("none", "recency", "reinforcement", "affect_weighted")
FACT_HALF_LIFE_DAYS = 30.0
GIST_HALF_LIFE_DAYS = 120.0
AFFECT_FLOOR = 0.15
AFFECT_GATE = 0.5
DEFAULT_CHANNEL = "affect_weighted"
# How strongly decay bends a relevance-ordered result list (see rerank()).
RERANK_GAMMA = 0.15

_OFF = {"0", "off", "false", "no", "disabled"}
_ON = {"", "1", "on", "true", "yes"}


LAYER_IMPORTANCE = {"short-term": 0.3, "mid-term": 0.5, "long-term": 0.7}
INTENSITY_IMPORTANCE_WEIGHT = 0.3
REINFORCE_IMPORTANCE_STEP = 0.02


def initial_importance(layer: str, intensity: float) -> float:
    """Importance at first index: layer base + 0.3 * intensity/10, capped at 1.0.

    ``intensity`` is the 0-10 emotional intensity stored on the memory. Stored
    only in the SQLite ``importance`` column, never in the flat file.
    """
    base = LAYER_IMPORTANCE.get(str(layer), 0.5)
    i = min(max(float(intensity or 0.0), 0.0), 10.0) / 10.0
    return min(1.0, round(base + INTENSITY_IMPORTANCE_WEIGHT * i, 4))


def parse_ts(value: str | datetime | None) -> datetime | None:
    """ISO-8601 string (or datetime) to an aware UTC datetime; None if unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days(as_of: datetime, ts: datetime | None) -> float:
    if ts is None:
        return 0.0
    return max((as_of - ts).total_seconds() / 86400.0, 0.0)


def decay_score(
    channel: str,
    *,
    as_of: datetime,
    created_at: str | datetime | None,
    last_accessed: str | datetime | None = None,
    access_count: int = 0,
    intensity: float = 0.0,
    kind: str = "fact",
    fact_half_life_days: float = FACT_HALF_LIFE_DAYS,
    gist_half_life_days: float = GIST_HALF_LIFE_DAYS,
    affect_floor: float = AFFECT_FLOOR,
) -> float:
    """Score one memory under ``channel``. ``intensity`` is on 0-1."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown decay channel {channel!r}; expected one of {CHANNELS}")
    if channel == "none":
        return 1.0
    created = parse_ts(created_at)
    half = gist_half_life_days if kind == "gist" else fact_half_life_days
    if channel == "recency":
        return 0.5 ** (_days(as_of, created) / half)
    accessed = parse_ts(last_accessed)
    touch = max((t for t in (created, accessed) if t is not None), default=None)
    f = 0.5 ** (_days(as_of, touch) / half)
    boost = 1.0 + math.log1p(max(int(access_count or 0), 0))
    if channel == "reinforcement":
        return f * boost
    i = min(max(float(intensity or 0.0), 0.0), 1.0)
    floor = affect_floor if i >= AFFECT_GATE else 0.0
    return max(f, floor) * boost * (1.0 + i)


def order_by_score(items: Sequence[dict], scores: dict[str, float]) -> list[str]:
    """Ids ranked high to low: score, then access_count desc, then id asc."""
    return [
        it["id"]
        for it in sorted(
            items,
            key=lambda it: (
                -round(scores[it["id"]], 12),
                -int(it.get("access_count") or 0),
                it["id"],
            ),
        )
    ]


def configured_channel(default: str = DEFAULT_CHANNEL) -> str | None:
    """Channel named by ``SKMEMORY_DECAY``; ``None`` when decay is off.

    Unset or "1/on/true" selects ``default``; "0/off/false" disables; a channel
    name selects it. An unrecognised value disables decay rather than guess.
    """
    raw = os.environ.get("SKMEMORY_DECAY")
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _OFF:
        return None
    if val in _ON:
        return default
    return val if val in CHANNELS and val != "none" else None


def rerank(
    memories: Sequence,
    usage: dict[str, dict] | None,
    channel: str,
    *,
    as_of: datetime | None = None,
    gamma: float = RERANK_GAMMA,
    score_fn: Callable[..., float] = decay_score,
) -> list:
    """Bend a relevance-ordered list by decay without replacing relevance.

    The input order IS the relevance signal, so each memory gets a positional
    prior ``1 / (1 + rank)`` and the final key is ``prior * score ** gamma``.
    With a small gamma, only near-ties move: rank 0 vs rank 1 flips only when
    the decay scores differ by more than 2 ** (1 / gamma). Memories with no
    usage row are left unscored (score 1.0). Stable on equal keys.
    """
    if channel == "none" or len(memories) < 2 or usage is None:
        return list(memories)
    now = as_of or datetime.now(timezone.utc)
    keyed = []
    for rank, m in enumerate(memories):
        u = usage.get(m.id, {})
        layer = getattr(getattr(m, "layer", None), "value", getattr(m, "layer", ""))
        s = score_fn(
            channel,
            as_of=now,
            created_at=getattr(m, "created_at", None),
            last_accessed=u.get("last_accessed"),
            access_count=u.get("access_count", 0),
            intensity=(getattr(getattr(m, "emotional", None), "intensity", 0.0) or 0.0) / 10.0,
            kind="gist" if layer == "long-term" else "fact",
        )
        keyed.append((-(1.0 / (1 + rank)) * max(s, 1e-9) ** gamma, rank, m))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [m for _, _, m in keyed]
