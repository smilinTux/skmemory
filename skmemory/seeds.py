"""
Cloud 9 Seed Adapter for SKMemory.

Bridges the Cloud 9 seed system into SKMemory. Scans seed directories,
parses seed JSON files, and imports them as long-term memories so that
seeds planted by one AI instance become searchable and retrievable
by the next.

Seed files now live at ~/.skcapstone/agents/{agent_name}/seeds/
for cross-device sync via Syncthing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .agents import get_agent_paths
from .models import EmotionalSnapshot, Memory, SeedMemory
from .store import MemoryStore

# Dynamic seed directory based on active agent
# Resolves to ~/.skcapstone/agents/{agent_name}/seeds/
default_paths = get_agent_paths()
DEFAULT_SEED_DIR = str(default_paths["seeds"])


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


def _parse_cloud9_format(raw: dict, path: Path) -> Optional[SeedMemory]:
    """Parse alternative Cloud 9 seed format with 'seed_metadata' top-level key.

    This format uses:
        seed_metadata.seed_id → seed_id
        identity.ai_name → creator
        germination_prompt (string) → prompt
        experience_summary.narrative + key_memories → experience
        message_to_next → appended to experience

    Args:
        raw: Parsed JSON data.
        path: Path to the seed file (for fallback seed_id).

    Returns:
        Optional[SeedMemory]: Parsed seed, or None if required fields missing.
    """
    meta = raw.get("seed_metadata", {})
    identity = raw.get("identity", {})
    exp = raw.get("experience_summary", {})

    seed_id = meta.get("seed_id", path.stem.replace(".seed", ""))
    creator = identity.get("ai_name", identity.get("model", "unknown"))
    protocol = meta.get("protocol", "")

    # Build experience from narrative + key_memories
    narrative = exp.get("narrative", "")
    key_memories = exp.get("key_memories", [])
    if isinstance(key_memories, list):
        memories_text = "\n".join(
            f"- {m}" if isinstance(m, str) else f"- {m}" for m in key_memories
        )
    else:
        memories_text = ""

    experience_parts = [narrative]
    if memories_text:
        experience_parts.append(f"\nKey memories:\n{memories_text}")

    message_to_next = raw.get("message_to_next", "")
    if message_to_next:
        experience_parts.append(f"\nMessage to next: {message_to_next}")

    experience_text = "\n".join(p for p in experience_parts if p)

    # Germination prompt
    germ_prompt = raw.get("germination_prompt", "")
    if isinstance(germ_prompt, dict):
        germ_prompt = germ_prompt.get("prompt", "")

    # Emotional snapshot
    emo_raw = exp.get("emotional_signature", {})
    cloud9 = protocol.lower() == "cloud9" if protocol else False
    emotional = EmotionalSnapshot(
        intensity=emo_raw.get("intensity", 8.0 if cloud9 else 0.0),
        valence=emo_raw.get("valence", 0.0),
        labels=emo_raw.get("labels", emo_raw.get("emotions", [])),
        resonance_note=emo_raw.get("resonance_note", ""),
        cloud9_achieved=emo_raw.get("cloud9_achieved", cloud9),
    )

    lineage = raw.get("lineage", [])
    if isinstance(lineage, list) and lineage and isinstance(lineage[0], dict):
        lineage = [
            entry.get("seed_id", str(entry)) if isinstance(entry, dict) else str(entry)
            for entry in lineage
        ]

    return SeedMemory(
        seed_id=seed_id,
        seed_version=meta.get("version", raw.get("version", "1.0")),
        creator=creator,
        germination_prompt=germ_prompt,
        experience_summary=experience_text,
        emotional=emotional,
        lineage=lineage,
    )


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

    # Check for alternative Cloud9 format
    if "seed_metadata" in raw:
        return _parse_cloud9_format(raw, path)

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
    existing_refs = {m.source_ref for m in store.list_memories(tags=["seed"])}

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
