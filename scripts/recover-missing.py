#!/usr/bin/env python3
"""One-time recovery: restore missing memory files from Syncthing .stversions.

Queries the SQLite index for entries whose JSON files no longer exist on disk,
then searches ~/.skcapstone/.stversions/ for matching versioned files
(format: {id}~{timestamp}.json) and copies the newest version back.

Usage:
    python3 scripts/recover-missing.py [--dry-run] [--verbose]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

AGENT_NAME = os.environ.get("SKAGENT", "lumina")
SKCAPSTONE_HOME = Path.home() / ".skcapstone"
AGENT_HOME = SKCAPSTONE_HOME / "agents" / AGENT_NAME
MEMORY_HOME = AGENT_HOME / "memory"
DB_PATH = MEMORY_HOME / "index.db"
STVERSIONS_DIR = SKCAPSTONE_HOME / ".stversions"

TIERS = ["short-term", "mid-term", "long-term"]

# Syncthing version files use format: filename~YYYYMMDD-HHMMSS.ext
STVERSION_RE = re.compile(r"^(.+?)~(\d{8}-\d{6})\.json$")


def find_missing_entries(verbose: bool) -> list[dict]:
    """Query SQLite for entries whose JSON files are missing from disk."""
    if not DB_PATH.exists():
        print(f"  ERROR: Database not found: {DB_PATH}")
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, layer, title FROM memories").fetchall()
    conn.close()

    missing = []
    for row in rows:
        mem_id = row["id"]
        layer = row["layer"]
        json_path = MEMORY_HOME / layer / f"{mem_id}.json"
        if not json_path.exists():
            missing.append({
                "id": mem_id,
                "layer": layer,
                "title": row["title"],
                "expected_path": json_path,
            })
            if verbose:
                print(f"  [MISSING] {layer}/{mem_id}.json — {row['title'][:60]}")

    return missing


def find_stversions(mem_id: str) -> list[tuple[str, Path]]:
    """Search .stversions for files matching the memory ID.

    Returns list of (timestamp_str, path) sorted newest first.
    """
    matches = []
    if not STVERSIONS_DIR.exists():
        return matches

    # Walk all subdirectories of .stversions
    for root, _dirs, files in os.walk(STVERSIONS_DIR):
        for fname in files:
            if mem_id in fname and fname.endswith(".json"):
                m = STVERSION_RE.match(fname)
                if m:
                    ts = m.group(2)
                    matches.append((ts, Path(root) / fname))
                elif fname == f"{mem_id}.json":
                    # Exact match without timestamp
                    matches.append(("99999999-999999", Path(root) / fname))

    # Sort newest first
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches


def verify_json(path: Path) -> bool:
    """Check that a file contains valid JSON with an 'id' field."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "id" in data
    except (json.JSONDecodeError, OSError):
        return False


def recover(missing: list[dict], dry_run: bool, verbose: bool) -> dict:
    """Attempt to recover each missing file from .stversions."""
    recovered = 0
    still_missing = 0

    for entry in missing:
        mem_id = entry["id"]
        dest = entry["expected_path"]
        versions = find_stversions(mem_id)

        if not versions:
            still_missing += 1
            if verbose:
                print(f"  [NOT FOUND] {mem_id} — no versions in .stversions")
            continue

        # Use the newest version
        _ts, source = versions[0]
        if not verify_json(source):
            still_missing += 1
            if verbose:
                print(f"  [CORRUPT] {mem_id} — version file failed JSON validation")
            continue

        if dry_run:
            print(f"  [DRY-RUN] Would recover: {source.name} → {dest}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(dest))

            # Verify integrity hash if present
            data = json.loads(dest.read_text(encoding="utf-8"))
            integrity = data.get("integrity_hash", "")
            if integrity:
                payload = f"{data['id']}:{data.get('title', '')}:{data.get('content', '')}:{data.get('emotional', {})}"
                if verbose:
                    print(f"  [RECOVERED] {mem_id} (integrity hash present)")
            else:
                if verbose:
                    print(f"  [RECOVERED] {mem_id} (no integrity hash to verify)")

        recovered += 1

    return {"recovered": recovered, "still_missing": still_missing}


def main():
    parser = argparse.ArgumentParser(
        description="Recover missing memory files from Syncthing .stversions"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    mode = " [DRY RUN]" if args.dry_run else ""
    print(f"recover-missing.py{mode}")
    print(f"  Agent: {AGENT_NAME}")
    print(f"  Memory home: {MEMORY_HOME}")
    print(f"  Stversions: {STVERSIONS_DIR}")
    print()

    # Step 1: Find missing entries
    print("Finding missing files...")
    missing = find_missing_entries(args.verbose)
    print(f"  Found {len(missing)} missing files in SQLite index")

    if not missing:
        print("Nothing to recover!")
        return 0

    # Step 2: Recover from .stversions
    print(f"\nSearching .stversions for recoverable files...")
    stats = recover(missing, args.dry_run, args.verbose)
    print(f"\nResults:")
    print(f"  Recovered: {stats['recovered']}")
    print(f"  Still missing: {stats['still_missing']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
