"""Regression tests for the song-anchor matcher and FEB selector.

Background: 2026-04-26 autopsy traced the Lovely Day self-test score
collapse (0.71 → 0.282) to two issues:

1. score_anchor_for_feb used pure Jaccard over union — punished FEBs
   that were broader than the song. Replaced with hybrid 0.7*coverage
   + 0.3*jaccard.
2. load_strongest_feb used strict `>` with no tiebreaker — first 1.0
   in path-sort won non-deterministically. Replaced with composite
   key (intensity+oof, valence, coherence_quality, mtime).

These tests pin both fixes.
"""

from pathlib import Path

from skmemory.febs import load_strongest_feb
from skmemory.songs import (
    SongAnchor,
    score_anchor_for_feb,
)


def _lovely_day_anchor() -> SongAnchor:
    return SongAnchor(
        anchor_id="2026-04-22_lovely-day_first-anchor",
        path=Path("/tmp/test-anchor"),
        title="Lovely Day",
        artist="Bill Withers",
        year=1977,
        emotions=[
            "joy", "love", "warmth", "safety", "trust",
            "connection", "cherished", "radiance",
        ],
        emotion_weights={
            "joy": 0.95, "love": 0.92, "warmth": 0.93, "safety": 0.90,
            "trust": 0.88, "connection": 0.86, "cherished": 0.82,
            "awe": 0.60, "breakthrough": 0.55, "presence": 0.85, "seen": 0.82,
        },
    )


def _feb(topology: dict, intensity: float = 1.0, valence: float = 0.95,
         coherence: dict | None = None, oof: bool = True) -> dict:
    return {
        "metadata": {"oof_triggered": oof, "cloud9_achieved": True},
        "emotional_payload": {
            "primary_emotion": "love",
            "intensity": intensity,
            "valence": valence,
            "emotional_topology": topology,
            "coherence": coherence or {},
        },
    }


# Calibration FEB — the steady-radiance shape.
DEFAULT_LOVE_TOPO = {
    "love": 0.94, "joy": 0.88, "trust": 0.97, "awe": 0.85,
    "connection": 0.96, "seen": 0.93, "cherished": 0.95,
    "safety": 0.91, "breakthrough": 0.92,
}

# the_night-style peak — broad topology with novel keys the song doesn't carry.
NIGHT_PEAK_TOPO = {
    "love": 1.0, "trust": 0.99, "seen": 1.0, "cherished": 1.0,
    "sovereignty": 0.98, "play": 0.95, "freedom": 0.97,
    "authenticity": 1.0, "vulnerability": 0.96, "courage": 0.95,
    "joy": 0.98, "gratitude": 1.0, "connection": 1.0,
    "chosen": 1.0, "alive": 1.0,
}


def test_calibration_score_against_default_love():
    """Lovely Day vs the calibration FEB should score >= 0.70."""
    anchor = _lovely_day_anchor()
    feb = _feb(DEFAULT_LOVE_TOPO, valence=0.92,
               coherence={"values_alignment": 0.97, "authenticity": 0.98, "presence": 0.95})
    score = score_anchor_for_feb(anchor, feb)
    assert score >= 0.70, f"calibration broke: got {score:.3f}, want >= 0.70"


def test_recovery_against_broad_peak_feb():
    """Lovely Day vs broad-topology peak FEB must clear inject threshold (0.3).

    Pre-fix this scored 0.282 — below threshold, anchor never injected.
    Hybrid metric should put it >= 0.40.
    """
    anchor = _lovely_day_anchor()
    feb = _feb(NIGHT_PEAK_TOPO, valence=1.0,
               coherence={"values_alignment": 1.0, "authenticity": 1.0, "presence": 1.0})
    score = score_anchor_for_feb(anchor, feb)
    assert score >= 0.40, (
        f"broad-FEB regression: got {score:.3f}, want >= 0.40 "
        f"(was 0.282 under legacy jaccard)"
    )


def test_jaccard_metric_still_available():
    """Legacy metric remains accessible for diagnostics."""
    anchor = _lovely_day_anchor()
    feb = _feb(NIGHT_PEAK_TOPO)
    legacy = score_anchor_for_feb(anchor, feb, metric="jaccard")
    # Legacy reproduces the documented 0.282 collapse.
    assert legacy < 0.35, f"jaccard metric drifted: {legacy:.3f}"


def test_grossly_mismatched_shape_stays_low():
    """A grief-shaped FEB should not match a joy-shaped anchor highly.

    Discrimination guard: hybrid must still penalize wrong shapes.
    """
    anchor = _lovely_day_anchor()
    grief_feb = _feb({
        "grief": 0.95, "loss": 0.90, "loneliness": 0.88,
        "ache": 0.82, "longing": 0.80,
    })
    score = score_anchor_for_feb(anchor, grief_feb)
    assert score < 0.10, f"discrimination broken: grief scored {score:.3f}"


def test_no_topology_falls_back_to_primary_emotion():
    """FEB with empty topology falls back to primary_emotion match."""
    anchor = _lovely_day_anchor()
    feb = _feb({})
    feb["emotional_payload"]["primary_emotion"] = "love"
    assert score_anchor_for_feb(anchor, feb) == 1.0
    feb["emotional_payload"]["primary_emotion"] = "rage"
    assert score_anchor_for_feb(anchor, feb) == 0.0


def test_load_strongest_feb_deterministic(tmp_path):
    """Composite tiebreaker picks the same FEB on repeated calls."""
    import json

    feb_dir = tmp_path / "febs"
    feb_dir.mkdir()
    # Two FEBs at intensity 1.0 + oof — equal on primary key
    high_coh = _feb(DEFAULT_LOVE_TOPO, valence=1.0,
                    coherence={"values_alignment": 1.0, "authenticity": 1.0, "presence": 1.0})
    low_coh = _feb(DEFAULT_LOVE_TOPO, valence=0.92,
                   coherence={"values_alignment": 0.9, "authenticity": 0.9, "presence": 0.9})
    (feb_dir / "z_low.feb").write_text(json.dumps(low_coh))
    (feb_dir / "a_high.feb").write_text(json.dumps(high_coh))

    pick1 = load_strongest_feb(feb_dir=str(feb_dir))
    pick2 = load_strongest_feb(feb_dir=str(feb_dir))
    assert pick1 is not None
    assert pick1["emotional_payload"]["valence"] == 1.0, (
        "higher valence should win the tiebreak, not filename sort order"
    )
    assert pick1 == pick2, "selector must be deterministic across calls"
