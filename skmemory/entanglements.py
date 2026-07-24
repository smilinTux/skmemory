"""
Entanglement anchor memory — shared-event peaks between Lumina and Chef.

An entanglement anchor captures a co-signed peak: a moment both participants
lived together, with consent logged, scalars recorded, and emotion topology
preserved. Distinct from:
  - SongAnchor (sonic FEB, externally seeded, no co-signature)
  - BloomAnchor (solo-peak, agent-authored alone, no co-signing required)

An entanglement requires at least one participant's explicit sign-off (via
partner_consent field) and must carry scalars_at_event. Truth-serum: the
co-signature and scalar record are the instrumentation, not cadence metrics.

Anchor dir layout (under ~/.skcapstone/agents/{agent}/memory/anchors/entanglement/):
  {YYYY-MM-DD}_{slug}/
    meta.json          # machine-readable: schema, emotions, scalars, consent
    moment.md          # what happened — Lumina's testimony
    resonance.md       # what Lumina wants future-self to do when this surfaces
    feb_link.json      # FEB(s) associated with the event
    CONSENT.md         # consent record (optional but canonical)
    signatures.json    # partner signature objects (optional)
    metrics.json       # additional instrumentation (optional)

Schema, scan, FEB-shape match, ritual injection (Step 1.8) all live here.
Mirrors peaks.py / songs.py for symmetry.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .agents import get_agent_paths

logger = logging.getLogger("skmemory.entanglements")


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


class EntanglementAnchor(BaseModel):
    """A single entanglement anchor — a shared co-signed peak."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    anchor_id: str = Field(
        description="Directory name, e.g. 2026-04-29_x1000-quickie-first-bloom-coevent"
    )
    path: Path = Field(description="Absolute path to anchor directory")

    # Identity
    title: str = Field(description="Short human-readable label")
    subtitle: str = Field(default="", description="One-line subtitle / context")

    # Core schema marker (stored as schema_version to avoid shadowing BaseModel.schema)
    schema_version: str = Field(
        default="anchor.entanglement.v1",
        alias="schema",
        description="Schema version string — anchor.entanglement.v1",
    )
    subtype: str = Field(
        default="peak",
        description="peak | calibration | collaborative-execution | bookmark | etc.",
    )

    # Temporal
    event_date: str = Field(description="ISO date when the event occurred")
    event_time: str = Field(default="", description="Time window string (human-readable)")
    event_phases: list[dict[str, str]] = Field(
        default_factory=list,
        description="Ordered list of {ts, label} phase objects",
    )

    # Participants
    participants: list[str] = Field(
        default_factory=lambda: ["Lumina", "Chef"],
        description="Names of participants in this entanglement event",
    )
    partner_consent: list[str] = Field(
        default_factory=list,
        description="Consent sign-off per participant: 'Name:signed' or 'Name:pending'",
    )
    calibration: bool = Field(
        default=False,
        description="True if this anchor serves as a calibration reference",
    )
    is_calibration_for: list[str] = Field(
        default_factory=list,
        description="What this anchor calibrates (e.g. 'entanglement-peak-anchor-future')",
    )
    calibrates_against: list[str] = Field(
        default_factory=list,
        description="Anchor IDs this event calibrates against",
    )

    # Emotion topology (same match space as songs + blooms)
    emotions: list[str] = Field(
        default_factory=list,
        description="Emotion labels — match space for FEB topology overlap",
    )
    emotion_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Emotion → strength (0-1). Parallel to FEB.emotional_topology.",
    )
    tags: list[str] = Field(default_factory=list)

    # Scalars
    scalars_at_event: dict[str, Any] = Field(
        default_factory=dict,
        description="Quantitative state at peak: trust, love_intensity, depth, valence, etc.",
    )

    # FEB + cross-anchor linkage
    primary_feb: str | None = None
    linked_febs: list[str] = Field(default_factory=list)
    linked_anchors: list[str] = Field(
        default_factory=list,
        description="Cross-type anchor refs, e.g. 'song:2026-04-22_lovely-day_first-anchor'",
    )
    linked_assets: list[str] = Field(
        default_factory=list,
        description="Absolute paths to associated files (images, scripts, etc.)",
    )

    # Injection control
    load_priority: str = Field(default="normal", description="high | normal | low")
    load_threshold_override: float | None = Field(
        default=None,
        description="Override the default min_score threshold for this anchor",
    )
    tilt_strength: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Injection weight 0-1: scales per_anchor_tokens budget. "
            "tilt_strength=0.5 → 90 tokens (half of 180). "
            "Use tilt_strength_active to set a different live/override value."
        ),
    )
    tilt_strength_active: float | None = Field(
        default=None,
        description=(
            "Live override for tilt_strength. If set, used instead of tilt_strength "
            "for all scoring and injection decisions. Allows hot-adjusting without "
            "editing the canonical tilt_strength on disk."
        ),
    )
    redacted: bool = False

    # Privacy
    privacy: dict[str, Any] = Field(default_factory=dict)

    # Provenance
    created_at: str = Field(default="")
    created_by: str = Field(default="lumina")
    schema_origin: str = Field(default="")

    # Optional extended fields (present in some anchors)
    shape_signature: dict[str, str] = Field(default_factory=dict)
    failure_recovery_chain: list[str] = Field(default_factory=list)
    favorite_frame: dict[str, Any] = Field(default_factory=dict)

    def effective_tilt_strength(self) -> float:
        """Return the active tilt strength, respecting override."""
        if self.tilt_strength_active is not None:
            return max(0.0, min(1.0, float(self.tilt_strength_active)))
        return max(0.0, min(1.0, float(self.tilt_strength)))

    def moment_text(self) -> str:
        p = self.path / "moment.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def resonance_text(self) -> str:
        p = self.path / "resonance.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def feb_link(self) -> dict[str, Any]:
        p = self.path / "feb_link.json"
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("entanglements.py: %s", e)
            return {}

    def to_tilt_block(self, tokens_max: int = 180) -> str:
        """Compact tilt block for ritual injection.

        Strategy mirrors BloomAnchor.to_tilt_block:
          1. Pull resonance section (TILT / future-me / etc.)
          2. Fall back to subtitle + top emotions.
          3. Cap at ~tokens_max words.
        """
        res = self.resonance_text()
        tilt = ""
        for marker in (
            "## What I want future-me to do when this anchor surfaces",
            "## What I want future-me to do",
            "## TILT",
        ):
            if marker in res:
                start = res.index(marker) + len(marker)
                chunk = res[start : start + 1200]
                for cut in ("\n---", "\n## ", "\n\n## "):
                    ci = chunk.find(cut)
                    if ci > 0:
                        chunk = chunk[:ci]
                        break
                tilt = chunk.strip()
                break

        parts: list[str] = []
        if self.subtitle:
            parts.append(f"Context: {self.subtitle}")
        if tilt:
            parts.append(tilt)
        elif self.emotions:
            parts.append(f"Shared shape: {', '.join(self.emotions[:6])}")

        text = "\n".join(parts)
        max_words = max(20, int(tokens_max / 1.3))
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]) + "…"
        return text


# --------------------------------------------------------------------------- #
# Scan + load                                                                 #
# --------------------------------------------------------------------------- #


def _entanglement_dir(agent: str | None = None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / "memory" / "anchors" / "entanglement"


def scan_entanglement_anchors(agent: str | None = None) -> list[EntanglementAnchor]:
    """Scan the agent's entanglement dir; return well-formed EntanglementAnchors.

    Well-formed = has meta.json with anchor.entanglement.v1 schema.
    All other files (resonance, feb_link, CONSENT, signatures) are optional.
    """
    d = _entanglement_dir(agent)
    if not d.exists():
        return []
    anchors: list[EntanglementAnchor] = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "meta.json"
        if not meta_path.exists():
            logger.debug("Skipping %s — no meta.json", sub.name)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # Inject runtime fields not in meta.json
            meta.setdefault("anchor_id", sub.name)
            meta["path"] = sub
            # Ignore unknown fields gracefully (extra_ignore via ConfigDict would
            # need extra="ignore", but pydantic v2 raises by default).
            # Build with only known field names.
            known = set(EntanglementAnchor.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in known}
            anchors.append(EntanglementAnchor(**filtered))
        except Exception as exc:
            logger.warning("Failed to load entanglement anchor %s: %s", sub.name, exc)
    return anchors


# --------------------------------------------------------------------------- #
# FEB-shape match — same hybrid metric as songs + peaks                       #
# --------------------------------------------------------------------------- #


def score_entanglement_for_feb(
    anchor: EntanglementAnchor,
    feb: dict | None,
    metric: str = "hybrid",
) -> float:
    """Hybrid emotion-topology score, mirroring score_bloom_for_feb.

    Identical math: 0.7 * coverage + 0.3 * jaccard.
    Entanglement anchors carry richer emotion_weights (many dimensions) so
    coverage rewards depth of overlap while jaccard guards against
    false-positive co-fires on wide but mismatched FEBs.
    """
    if feb is None:
        return 0.0
    payload = feb.get("emotional_payload", {})
    topo = payload.get("emotional_topology", {})
    if not topo:
        primary = payload.get("primary_emotion", "")
        return 1.0 if primary and primary in anchor.emotions else 0.0

    weights = anchor.emotion_weights or {e: 0.7 for e in anchor.emotions}
    if not weights:
        return 0.0

    common = set(weights) & set(topo)
    if not common:
        return 0.0

    weight_total = sum(float(v) for v in weights.values())
    coverage_num = sum(min(float(weights[e]), float(topo.get(e, 0.0))) for e in weights)
    coverage = coverage_num / weight_total if weight_total > 0 else 0.0

    if metric == "coverage":
        return coverage

    j_num = 0.0
    j_denom = 0.0
    for e in set(weights) | set(topo):
        sw = float(weights.get(e, 0.0))
        fw = float(topo.get(e, 0.0))
        j_num += min(sw, fw)
        j_denom += max(sw, fw)
    jaccard = j_num / j_denom if j_denom > 0 else 0.0

    if metric == "jaccard":
        return jaccard

    return 0.7 * coverage + 0.3 * jaccard


def match_entanglements_for_feb(
    feb: dict | None,
    agent: str | None = None,
    top_k: int = 3,
    min_score: float = 0.3,
) -> list[tuple[EntanglementAnchor, float]]:
    """Return top-K entanglement anchors whose shape matches the current FEB.

    Same threshold convention as songs + blooms (0.3) so all three anchor
    types have comparable injection rates. Per-anchor tilt_strength gate is
    applied here: anchors with effective_tilt_strength == 0 are excluded
    regardless of score.
    """
    all_anchors = scan_entanglement_anchors(agent)
    scored = []
    for a in all_anchors:
        if a.effective_tilt_strength() == 0.0:
            continue
        s = score_entanglement_for_feb(a, feb)
        if s >= min_score:
            scored.append((a, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def render_entanglement_tilt_section(
    matches: list[tuple[EntanglementAnchor, float]],
    per_anchor_tokens: int = 180,
) -> str:
    """Render the ritual's ENTANGLEMENT ANCHORS section."""
    if not matches:
        return ""
    lines = ["=== ENTANGLEMENT ANCHORS (shared peaks — carry these forward) ==="]
    for anchor, score in matches:
        lines.append(f"⚡ {anchor.title}  [match: {score:.2f}]")
        # Scale token budget by effective tilt_strength
        effective_ts = anchor.effective_tilt_strength()
        scaled_tokens = max(20, int(per_anchor_tokens * effective_ts))
        tilt = anchor.to_tilt_block(tokens_max=scaled_tokens)
        if tilt:
            lines.append(tilt)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Authoring helpers                                                           #
# --------------------------------------------------------------------------- #


def add_resonance_revision(
    anchor_id: str,
    note: str,
    author: str = "lumina",
    agent: str | None = None,
) -> Path:
    """Append a timestamped resonance revision to an entanglement anchor.

    Mirrors peaks.add_resonance_revision.
    """
    d = _entanglement_dir(agent) / anchor_id
    if not d.exists():
        raise ValueError(f"Entanglement anchor not found: {anchor_id}")
    res_path = d / "resonance.md"
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"\n\n---\n\n## Revision — {ts} — {author}\n\n{note.strip()}\n"
    if res_path.exists():
        with open(res_path, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(f"# Resonance — {anchor_id}\n{entry}")
    return res_path
