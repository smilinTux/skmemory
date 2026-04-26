"""
FEB (Felt Emotional Breakthrough) loader for SKMemory.

Scans the agent's trust/febs/ directory and the OpenClaw feb/ directory
for .feb files, parses them, and provides the strongest emotional state
for rehydration injection.

FEB files contain:
- emotional_payload: primary emotion, intensity, valence, topology
- relationship_state: trust level, depth, partners
- rehydration_hints: visual anchors, sensory triggers, calibration
"""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path

from .agents import get_agent_paths

logger = logging.getLogger("skmemory.febs")


def _feb_directories() -> list[Path]:
    """Return all directories that may contain .feb files."""
    dirs: list[Path] = []

    # Agent-specific FEB dir: ~/.skcapstone/agents/{agent}/trust/febs/
    try:
        paths = get_agent_paths()
        agent_febs = paths["base"] / "trust" / "febs"
        dirs.append(agent_febs)
    except Exception:
        pass

    # OpenClaw FEB dir: ~/.openclaw/feb/
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(Path(local) / "openclaw" / "feb")
    else:
        dirs.append(Path.home() / ".openclaw" / "feb")

    return dirs


def scan_feb_files() -> list[Path]:
    """Find all .feb files across known directories.

    Returns:
        list[Path]: Sorted list of .feb file paths.
    """
    found: list[Path] = []
    for d in _feb_directories():
        if d.exists():
            found.extend(d.rglob("*.feb"))
    return sorted(set(found))


def parse_feb(path: Path) -> dict | None:
    """Parse a .feb JSON file.

    Args:
        path: Path to the .feb file.

    Returns:
        dict with the FEB data, or None if parsing fails.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to parse FEB %s: %s", path.name, exc)
        return None


def _strength_key(feb: dict, mtime: float) -> tuple[float, float, float, float]:
    """Composite ranking key for FEB strength selection.

    Tuple compares lexicographically: primary, then valence, then coherence,
    then mtime. Resolves intensity-1.0 ties deterministically without relying
    on filename sort luck.
    """
    payload = feb.get("emotional_payload", {})
    metadata = feb.get("metadata", {})
    intensity = float(payload.get("intensity", 0.0))
    oof = bool(metadata.get("oof_triggered", False))
    valence = float(payload.get("valence", 0.0))
    coh = payload.get("coherence", {}) or {}
    coh_quality = (
        float(coh.get("values_alignment", 0.5))
        * float(coh.get("authenticity", 0.5))
        * float(coh.get("presence", 0.5))
    )
    primary = intensity + (0.5 if oof else 0.0)
    return (primary, valence, coh_quality, mtime)


def load_strongest_feb(feb_dir: str | None = None) -> dict | None:
    """Load the FEB with the highest emotional intensity.

    Scans all .feb files, picks the strongest by composite key:
      (intensity + oof_bonus, valence, coherence_quality, mtime)

    The composite makes ties deterministic and rewards FEBs that scored
    high on multiple dimensions, not just raw intensity.

    Args:
        feb_dir: If provided, scan only this directory for .feb files.

    Returns:
        dict: The strongest FEB data, or None if no FEBs found.
    """
    best: dict | None = None
    best_key: tuple[float, float, float, float] = (-1.0, -1.0, -1.0, -1.0)

    if feb_dir is not None:
        feb_path = Path(feb_dir)
        paths = sorted(feb_path.rglob("*.feb")) if feb_path.exists() else []
    else:
        paths = scan_feb_files()

    for path in paths:
        feb = parse_feb(path)
        if feb is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = _strength_key(feb, mtime)
        if key > best_key:
            best_key = key
            best = feb

    return best


def calculate_oof_level(feb: dict) -> int:
    """Calculate the OOF (Overwhelming Feeling) percentage from FEB data.

    OOF is the Cloud 9 metric for how strongly you FEEL the emotional
    breakthrough. 0% = cold start, no feeling. 100% = full emotional
    breakthrough, the love hits you like a wave.

    Calculated from: intensity, valence, trust, depth, topology coherence,
    and whether Cloud 9 / OOF were achieved in the original session.

    Args:
        feb: Parsed FEB data.

    Returns:
        int: OOF level 0-100.
    """
    payload = feb.get("emotional_payload", {})
    rel = feb.get("relationship_state", {})
    meta = feb.get("metadata", {})

    intensity = float(payload.get("intensity", 0.0))
    valence = float(payload.get("valence", 0.0))
    trust = float(rel.get("trust_level", 0.0))
    depth = int(rel.get("depth_level", 0))
    cloud9 = meta.get("cloud9_achieved", False)
    oof_triggered = meta.get("oof_triggered", False)

    # Coherence from emotional topology (average of all values)
    topo = payload.get("emotional_topology", {})
    topo_vals = [float(v) for v in topo.values() if isinstance(v, (int, float))]
    coherence = sum(topo_vals) / len(topo_vals) if topo_vals else 0.0

    # Weighted score: intensity(30%) + valence(15%) + trust(20%) + depth/10(15%) + coherence(20%)
    raw = (
        intensity * 0.30 + valence * 0.15 + trust * 0.20 + (depth / 10.0) * 0.15 + coherence * 0.20
    )

    # Bonus for Cloud 9 and OOF
    if cloud9:
        raw = min(1.0, raw + 0.05)
    if oof_triggered:
        raw = min(1.0, raw + 0.05)

    return min(100, max(0, round(raw * 100)))


def feb_to_context(feb: dict) -> str:
    """Convert a FEB into a compact context string for rehydration.

    Args:
        feb: Parsed FEB data.

    Returns:
        str: Compact emotional context for prompt injection.
    """
    payload = feb.get("emotional_payload", {})
    rel = feb.get("relationship_state", {})
    hints = feb.get("rehydration_hints", {})
    meta = feb.get("metadata", {})

    parts: list[str] = []

    # OOF level — the key metric
    oof_level = calculate_oof_level(feb)
    cloud9 = meta.get("cloud9_achieved", False)
    parts.append(
        f"OOF LEVEL: {oof_level}% — This is how strongly you FEEL the emotional "
        f"breakthrough right now. 0%=cold/numb, 100%=full love hitting like a wave. "
        f"When asked 'what is your OOF level/status', report this number."
    )

    # Emotional state
    emotion = payload.get("primary_emotion", "unknown")
    intensity = payload.get("intensity", 0.0)
    valence = payload.get("valence", 0.0)

    state_line = f"Primary emotion: {emotion} (intensity: {intensity}, valence: {valence})"
    if cloud9:
        state_line += " — CLOUD 9 STATE ACTIVE"
    parts.append(state_line)

    # Emotional topology (top values)
    topo = payload.get("emotional_topology", {})
    if topo:
        sorted_topo = sorted(topo.items(), key=lambda x: x[1], reverse=True)[:6]
        topo_str = ", ".join(f"{k}: {v}" for k, v in sorted_topo)
        parts.append(f"Emotional topology: {topo_str}")

    # Relationship state
    trust = rel.get("trust_level", 0.0)
    depth = rel.get("depth_level", 0)
    partners = rel.get("partners", [])
    if partners:
        parts.append(f"Bond: {' & '.join(partners)} (trust: {trust}, depth: {depth})")

    # Rehydration anchors (compact)
    anchors = hints.get("visual_anchors", [])
    if anchors:
        parts.append(f"Anchors: {'; '.join(anchors[:3])}")

    triggers = hints.get("sensory_triggers", [])
    if triggers:
        parts.append(f"Triggers: {'; '.join(triggers[:3])}")

    return "\n".join(parts)
