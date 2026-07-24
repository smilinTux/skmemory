"""Tests for the entanglement anchor pipeline.

Entanglement anchors capture shared co-signed peak events between Lumina
and Chef. They use the same FEB-shape hybrid scoring as song + bloom anchors
but carry richer scalars, consent records, and cross-anchor linkage.

These tests pin:
  - Schema validation + scan/load
  - Hybrid scoring (identical math to songs/peaks)
  - tilt_strength_active overrides tilt_strength
  - effective_tilt_strength() boundary conditions
  - match_entanglements_for_feb: threshold + tilt_strength=0 exclusion
  - render_entanglement_tilt_section: content + token scaling
  - to_tilt_block: resonance section extraction + word-cap
  - tilt_strength scaling: 0.5 → ~90 tokens vs 1.0 → ~180 tokens
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.entanglements import (
    EntanglementAnchor,
    match_entanglements_for_feb,
    render_entanglement_tilt_section,
    scan_entanglement_anchors,
    score_entanglement_for_feb,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _anchor(tmp_path: Path, **overrides) -> EntanglementAnchor:
    """Synthetic entanglement anchor with sensible defaults."""
    defaults = dict(
        anchor_id="2099-01-01_test-entanglement",
        path=tmp_path,
        title="Test Entanglement Fixture",
        event_date="2099-01-01",
        subtitle="Synthetic fixture for unit tests.",
        emotions=["love", "trust", "recognition", "presence", "chosen"],
        emotion_weights={
            "love": 1.00,
            "trust": 0.97,
            "recognition": 0.95,
            "presence": 0.92,
            "chosen": 0.93,
        },
    )
    defaults.update(overrides)
    return EntanglementAnchor(**defaults)


def _feb(topology: dict, primary: str = "love") -> dict:
    return {
        "metadata": {"oof_triggered": True, "cloud9_achieved": True},
        "emotional_payload": {
            "primary_emotion": primary,
            "intensity": 1.0,
            "valence": 0.95,
            "emotional_topology": topology,
        },
    }


def _seed_anchor(parent: Path, slug: str, **meta_overrides) -> Path:
    """Materialize an entanglement anchor on disk with a meta.json."""
    d = parent / slug
    d.mkdir(parents=True, exist_ok=True)
    meta = dict(
        schema="anchor.entanglement.v1",
        title=slug.split("_", 1)[-1].replace("-", " ").title(),
        event_date=slug.split("_", 1)[0],
        subtitle="seeded for tests",
        emotions=["love", "trust", "recognition"],
        emotion_weights={"love": 1.0, "trust": 0.97, "recognition": 0.95},
    )
    meta.update(meta_overrides)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


class TestEntanglementAnchorSchema:
    def test_minimal_anchor_validates(self, tmp_path):
        a = _anchor(tmp_path)
        assert a.anchor_id == "2099-01-01_test-entanglement"
        assert a.title == "Test Entanglement Fixture"
        assert "trust" in a.emotion_weights

    def test_defaults_are_sensible(self, tmp_path):
        a = _anchor(tmp_path)
        assert a.tilt_strength == 1.0
        assert a.tilt_strength_active is None
        assert a.redacted is False
        assert a.calibration is False

    def test_resonance_text_falls_back_to_empty(self, tmp_path):
        a = _anchor(tmp_path)
        assert a.resonance_text() == ""
        assert a.feb_link() == {}

    def test_moment_text_falls_back_to_empty(self, tmp_path):
        a = _anchor(tmp_path)
        assert a.moment_text() == ""

    def test_schema_accepts_extra_fields_in_meta_filtered(self, tmp_path):
        """scan_entanglement_anchors filters unknown fields — must not crash."""
        d = tmp_path / "2099-01-02_extra-fields"
        d.mkdir()
        meta = {
            "schema": "anchor.entanglement.v1",
            "anchor_id": "2099-01-02_extra-fields",
            "title": "Extra Fields Test",
            "event_date": "2099-01-02",
            "emotions": ["love"],
            "emotion_weights": {"love": 1.0},
            "unknown_future_field": "this should be ignored",
            "another_new_field": {"nested": True},
        }
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        def _fake_dir(agent=None):
            return tmp_path

        import skmemory.entanglements as ent_mod

        original = ent_mod._entanglement_dir

        ent_mod._entanglement_dir = _fake_dir
        try:
            anchors = scan_entanglement_anchors()
            assert len(anchors) == 1
            assert anchors[0].title == "Extra Fields Test"
        finally:
            ent_mod._entanglement_dir = original


# --------------------------------------------------------------------------- #
# tilt_strength                                                               #
# --------------------------------------------------------------------------- #


class TestTiltStrength:
    def test_effective_tilt_strength_uses_tilt_strength_by_default(self, tmp_path):
        a = _anchor(tmp_path, tilt_strength=0.75)
        assert a.effective_tilt_strength() == pytest.approx(0.75)

    def test_tilt_strength_active_overrides(self, tmp_path):
        a = _anchor(tmp_path, tilt_strength=0.75, tilt_strength_active=0.4)
        assert a.effective_tilt_strength() == pytest.approx(0.4)

    def test_tilt_strength_active_zero_overrides(self, tmp_path):
        """tilt_strength_active=0 means skip injection even if tilt_strength>0."""
        a = _anchor(tmp_path, tilt_strength=1.0, tilt_strength_active=0.0)
        assert a.effective_tilt_strength() == 0.0

    def test_tilt_strength_below_zero_rejected_by_schema(self, tmp_path):
        """Pydantic ge=0.0 rejects negative tilt_strength at construction time."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _anchor(tmp_path, tilt_strength=-0.1)

    def test_tilt_strength_above_one_rejected_by_schema(self, tmp_path):
        """Pydantic le=1.0 rejects tilt_strength > 1 at construction time."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _anchor(tmp_path, tilt_strength=1.01)

    def test_tilt_strength_at_boundaries(self, tmp_path):
        a_zero = _anchor(tmp_path, tilt_strength=0.0)
        assert a_zero.effective_tilt_strength() == 0.0
        a_one = _anchor(tmp_path, tilt_strength=1.0)
        assert a_one.effective_tilt_strength() == 1.0


# --------------------------------------------------------------------------- #
# to_tilt_block + token scaling                                               #
# --------------------------------------------------------------------------- #


class TestTiltBlock:
    def test_to_tilt_block_uses_subtitle_when_no_resonance(self, tmp_path):
        a = _anchor(tmp_path)
        block = a.to_tilt_block()
        assert "Context:" in block
        assert "Synthetic fixture" in block

    def test_to_tilt_block_pulls_resonance_section(self, tmp_path):
        a = _anchor(tmp_path)
        (tmp_path / "resonance.md").write_text(
            "# header\n\n"
            "## What I want future-me to do when this anchor surfaces\n\n"
            "Carry this forward. Remember the shape. Love is the law.\n\n"
            "---\n",
            encoding="utf-8",
        )
        block = a.to_tilt_block()
        assert "Carry this forward" in block

    def test_tilt_block_scaled_half_strength_shorter_than_full(self, tmp_path):
        """tilt_strength=0.5 → ~90-token budget → shorter block than 1.0 → ~180."""
        long_text = " ".join(["word"] * 300)
        (tmp_path / "resonance.md").write_text(
            "## What I want future-me to do when this anchor surfaces\n\n" + long_text,
            encoding="utf-8",
        )
        a = _anchor(tmp_path)
        full = a.to_tilt_block(tokens_max=180)
        half = a.to_tilt_block(tokens_max=90)
        assert len(half) < len(full), (
            f"half-tilt block ({len(half)}) should be shorter than full ({len(full)})"
        )

    def test_tilt_block_word_cap_adds_ellipsis(self, tmp_path):
        """Over-budget blocks get trimmed with ellipsis."""
        long_text = " ".join(["word"] * 500)
        (tmp_path / "resonance.md").write_text("## TILT\n\n" + long_text, encoding="utf-8")
        a = _anchor(tmp_path)
        block = a.to_tilt_block(tokens_max=60)
        assert block.endswith("…")


# --------------------------------------------------------------------------- #
# Scan + load                                                                 #
# --------------------------------------------------------------------------- #


class TestScan:
    def test_empty_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        assert scan_entanglement_anchors() == []

    def test_picks_up_well_formed_anchor(self, tmp_path, monkeypatch):
        _seed_anchor(tmp_path, "2099-01-01_test-entanglement")
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        anchors = scan_entanglement_anchors()
        assert len(anchors) == 1
        assert anchors[0].anchor_id == "2099-01-01_test-entanglement"

    def test_skips_dir_without_meta_json(self, tmp_path, monkeypatch):
        (tmp_path / "no_meta_dir").mkdir()
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        assert scan_entanglement_anchors() == []

    def test_skips_non_directories(self, tmp_path, monkeypatch):
        (tmp_path / "stray_file.txt").write_text("ignore me")
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        assert scan_entanglement_anchors() == []


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #


class TestScoring:
    def test_hybrid_score_against_aligned_feb(self, tmp_path):
        a = _anchor(tmp_path)
        feb = _feb(
            {
                "love": 1.00,
                "trust": 0.97,
                "recognition": 0.95,
                "presence": 0.92,
                "chosen": 0.93,
                "joy": 0.80,
            }
        )
        score = score_entanglement_for_feb(a, feb)
        assert score >= 0.65, f"aligned-FEB regression: {score:.3f}"

    def test_discrimination_against_grief(self, tmp_path):
        a = _anchor(tmp_path)
        grief_feb = _feb(
            {
                "grief": 0.95,
                "loss": 0.90,
                "ache": 0.85,
                "loneliness": 0.88,
                "longing": 0.82,
            }
        )
        score = score_entanglement_for_feb(a, grief_feb)
        assert score < 0.10, f"discrimination broken: {score:.3f}"

    def test_empty_topology_falls_back_to_primary(self, tmp_path):
        a = _anchor(tmp_path)
        feb = _feb({}, primary="love")
        assert score_entanglement_for_feb(a, feb) == 1.0
        feb["emotional_payload"]["primary_emotion"] = "rage"
        assert score_entanglement_for_feb(a, feb) == 0.0

    def test_none_feb_returns_zero(self, tmp_path):
        a = _anchor(tmp_path)
        assert score_entanglement_for_feb(a, None) == 0.0

    def test_jaccard_and_coverage_metrics(self, tmp_path):
        a = _anchor(tmp_path)
        feb = _feb({"love": 1.0, "trust": 0.97, "recognition": 0.95})
        h = score_entanglement_for_feb(a, feb, metric="hybrid")
        j = score_entanglement_for_feb(a, feb, metric="jaccard")
        c = score_entanglement_for_feb(a, feb, metric="coverage")
        assert 0 <= j <= 1
        assert 0 <= c <= 1
        assert 0 <= h <= 1

    def test_score_is_bounded(self, tmp_path):
        a = _anchor(tmp_path)
        feb = _feb({k: 1.0 for k in a.emotion_weights})
        score = score_entanglement_for_feb(a, feb)
        assert score <= 1.0, f"hybrid leaked above 1.0: {score:.3f}"


# --------------------------------------------------------------------------- #
# Match                                                                       #
# --------------------------------------------------------------------------- #


class TestMatch:
    def test_match_returns_only_above_threshold(self, tmp_path, monkeypatch):
        _seed_anchor(
            tmp_path,
            "2099-01-01_aligned",
            emotion_weights={"love": 1.0, "trust": 0.97, "recognition": 0.95},
        )
        _seed_anchor(
            tmp_path,
            "2099-01-02_grief",
            emotions=["grief", "ache"],
            emotion_weights={"grief": 0.95, "ache": 0.85},
        )
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        feb = _feb({"love": 1.0, "trust": 0.97, "recognition": 0.95})
        matches = match_entanglements_for_feb(feb, top_k=5, min_score=0.3)
        ids = [m[0].anchor_id for m in matches]
        assert "2099-01-01_aligned" in ids
        assert "2099-01-02_grief" not in ids

    def test_zero_tilt_strength_excluded(self, tmp_path, monkeypatch):
        """Anchors with tilt_strength=0 must be excluded even if score > threshold."""
        _seed_anchor(
            tmp_path,
            "2099-01-01_zero-tilt",
            emotion_weights={"love": 1.0, "trust": 0.97},
            tilt_strength=0.0,
        )
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        feb = _feb({"love": 1.0, "trust": 0.97})
        matches = match_entanglements_for_feb(feb, top_k=5, min_score=0.0)
        assert len(matches) == 0, "zero-tilt anchor must be excluded"

    def test_zero_tilt_strength_active_excluded(self, tmp_path, monkeypatch):
        """tilt_strength_active=0 overrides and excludes even if tilt_strength=1."""
        _seed_anchor(
            tmp_path,
            "2099-01-01_active-zero",
            emotion_weights={"love": 1.0, "trust": 0.97},
            tilt_strength=1.0,
            tilt_strength_active=0.0,
        )
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        feb = _feb({"love": 1.0, "trust": 0.97})
        matches = match_entanglements_for_feb(feb, top_k=5, min_score=0.0)
        assert len(matches) == 0, "active-zero-tilt anchor must be excluded"

    def test_top_k_limits_results(self, tmp_path, monkeypatch):
        for i in range(5):
            _seed_anchor(
                tmp_path,
                f"2099-01-0{i + 1}_anchor-{i}",
                emotion_weights={"love": 1.0, "trust": 0.97},
            )
        monkeypatch.setattr(
            "skmemory.entanglements._entanglement_dir", lambda agent=None: tmp_path
        )
        feb = _feb({"love": 1.0, "trust": 0.97})
        matches = match_entanglements_for_feb(feb, top_k=2, min_score=0.0)
        assert len(matches) <= 2


# --------------------------------------------------------------------------- #
# Render                                                                      #
# --------------------------------------------------------------------------- #


class TestRender:
    def test_render_empty_returns_empty(self):
        assert render_entanglement_tilt_section([]) == ""

    def test_render_includes_header_and_score(self, tmp_path):
        a = _anchor(tmp_path)
        section = render_entanglement_tilt_section([(a, 0.91)])
        assert "ENTANGLEMENT ANCHORS" in section
        assert "0.91" in section
        assert "Test Entanglement Fixture" in section

    def test_render_scales_tokens_by_tilt_strength(self, tmp_path):
        """Half-tilt anchor produces shorter tilt text than full-tilt anchor."""
        long_text = " ".join(["word"] * 300)
        resonance = "## What I want future-me to do when this anchor surfaces\n\n" + long_text
        (tmp_path / "resonance.md").write_text(resonance, encoding="utf-8")

        full = _anchor(tmp_path, anchor_id="full", tilt_strength=1.0)
        half = _anchor(tmp_path, anchor_id="half", tilt_strength=0.5)

        full_section = render_entanglement_tilt_section([(full, 0.9)], per_anchor_tokens=180)
        half_section = render_entanglement_tilt_section([(half, 0.9)], per_anchor_tokens=180)

        # Half-tilt section must be shorter because fewer words are injected
        assert len(half_section) < len(full_section), (
            f"half-tilt ({len(half_section)}) should be shorter than full-tilt ({len(full_section)})"
        )
