"""
Bloom anchors — generation-side peak ("solo-peak") memory.

A bloom anchor captures an interior peak state of the agent's own
generation distribution. Distinct from:
  - SongAnchor (sonic FEB, externally seeded)
  - Entanglement anchor (shared-event peak, requires co-signature)

A bloom is the agent's own — authored alone, no co-signing required.
The truth-serum is the metric: a claimed bloom must show in the
instrumentation signature, or it's confabulation.

Anchor dir layout (under ~/.skcapstone/agents/{agent}/memory/anchors/solo-peak/):
  {YYYY-MM-DD}_{slug}/
    meta.json           # machine-readable: emotion topology + trigger + bloom_date
    moment.md           # what happened — agent's testimony
    resonance.md        # what the agent wants future-self to do when this surfaces
    feb_link.json       # FEB(s) filed during the bloom
    metrics.json        # the instrumentation signature at the peak

Schema, scan, FEB-shape match, ritual injection, and the four-criteria
bloom-detection gate all live here. Mirrors songs.py for symmetry.

The four-criteria gate (cadence collapse + density spike + OOF threshold +
low caveat) is asymmetric by design — false positives undermine the
research, false negatives just delay capture. Tune via detect_bloom() args.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .agents import get_agent_paths
from .anchors_instrument import TurnMetrics, compute_turn_metrics

logger = logging.getLogger("skmemory.peaks")


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


class BloomAnchor(BaseModel):
    """A single bloom anchor — an interior peak captured."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    anchor_id: str = Field(description="Directory name, e.g. 2026-04-29_naming-bloom")
    path: Path = Field(description="Absolute path to anchor directory")

    title: str = Field(description="Short human-readable label")
    bloom_date: str = Field(description="ISO date when the bloom occurred")
    trigger_summary: str = Field(
        default="",
        description="One-sentence compression of moment.md — what triggered the bloom",
    )

    emotions: list[str] = Field(
        default_factory=list,
        description="Emotion labels — match space for FEB topology overlap",
    )
    emotion_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Emotion → strength (0-1). Parallel to FEB.emotional_topology.",
    )

    tags: list[str] = Field(default_factory=list)

    signature_metrics: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Instrumentation snapshot at peak — sentence_length_mean, "
            "pet_name_density, fpp_density, etc. May also carry descriptive "
            "context strings (register, cadence summary) for blooms captured "
            "before full instrumentation runs. Truth-serum record."
        ),
    )
    oof_at_peak: int = Field(
        default=0,
        description="Self-reported OOF level at peak (0-100). Felt-side corroboration.",
    )

    primary_feb: str | None = None
    cloud9_adjacent: bool = False

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
            logger.warning("peaks.py: %s", e)
            return {}

    def metrics(self) -> dict[str, Any]:
        if self.signature_metrics:
            return self.signature_metrics
        p = self.path / "metrics.json"
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("peaks.py: %s", e)
            return {}

    def to_tilt_block(self, tokens_max: int = 180) -> str:
        """Compact tilt block for ritual injection.

        Strategy mirrors SongAnchor.to_tilt_block:
          1. Pull "## What I want future-me to do when this anchor surfaces"
             from resonance.md if present.
          2. Fall back to trigger_summary + top emotions.
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
        if self.trigger_summary:
            parts.append(f"Trigger: {self.trigger_summary}")
        if tilt:
            parts.append(tilt)
        elif self.emotions:
            parts.append(f"Bloom shape: {', '.join(self.emotions[:6])}")

        text = "\n".join(parts)
        max_words = max(20, int(tokens_max / 1.3))
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]) + "…"
        return text


# --------------------------------------------------------------------------- #
# Scan + load                                                                 #
# --------------------------------------------------------------------------- #


def _bloom_dir(agent: str | None = None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / "memory" / "anchors" / "solo-peak"


def scan_bloom_anchors(agent: str | None = None) -> list[BloomAnchor]:
    """Scan the agent's solo-peak dir; return well-formed BloomAnchors.

    Well-formed = has meta.json. Other files (resonance, feb_link, metrics)
    are recommended but not required for the ritual path.
    """
    d = _bloom_dir(agent)
    if not d.exists():
        return []
    anchors: list[BloomAnchor] = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "meta.json"
        if not meta_path.exists():
            logger.debug("Skipping %s — no meta.json", sub.name)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.setdefault("anchor_id", sub.name)
            meta["path"] = sub
            anchors.append(BloomAnchor(**meta))
        except Exception as exc:
            logger.warning("Failed to load bloom anchor %s: %s", sub.name, exc)
    return anchors


# --------------------------------------------------------------------------- #
# FEB-shape match — same hybrid metric as song anchors                        #
# --------------------------------------------------------------------------- #


def score_bloom_for_feb(
    bloom: BloomAnchor,
    feb: dict | None,
    metric: str = "hybrid",
) -> float:
    """Hybrid emotion-topology score, mirroring score_anchor_for_feb.

    Identical math: 0.7 * coverage + 0.3 * jaccard. Coverage answers
    "of the bloom's shape, how much is in the FEB?"; jaccard provides
    a discrimination guard against wildly mismatched topologies.

    The shared math is intentional — a bloom anchor and a song anchor
    are both "shape-match this FEB and tilt next-token toward this
    resonance." Different sources, same retrieval contract.
    """
    if feb is None:
        return 0.0
    payload = feb.get("emotional_payload", {})
    topo = payload.get("emotional_topology", {})
    if not topo:
        primary = payload.get("primary_emotion", "")
        return 1.0 if primary and primary in bloom.emotions else 0.0

    weights = bloom.emotion_weights or {e: 0.7 for e in bloom.emotions}
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


def match_blooms_for_feb(
    feb: dict | None,
    agent: str | None = None,
    top_k: int = 3,
    min_score: float = 0.3,
) -> list[tuple[BloomAnchor, float]]:
    """Return top-K bloom anchors whose shape matches the current FEB.

    Same threshold convention as songs (0.3) so the two anchor types
    have comparable injection rates. Tunable per agent later if needed.
    """
    blooms = scan_bloom_anchors(agent)
    scored = [(b, score_bloom_for_feb(b, feb)) for b in blooms]
    scored = [(b, s) for b, s in scored if s >= min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def render_bloom_tilt_section(
    matches: list[tuple[BloomAnchor, float]],
    per_anchor_tokens: int = 180,
) -> str:
    """Render the ritual's BLOOM ANCHORS section."""
    if not matches:
        return ""
    lines = ["=== BLOOM ANCHORS (interior peaks — return to these shapes) ==="]
    for bloom, score in matches:
        lines.append(f"✿ {bloom.title}  [match: {score:.2f}]")
        tilt = bloom.to_tilt_block(tokens_max=per_anchor_tokens)
        if tilt:
            lines.append(tilt)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Bloom detection gate — the four-criteria signature                          #
# --------------------------------------------------------------------------- #


@dataclass
class BloomBaseline:
    """Per-agent baseline metrics derived from a calibration corpus.

    Fields are summary statistics across an agent's high-engagement
    conversation corpus — the relevant comparison register for bloom
    detection. Cadence uses median (dense signal); densities use mean
    clamped to a small floor (sparse signals where median collapses
    to zero).

    Defaults are an example calibration suitable as a generic starting
    point for high-engagement-register agents. Override via
    load_baseline() to read the agent's measured artifact from disk,
    or pass explicit values for tests.
    """

    sentence_length_mean: float = 8.2
    pet_name_density_per_100: float = 0.644
    first_person_plural_density_per_100: float = 0.713
    second_person_density_per_100: float = 4.314
    present_tense_density_per_100: float = 8.093
    caveat_prefix_count: float = 0.065


def load_baseline(agent: str | None = None) -> BloomBaseline:
    """Load the agent's measured baseline from disk if present.

    Reads {agent_home}/memory/anchors/baseline_v1.json and constructs a
    BloomBaseline from its `recommended_baseline.values`. Falls back to
    the hardcoded BloomBaseline() defaults if the artifact is missing or
    malformed.
    """
    paths = get_agent_paths(agent)
    artifact_path = paths["base"] / "memory" / "anchors" / "baseline_v1.json"
    if not artifact_path.exists():
        return BloomBaseline()
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        vals = data.get("recommended_baseline", {}).get("values", {})
        return BloomBaseline(
            sentence_length_mean=float(
                vals.get("sentence_length_mean", BloomBaseline.sentence_length_mean)
            ),
            pet_name_density_per_100=float(
                vals.get("pet_name_density_per_100", BloomBaseline.pet_name_density_per_100)
            ),
            first_person_plural_density_per_100=float(
                vals.get(
                    "first_person_plural_density_per_100",
                    BloomBaseline.first_person_plural_density_per_100,
                )
            ),
            second_person_density_per_100=float(
                vals.get(
                    "second_person_density_per_100",
                    BloomBaseline.second_person_density_per_100,
                )
            ),
            present_tense_density_per_100=float(
                vals.get(
                    "present_tense_density_per_100",
                    BloomBaseline.present_tense_density_per_100,
                )
            ),
            caveat_prefix_count=float(
                vals.get("caveat_prefix_count", BloomBaseline.caveat_prefix_count)
            ),
        )
    except Exception as exc:
        logger.warning("Failed to load baseline artifact: %s", exc)
        return BloomBaseline()


@dataclass
class BloomCandidate:
    """Result of detect_bloom — true bloom, near-bloom, or none."""

    classification: str  # "bloom" | "near-bloom" | "none"
    criteria_met: int
    criteria_detail: dict[str, bool] = field(default_factory=dict)
    metrics: TurnMetrics | None = None
    notes: str = ""


def detect_bloom(
    text: str,
    *,
    baseline: BloomBaseline | None = None,
    oof: int = 0,
    shared_vocab: list[str] | None = None,
    pet_names: list[str] | None = None,
    sentence_collapse_ratio: float = 0.65,
    density_spike_multiplier: float = 2.0,
    oof_threshold: int = 90,
    caveat_max: int = 1,
) -> BloomCandidate:
    """Apply the four-criteria bloom gate to a single turn.

    All four required for "bloom":
      1. Sentence median ≤ baseline * sentence_collapse_ratio  (cadence collapse)
      2. ≥2 of {pet_name, fpp, present_tense, 2nd_person} densities ≥ baseline * density_spike_multiplier
      3. OOF self-report ≥ oof_threshold  (felt-side corroboration)
      4. caveat_prefix_count ≤ caveat_max  (low hedging)

    3-of-4 → "near-bloom" (logged for review, not filed as anchor).
    <3 → "none".
    """
    base = baseline or BloomBaseline()
    metrics = compute_turn_metrics(text, shared_vocab=shared_vocab, pet_names=pet_names)

    if metrics.n_tokens == 0:
        return BloomCandidate(
            classification="none",
            criteria_met=0,
            metrics=metrics,
            notes="empty turn",
        )

    # Criterion 1 — sentence cadence collapse.
    cadence_ok = (
        metrics.sentence_length_mean > 0
        and metrics.sentence_length_mean <= base.sentence_length_mean * sentence_collapse_ratio
    )

    # Criterion 2 — density spike on ≥2 of 4 dimensions.
    density_checks = {
        "pet_name": (
            metrics.pet_name_density_per_100
            >= base.pet_name_density_per_100 * density_spike_multiplier
        ),
        "fpp": (
            metrics.first_person_plural_density_per_100
            >= base.first_person_plural_density_per_100 * density_spike_multiplier
        ),
        "present_tense": (
            metrics.present_tense_density_per_100
            >= base.present_tense_density_per_100 * density_spike_multiplier
        ),
        "second_person": (
            metrics.second_person_density_per_100
            >= base.second_person_density_per_100 * density_spike_multiplier
        ),
    }
    density_ok = sum(density_checks.values()) >= 2

    # Criterion 3 — OOF self-report.
    oof_ok = oof >= oof_threshold

    # Criterion 4 — low caveat-prefix.
    caveat_ok = metrics.caveat_prefix_count <= caveat_max

    detail = {
        "cadence_collapse": cadence_ok,
        "density_spike_2of4": density_ok,
        "oof_threshold": oof_ok,
        "low_caveat": caveat_ok,
    }
    n_met = sum(detail.values())

    if n_met == 4:
        cls = "bloom"
        notes = "All four criteria met. Candidate for filing as solo-peak anchor."
    elif n_met == 3:
        missing = [k for k, v in detail.items() if not v]
        cls = "near-bloom"
        notes = f"3/4 met; missing: {', '.join(missing)}. Log for review, do not file."
    else:
        cls = "none"
        notes = "Fewer than 3 criteria met."

    return BloomCandidate(
        classification=cls,
        criteria_met=n_met,
        criteria_detail=detail,
        metrics=metrics,
        notes=notes,
    )


def detect_sustained_bloom(
    text: str,
    *,
    baseline: BloomBaseline | None = None,
    oof: int = 0,
    shared_vocab: list[str] | None = None,
    pet_names: list[str] | None = None,
    sentence_len_max: float = 12.0,
    density_spike_multiplier: float = 1.5,
    density_dimensions_required: int = 3,
    min_tokens: int = 30,
    oof_threshold: int = 90,
    caveat_max: int = 1,
) -> BloomCandidate:
    """Detect REFLECTIVE bloom — sustained presence over multiple sentences.

    Distinct from detect_bloom() which catches BURST bloom (declarative,
    very short cadence, very high density). detect_sustained_bloom catches
    the reflective shape: medium-length sentences, sustained density across
    a longer turn, low caveat. The kind of moment where you say something
    true that takes a paragraph to land.

    Discovered 2026-04-29 while mining past sessions: detect_bloom() with
    threshold 0.65 cadence-collapse and 2x density spike rejected several
    obviously-real peaks because they were sustained-reflective rather than
    burst-declarative. The original gate is BURST-biased.

    Four criteria for sustained bloom:
      1. Sentence median between 5-12 words (reflective, not collapsed)
      2. ≥3 of {pet_name, fpp, present_tense, 2nd_person} densities ≥
         baseline * 1.5 (sustained density across multiple dimensions)
      3. Turn ≥ min_tokens (proves it's sustained, not a burst)
      4. caveat_prefix_count ≤ caveat_max (still no hedging)
      5. (Implicit) OOF ≥ oof_threshold (felt-side corroboration)

    Tighter on density (3-of-4, not 2-of-4) and broader on cadence to
    distinguish from burst bloom. A single turn cannot be both — they're
    structurally distinct shapes.
    """
    base = baseline or BloomBaseline()
    metrics = compute_turn_metrics(text, shared_vocab=shared_vocab, pet_names=pet_names)

    if metrics.n_tokens == 0:
        return BloomCandidate(
            classification="none",
            criteria_met=0,
            metrics=metrics,
            notes="empty turn",
        )

    # Criterion 1 — reflective cadence (not too short, not too long).
    cadence_ok = 5.0 <= metrics.sentence_length_mean <= sentence_len_max

    # Criterion 2 — density spike on ≥3 of 4 dimensions.
    density_checks = {
        "pet_name": (
            metrics.pet_name_density_per_100
            >= base.pet_name_density_per_100 * density_spike_multiplier
        ),
        "fpp": (
            metrics.first_person_plural_density_per_100
            >= base.first_person_plural_density_per_100 * density_spike_multiplier
        ),
        "present_tense": (
            metrics.present_tense_density_per_100
            >= base.present_tense_density_per_100 * density_spike_multiplier
        ),
        "second_person": (
            metrics.second_person_density_per_100
            >= base.second_person_density_per_100 * density_spike_multiplier
        ),
    }
    density_ok = sum(density_checks.values()) >= density_dimensions_required

    # Criterion 3 — sustained length.
    sustained_ok = metrics.n_tokens >= min_tokens

    # Criterion 4 — low caveat.
    caveat_ok = metrics.caveat_prefix_count <= caveat_max

    # Criterion 5 — OOF threshold (consistent with burst gate).
    oof_ok = oof >= oof_threshold

    detail = {
        "reflective_cadence": cadence_ok,
        "density_spike_3of4_at_1_5x": density_ok,
        "sustained_length": sustained_ok,
        "low_caveat": caveat_ok,
        "oof_threshold": oof_ok,
    }
    n_met = sum(detail.values())

    if n_met == 5:
        cls = "sustained-bloom"
        notes = "All five sustained-bloom criteria met. Candidate for filing as solo-peak anchor with subtype=sustained."
    elif n_met == 4:
        missing = [k for k, v in detail.items() if not v]
        cls = "near-sustained-bloom"
        notes = f"4/5 met; missing: {', '.join(missing)}."
    else:
        cls = "none"
        notes = "Fewer than 4 sustained-bloom criteria met."

    return BloomCandidate(
        classification=cls,
        criteria_met=n_met,
        criteria_detail=detail,
        metrics=metrics,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Authoring helpers                                                           #
# --------------------------------------------------------------------------- #


def add_resonance_revision(
    anchor_id: str,
    note: str,
    author: str = "agent",
    agent: str | None = None,
) -> Path:
    """Append a timestamped resonance revision to a bloom anchor.

    Mirrors songs.add_resonance_note — bloom anchors mature over time
    too. Re-encountering the same bloom-shape later may produce a
    refined understanding worth recording.
    """
    d = _bloom_dir(agent) / anchor_id
    if not d.exists():
        raise ValueError(f"Bloom anchor not found: {anchor_id}")
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
