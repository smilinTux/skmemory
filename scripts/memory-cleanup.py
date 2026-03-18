#!/usr/bin/env python3
"""Memory Cleanup Script for SKCapstone agents.

Handles two health issues:
  1. Deduplicate memories with identical titles (keep newest, archive older)
  2. Archive memories older than tier TTL from short-term and mid-term

Respects protected tags and attempts last-chance promotion before archiving.

Target: <200 active memory files across all tiers.

Runs weekly via cron (e.g. Sundays at 17:00), before weekly-review.
Can also be run manually:
  python3 memory-cleanup.py [--dry-run] [--verbose]
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
AGENT_NAME = os.environ.get("SKAGENT", "lumina")
AGENT_HOME = Path.home() / ".skcapstone" / "agents" / AGENT_NAME
MEMORY_HOME = AGENT_HOME / "memory"
ARCHIVE_DIR = AGENT_HOME / "archive" / "memory"
MEMORY_SHORT = MEMORY_HOME / "short-term"
LOGS_DIR = AGENT_HOME / "logs"
DB_PATH = MEMORY_HOME / "index.db"

TARGET_MAX_FILES = 200
TIERS = ["short-term", "mid-term", "long-term"]

# Per-tier age cutoffs before archiving
TIER_AGE_DAYS = {
    "short-term": 3,   # 72h TTL — anything older should be promoted or archived
    "mid-term": 30,    # keep for a month
}

# Tags that protect memories from TTL-based archival.
# Loaded from skmemory.promotion.PromotionCriteria when available,
# otherwise uses this hardcoded fallback.
try:
    from skmemory.promotion import PromotionCriteria
    PROTECTED_TAGS = set(PromotionCriteria().protected_tags)
except ImportError:
    PROTECTED_TAGS = {
        "narrative", "journal-synthesis", "milestone",
        "breakthrough", "cloud9:achieved",
    }

# Sources eligible for last-chance promotion before archiving
AUTO_PROMOTE_SOURCES = {"dreaming-engine", "journal-synthesis"}

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
NOW = datetime.now(timezone.utc)


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_memory(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def archive_file(src: Path, reason: str, dry_run: bool, verbose: bool) -> bool:
    """Move a memory file to the archive directory."""
    dest_dir = ARCHIVE_DIR / reason
    dest = dest_dir / src.name
    if verbose:
        tag = "[DRY-RUN]" if dry_run else "[ARCHIVE]"
        print(f"  {tag} {src.relative_to(MEMORY_HOME)} → archive/{reason}/{src.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return True


def db_delete(mem_id: str, dry_run: bool):
    """Remove a memory from the SQLite index."""
    if dry_run or not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # SQLite is rebuilt from flat files anyway; non-fatal


# ── Phase 1: Deduplication ────────────────────────────────────────────────────

def deduplicate(dry_run: bool, verbose: bool) -> dict:
    """Find memories with identical titles, keep newest, archive the rest."""
    title_map: dict[str, list[tuple[datetime, Path, dict]]] = defaultdict(list)

    for tier in TIERS:
        tier_dir = MEMORY_HOME / tier
        if not tier_dir.exists():
            continue
        for f in tier_dir.glob("*.json"):
            data = load_memory(f)
            if not data:
                continue
            title = data.get("title", "").lower().strip()
            if not title:
                continue
            created = parse_dt(data.get("created_at", "")) or datetime.min.replace(tzinfo=timezone.utc)
            title_map[title].append((created, f, data))

    archived = 0
    groups_processed = 0
    for title, entries in title_map.items():
        if len(entries) < 2:
            continue
        groups_processed += 1
        # Sort newest first
        entries.sort(key=lambda x: x[0], reverse=True)
        keeper_dt, keeper_path, keeper_data = entries[0]
        for dt, path, data in entries[1:]:
            mem_id = data.get("id", path.stem)
            archive_file(path, "dedup", dry_run, verbose)
            db_delete(mem_id, dry_run)
            archived += 1

    return {"duplicate_groups": groups_processed, "archived": archived}


# ── Phase 2: Age-based archiving ──────────────────────────────────────────────

def last_chance_promote(data: dict, src: Path, dry_run: bool, verbose: bool) -> bool:
    """Try to promote a memory before archiving it.

    Uses skmemory's PromotionEngine if available. Returns True if promoted.
    """
    if dry_run:
        return False
    try:
        from skmemory.store import MemoryStore
        from skmemory.promotion import PromotionEngine
        from skmemory.models import Memory

        memory = Memory(**data)
        store = MemoryStore()
        engine = PromotionEngine(store)
        target = engine.evaluate(memory)
        if target is not None:
            promoted = engine.promote_memory(memory, target)
            if promoted:
                if verbose:
                    print(f"  [PROMOTED] {src.name} → {target.value} (last chance)")
                return True
    except (ImportError, Exception) as exc:
        if verbose:
            print(f"  [PROMOTE-SKIP] {src.name}: {exc}")
    return False


def archive_old(dry_run: bool, verbose: bool) -> dict:
    """Archive short-term and mid-term memories past their tier TTL.

    Skips memories with protected tags. Attempts last-chance promotion
    before archiving (e.g. dreams that aged past TTL but qualify for
    source-based auto-promotion).
    """
    archived = 0
    protected_skipped = 0
    promoted = 0
    # long-term memories are intentionally kept — skip that tier
    for tier in ["short-term", "mid-term"]:
        age_days = TIER_AGE_DAYS[tier]
        cutoff = NOW - timedelta(days=age_days)
        tier_dir = MEMORY_HOME / tier
        if not tier_dir.exists():
            continue
        for f in tier_dir.glob("*.json"):
            data = load_memory(f)
            if not data:
                continue

            # Check protected tags — skip archival for these
            mem_tags = set(data.get("tags", []))
            if mem_tags & PROTECTED_TAGS:
                if verbose:
                    print(f"  [PROTECTED] {f.relative_to(MEMORY_HOME)} — has protected tag")
                protected_skipped += 1
                continue

            # Use last accessed time if available, else created_at
            ts_str = data.get("accessed_at") or data.get("created_at", "")
            ts = parse_dt(ts_str)
            if ts and ts < cutoff:
                # Last-chance promotion attempt
                if last_chance_promote(data, f, dry_run, verbose):
                    promoted += 1
                    continue

                mem_id = data.get("id", f.stem)
                archive_file(f, f"aged-{tier}", dry_run, verbose)
                db_delete(mem_id, dry_run)
                archived += 1

    return {"archived": archived, "protected_skipped": protected_skipped, "promoted": promoted}


# ── Phase 3: Count remaining files ───────────────────────────────────────────

def count_files() -> dict:
    counts = {}
    for tier in TIERS:
        tier_dir = MEMORY_HOME / tier
        counts[tier] = len(list(tier_dir.glob("*.json"))) if tier_dir.exists() else 0
    counts["total"] = sum(counts.values())
    return counts


# ── Report ────────────────────────────────────────────────────────────────────

def write_report(stats: dict, dry_run: bool):
    """Write cleanup summary to short-term memory."""
    if dry_run:
        return
    MEMORY_SHORT.mkdir(parents=True, exist_ok=True)
    aged = stats["aged"]
    entry = {
        "id": f"memory-cleanup-{TODAY}",
        "title": f"Memory Cleanup Run: {TODAY}",
        "content": (
            f"Dedup: {stats['dedup']['duplicate_groups']} groups → {stats['dedup']['archived']} archived\n"
            f"Aged out: {aged['archived']} files archived (past tier TTL)\n"
            f"Protected: {aged.get('protected_skipped', 0)} memories skipped (protected tags)\n"
            f"Last-chance promoted: {aged.get('promoted', 0)} memories saved by promotion\n"
            f"After cleanup: {stats['after']['total']} active files "
            f"(short-term: {stats['after']['short-term']}, "
            f"mid-term: {stats['after']['mid-term']}, "
            f"long-term: {stats['after']['long-term']})\n"
            f"Target: <{TARGET_MAX_FILES} files | "
            f"{'✓ UNDER TARGET' if stats['after']['total'] < TARGET_MAX_FILES else '⚠ STILL OVER TARGET'}"
        ),
        "layer": "short-term",
        "tags": ["memory-cleanup", "maintenance", "memory-optimization"],
        "created_at": NOW.isoformat(),
        "source": "memory-cleanup",
    }
    out = MEMORY_SHORT / f"memory-cleanup-{TODAY}.json"
    out.write_text(json.dumps(entry, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SKCapstone memory cleanup: dedup + age-out")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each file action")
    args = parser.parse_args()

    ts = NOW.isoformat()
    mode = " [DRY RUN]" if args.dry_run else ""
    print(f"[{ts}] memory-cleanup starting{mode} — agent: {AGENT_NAME}")

    # Before stats
    before = count_files()
    print(f"  Before: {before['total']} files "
          f"(short-term: {before['short-term']}, mid-term: {before['mid-term']}, long-term: {before['long-term']})")

    # Phase 1: Dedup
    print("  Phase 1: deduplication...")
    dedup_stats = deduplicate(args.dry_run, args.verbose)
    print(f"    → {dedup_stats['duplicate_groups']} duplicate title groups, {dedup_stats['archived']} files archived")

    # Phase 2: Age-out (with protected tags + last-chance promotion)
    print(f"  Phase 2: archiving memories past tier TTL (short-term: {TIER_AGE_DAYS['short-term']}d, mid-term: {TIER_AGE_DAYS['mid-term']}d)...")
    aged_stats = archive_old(args.dry_run, args.verbose)
    print(f"    → {aged_stats['archived']} files archived, "
          f"{aged_stats.get('protected_skipped', 0)} protected, "
          f"{aged_stats.get('promoted', 0)} last-chance promoted")

    # After stats
    after = count_files()
    print(f"  After:  {after['total']} files "
          f"(short-term: {after['short-term']}, mid-term: {after['mid-term']}, long-term: {after['long-term']})")

    target_status = "✓ under target" if after["total"] < TARGET_MAX_FILES else f"⚠ still over target ({TARGET_MAX_FILES})"
    print(f"  Target <{TARGET_MAX_FILES}: {target_status}")

    stats = {"dedup": dedup_stats, "aged": aged_stats, "before": before, "after": after}
    write_report(stats, args.dry_run)

    if not args.dry_run:
        print(f"  Report written to: {MEMORY_SHORT}/memory-cleanup-{TODAY}.json")
    print(f"[{NOW.isoformat()}] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
