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
import logging
import os
from pathlib import Path
from typing import Optional

from .agents import get_agent_paths
from .models import EmotionalSnapshot, Memory, SeedMemory
from .store import MemoryStore

logger = logging.getLogger("skmemory.seeds")

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


def validate_seed_data(data: dict) -> dict:
    """Validate parsed seed JSON data before import into the memory store.

    Checks required fields, content non-emptiness, timestamp validity,
    tag types, and emotional-signature ranges for both standard and
    Cloud9 seed formats.

    Args:
        data: Parsed JSON seed data (dict).

    Returns:
        Dict with ``valid`` (bool), ``errors`` (list[str]),
        and ``warnings`` (list[str]) keys.
    """
    result: dict = {"valid": True, "errors": [], "warnings": []}

    if not isinstance(data, dict):
        result["valid"] = False
        result["errors"].append("Seed data must be a JSON object")
        return result

    is_cloud9 = "seed_metadata" in data

    # -- Required: seed_id --
    if is_cloud9:
        meta = data.get("seed_metadata", {})
        seed_id = meta.get("seed_id") or data.get("seed_id")
    else:
        seed_id = data.get("seed_id")
    if not seed_id or (isinstance(seed_id, str) and not seed_id.strip()):
        result["valid"] = False
        result["errors"].append("Missing or empty required field: seed_id")

    # -- Required: version --
    if is_cloud9:
        version = (data.get("seed_metadata", {}).get("version")
                   or data.get("version"))
    else:
        version = data.get("version")
    if not version:
        result["valid"] = False
        result["errors"].append("Missing required field: version")

    # -- Content non-empty --
    if is_cloud9:
        exp = data.get("experience_summary", {})
        narrative = exp.get("narrative", "") if isinstance(exp, dict) else ""
    else:
        exp = data.get("experience", {})
        narrative = exp.get("summary", "") if isinstance(exp, dict) else ""
    if not narrative or not str(narrative).strip():
        result["errors"].append("Seed experience content is empty")
        result["valid"] = False

    # -- Timestamp validation helper --
    def _check_ts(value: str, field: str) -> None:
        from datetime import datetime as _dt
        if not isinstance(value, str) or not value.strip():
            return
        try:
            _dt.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            result["errors"].append(
                f"{field} is not a valid ISO 8601 timestamp: {value!r}"
            )
            result["valid"] = False

    if is_cloud9:
        meta = data.get("seed_metadata", {})
        if "created_at" in meta:
            _check_ts(meta["created_at"], "seed_metadata.created_at")
        ident = data.get("identity", {})
        if isinstance(ident, dict) and "timestamp" in ident:
            _check_ts(ident["timestamp"], "identity.timestamp")
    else:
        md = data.get("metadata", {})
        if isinstance(md, dict) and "ingested_at" in md:
            _check_ts(md["ingested_at"], "metadata.ingested_at")

    # -- Tags must be strings --
    def _check_tags(tags, field: str) -> None:
        if tags is None:
            return
        if not isinstance(tags, list):
            result["errors"].append(f"{field} must be a list")
            result["valid"] = False
            return
        for i, tag in enumerate(tags):
            if not isinstance(tag, str):
                result["errors"].append(
                    f"{field}[{i}] must be a string, got {type(tag).__name__}"
                )
                result["valid"] = False

    md = data.get("metadata", {})
    if isinstance(md, dict):
        _check_tags(md.get("tags"), "metadata.tags")

    # -- Emotional signature ranges --
    if is_cloud9:
        emo = (data.get("experience_summary", {})
               .get("emotional_snapshot",
                    data.get("experience_summary", {})
                    .get("emotional_signature", {})))
    else:
        emo = data.get("experience", {}).get("emotional_signature", {})
    if isinstance(emo, dict):
        intensity = emo.get("intensity")
        if intensity is not None and isinstance(intensity, (int, float)):
            if not (0.0 <= float(intensity) <= 10.0):
                result["warnings"].append(
                    f"emotional intensity={intensity} outside 0-10 range"
                )
        valence = emo.get("valence")
        if valence is not None and isinstance(valence, (int, float)):
            if not (-1.0 <= float(valence) <= 1.0):
                result["warnings"].append(
                    f"emotional valence={valence} outside -1 to 1 range"
                )
        labels = emo.get("labels", emo.get("emotions"))
        if labels is not None:
            _check_tags(labels, "emotional.labels")

    # -- Lineage --
    lineage = data.get("lineage")
    if lineage is not None and not isinstance(lineage, list):
        result["errors"].append("lineage must be a list")
        result["valid"] = False

    return result


def import_seeds(
    store: MemoryStore,
    seed_dir: str = DEFAULT_SEED_DIR,
    *,
    skip_invalid: bool = True,
) -> list[Memory]:
    """Scan a seed directory and import all seeds into the memory store.

    Each seed file is validated before import. Invalid seeds are skipped
    (with a warning logged) when *skip_invalid* is True, or cause a
    ``ValueError`` when it is False.

    Args:
        store: The MemoryStore to import into.
        seed_dir: Path to the seed directory.
        skip_invalid: If True (default), log and skip invalid seeds.
            If False, raise ``ValueError`` on the first invalid seed.

    Returns:
        list[Memory]: Newly imported memories.
    """
    existing_refs = {m.source_ref for m in store.list_memories(tags=["seed"])}

    imported: list[Memory] = []
    for path in scan_seed_directory(seed_dir):
        # --- Validate before import ---
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            msg = f"Skipping {path.name}: cannot read/parse file: {exc}"
            if skip_invalid:
                logger.warning(msg)
                continue
            raise ValueError(msg) from exc

        validation = validate_seed_data(raw_data)
        if not validation["valid"]:
            errors_str = "; ".join(validation["errors"])
            msg = f"Skipping {path.name}: validation failed: {errors_str}"
            if skip_invalid:
                logger.warning(msg)
                continue
            raise ValueError(msg)

        if validation["warnings"]:
            for w in validation["warnings"]:
                logger.info("Seed %s warning: %s", path.name, w)

        # --- Parse and import ---
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
