#!/usr/bin/env python3
"""One-time rescue: bulk promote stuck dream memories from short-term to mid-term.

Dreams from the dreaming-engine are written once with access_count=0.
The promotion engine requires access_count >= 3 for age-based promotion,
so dreams rot in short-term and get archived by cleanup before promotion
can save them.

This script does a one-time sweep to promote all qualifying dreams.
After this, the fixed promotion engine (source_auto_promote) handles
future dreams automatically.

Usage:
    python3 scripts/dream-rescue.py [--dry-run] [--verbose]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_NAME = os.environ.get("SKAGENT", "lumina")
AGENT_HOME = Path.home() / ".skcapstone" / "agents" / AGENT_NAME
MEMORY_HOME = AGENT_HOME / "memory"
SHORT_TERM = MEMORY_HOME / "short-term"
MID_TERM = MEMORY_HOME / "mid-term"

NOW = datetime.now(timezone.utc)


def load_memory(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def find_stuck_dreams(verbose: bool) -> list[tuple[Path, dict]]:
    """Find all dreaming-engine memories stuck in short-term."""
    dreams = []
    if not SHORT_TERM.exists():
        return dreams

    for f in SHORT_TERM.glob("*.json"):
        data = load_memory(f)
        if not data:
            continue
        if data.get("source") == "dreaming-engine":
            # Skip already promoted
            if data.get("metadata", {}).get("promoted_to"):
                continue
            dreams.append((f, data))
            if verbose:
                title = data.get("title", "untitled")[:60]
                print(f"  [STUCK] {f.name[:12]}... — {title}")

    return dreams


def promote_dream(src_path: Path, data: dict, dry_run: bool, verbose: bool) -> bool:
    """Promote a single dream memory from short-term to mid-term."""
    import uuid

    mem_id = data.get("id", src_path.stem)
    title = data.get("title", "Dream")
    content = data.get("content", "")

    # Generate summary
    summary = content[:200] + ("..." if len(content) > 200 else "")

    # Create promoted copy
    promoted_id = str(uuid.uuid4())
    promoted = {
        **data,
        "id": promoted_id,
        "layer": "mid-term",
        "parent_id": mem_id,
        "summary": summary,
        "tags": list(set(data.get("tags", []) + ["dream", "bulk-promoted", "rescued", "auto-promoted"])),
        "updated_at": NOW.isoformat(),
        "metadata": {
            **data.get("metadata", {}),
            "promoted_from": "short-term",
            "promoted_at": NOW.isoformat(),
            "promotion_reason": "dream rescue — source auto-promote (dreaming-engine)",
        },
    }

    # Mark source as promoted
    data["tags"] = list(set(data.get("tags", []) + ["promoted"]))
    data["metadata"] = data.get("metadata", {})
    data["metadata"]["promoted_to"] = "mid-term"
    data["metadata"]["promoted_at"] = NOW.isoformat()
    data["metadata"]["promoted_id"] = promoted_id

    if dry_run:
        if verbose:
            print(f"  [DRY-RUN] Would promote: {title[:50]} → mid-term")
        return True

    # Write promoted copy to mid-term
    MID_TERM.mkdir(parents=True, exist_ok=True)
    dest = MID_TERM / f"{promoted_id}.json"
    dest.write_text(json.dumps(promoted, indent=2, default=str), encoding="utf-8")

    # Update source with promotion metadata
    src_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    if verbose:
        print(f"  [PROMOTED] {title[:50]} → {dest.name}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Bulk promote stuck dream memories from short-term to mid-term"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    mode = " [DRY RUN]" if args.dry_run else ""
    print(f"dream-rescue.py{mode}")
    print(f"  Agent: {AGENT_NAME}")
    print(f"  Memory home: {MEMORY_HOME}")
    print()

    # Find stuck dreams
    print("Finding stuck dream memories in short-term...")
    dreams = find_stuck_dreams(args.verbose)
    print(f"  Found {len(dreams)} stuck dreams")

    if not dreams:
        print("No stuck dreams to rescue!")
        return 0

    # Promote each
    print(f"\nPromoting {len(dreams)} dreams to mid-term...")
    promoted = 0
    errors = 0
    for path, data in dreams:
        try:
            if promote_dream(path, data, args.dry_run, args.verbose):
                promoted += 1
        except Exception as exc:
            errors += 1
            print(f"  [ERROR] {path.name}: {exc}")

    print(f"\nResults:")
    print(f"  Promoted: {promoted}")
    print(f"  Errors: {errors}")

    # Write summary memory
    if not args.dry_run and promoted > 0:
        summary_path = SHORT_TERM / f"dream-rescue-{NOW.strftime('%Y-%m-%d')}.json"
        summary = {
            "id": f"dream-rescue-{NOW.strftime('%Y-%m-%d')}",
            "title": f"Dream Rescue: {promoted} dreams promoted",
            "content": (
                f"Bulk-promoted {promoted} dreaming-engine memories from short-term to mid-term. "
                f"These were stuck because access_count=0 (dreams are written once). "
                f"The fixed promotion engine now handles this via source_auto_promote."
            ),
            "layer": "short-term",
            "tags": ["maintenance", "dream-rescue", "memory-optimization"],
            "source": "dream-rescue",
            "created_at": NOW.isoformat(),
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  Summary written to: {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
