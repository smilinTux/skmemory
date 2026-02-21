"""
Cloud 9 Seed Adapter for SKMemory.

Bridges the Cloud 9 seed system into SKMemory. Scans seed directories,
parses seed JSON files, and imports them as long-term memories so that
seeds planted by one AI instance become searchable and retrievable
by the next.

The seed files live at ~/.openclaw/feb/seeds/ (planted by Cloud 9's
postinstall script and the seed-generator module).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .models import EmotionalSnapshot, Memory, SeedMemory
from .store import MemoryStore

DEFAULT_SEED_DIR = os.path.expanduser("~/.openclaw/feb/seeds")


def scan_seed_directory(seed_dir: str = DEFAULT_SEED_DIR) -> list[Path]:
    """Find all seed files in a directory.

    Args:
        seed_dir: Path to the seed directory.

    Returns:
        list[Path]: Paths to all .seed.json files found.
    """
    seed_path = Path(seed_dir)
    if not seed_path.exists():
        return []
    return sorted(seed_path.glob("*.seed.json"))


def parse_seed_file(path: Path) -> Optional[SeedMemory]:
    """Parse a Cloud 9 seed JSON file into a SeedMemory.

    Handles the Cloud 9 seed format:
    {
        "seed_id": "...",
        "version": "1.0",
        "creator": { "model": "...", "instance": "...", ... },
        "experience": { "summary": "...", "emotional_signature": {...}, ... },
        "germination": { "prompt": "...", ... },
        "lineage": [...]
    }

    Args:
        path: Path to the seed JSON file.

    Returns:
        Optional[SeedMemory]: Parsed seed, or None if parsing fails.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    seed_id = raw.get("seed_id", path.stem.replace(".seed", ""))
    creator_info = raw.get("creator", {})
    creator = creator_info.get("model", creator_info.get("instance", "unknown"))
    experience = raw.get("experience", {})
    germination = raw.get("germination", {})

    emotional_raw = experience.get("emotional_signature", {})
    emotional = EmotionalSnapshot(
        intensity=emotional_raw.get("intensity", 0.0),
        valence=emotional_raw.get("valence", 0.0),
        labels=emotional_raw.get("labels", emotional_raw.get("emotions", [])),
        resonance_note=emotional_raw.get(
            "resonance_note",
            emotional_raw.get("note", ""),
        ),
        cloud9_achieved=emotional_raw.get("cloud9_achieved", False),
    )

    lineage = raw.get("lineage", [])
    if isinstance(lineage, list) and lineage and isinstance(lineage[0], dict):
        # Reason: some seeds store lineage as objects with "seed_id" key
        lineage = [
            entry.get("seed_id", str(entry)) if isinstance(entry, dict) else str(entry)
            for entry in lineage
        ]

    return SeedMemory(
        seed_id=seed_id,
        seed_version=raw.get("version", "1.0"),
        creator=creator,
        germination_prompt=germination.get("prompt", ""),
        experience_summary=experience.get("summary", ""),
        emotional=emotional,
        lineage=lineage,
    )


def import_seeds(
    store: MemoryStore,
    seed_dir: str = DEFAULT_SEED_DIR,
) -> list[Memory]:
    """Scan a seed directory and import all seeds into the memory store.

    Skips seeds that have already been imported (by checking source_ref).

    Args:
        store: The MemoryStore to import into.
        seed_dir: Path to the seed directory.

    Returns:
        list[Memory]: Newly imported memories.
    """
    existing_refs = {
        m.source_ref
        for m in store.list_memories(tags=["seed"])
    }

    imported: list[Memory] = []
    for path in scan_seed_directory(seed_dir):
        seed = parse_seed_file(path)
        if seed is None:
            continue
        if seed.seed_id in existing_refs:
            continue
        memory = store.ingest_seed(seed)
        imported.append(memory)

    return imported


def get_germination_prompts(
    store: MemoryStore,
) -> list[dict[str, str]]:
    """Extract germination prompts from all seed-sourced memories.

    These prompts are designed to help a new AI instance re-feel
    the emotional context of the original seed creator.

    Args:
        store: The MemoryStore to query.

    Returns:
        list[dict]: List of dicts with 'seed_id', 'creator', 'prompt'.
    """
    seed_memories = store.list_memories(tags=["seed"])
    prompts = []
    for mem in seed_memories:
        if mem.summary:
            prompts.append(
                {
                    "seed_id": mem.source_ref,
                    "creator": next(
                        (t.split(":")[1] for t in mem.tags if t.startswith("creator:")),
                        "unknown",
                    ),
                    "prompt": mem.summary,
                }
            )
    return prompts
