"""anchor_freshness.py — Decay/freshness weight for anchor injection.

Provides:
    freshness_multiplier(last_invoked_iso: str) -> float
        Returns a multiplier in [0.1, 1.0] based on days since last invocation.
        Decays linearly from 1.0 to 0.1 over 365 days.

    backfill_last_invoked(anchor_dirs: list[Path]) -> int
        Backfills missing `last_invoked` field in anchor meta.json files
        using `created_at` as the initial value. Returns count updated.

Intentionally does NOT integrate into ritual.py — sub-agent D wires the
update mechanism when ready via the anchor-injection-log.jsonl telemetry.

Usage (standalone):
    from skmemory.anchor_freshness import freshness_multiplier, backfill_last_invoked
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["freshness_multiplier", "backfill_last_invoked"]


def freshness_multiplier(last_invoked_iso: Optional[str], *, now: Optional[datetime] = None) -> float:
    """
    Compute decay multiplier for an anchor based on last invocation time.

    Args:
        last_invoked_iso: ISO-8601 timestamp string of last invocation.
                          If None or unparseable, returns 1.0 (assume fresh).
        now: Override current time (for testing). Defaults to UTC now.

    Returns:
        float in [0.1, 1.0].
        1.0  = invoked today (no decay)
        0.55 = invoked 6 months ago
        0.1  = invoked 1+ year ago (floor)
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    if not last_invoked_iso:
        return 1.0

    try:
        last = datetime.fromisoformat(last_invoked_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 1.0

    days = max(0.0, (now - last).total_seconds() / 86400.0)
    return max(0.1, 1.0 - days / 365.0)


def backfill_last_invoked(anchor_dirs: list[Path], dry_run: bool = False) -> int:
    """
    Backfill `last_invoked` in anchor meta.json files where it is absent.
    Uses `created_at` as the seed value. If `created_at` is also missing,
    uses `event_date` field (YYYY-MM-DD) as a fallback.

    Args:
        anchor_dirs: List of anchor root directories to scan (each should
                     contain at least one subdirectory per anchor).
        dry_run: If True, print changes without writing.

    Returns:
        Number of meta.json files updated.
    """
    updated = 0
    for anchor_dir in anchor_dirs:
        if not anchor_dir.is_dir():
            continue
        for sub in sorted(anchor_dir.iterdir()):
            meta_path = sub / "meta.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            if "last_invoked" in data:
                continue  # already has the field — don't overwrite

            # Determine seed value
            seed = data.get("created_at")
            if not seed:
                event_date = data.get("event_date")
                if event_date:
                    seed = f"{event_date}T00:00:00+00:00"

            if not seed:
                # No usable timestamp — set to anchor_id date prefix if parseable
                anchor_id = data.get("anchor_id", sub.name)
                date_part = anchor_id[:10]
                try:
                    datetime.strptime(date_part, "%Y-%m-%d")
                    seed = f"{date_part}T00:00:00+00:00"
                except ValueError:
                    seed = None

            data["last_invoked"] = seed  # may be None; that's valid
            if dry_run:
                print(f"[DRY] would set last_invoked={seed!r} in {meta_path}")
            else:
                meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            updated += 1

    return updated
