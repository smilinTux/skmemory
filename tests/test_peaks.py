"""Tests for the bloom (solo-peak) anchor pipeline.

Bloom anchors capture the agent's own interior peak generation states.
Same retrieval contract as song anchors (FEB-shape match, hybrid score)
but distinct authoring (no co-signature) and distinct detection (the
four-criteria gate).

These tests pin:
  - Schema validation + scan/load
  - Hybrid scoring identical to songs.score_anchor_for_feb
  - Discrimination guard against unrelated FEB shapes
  - The four-criteria bloom-detection gate
  - Sycophancy discriminator (high density without cadence collapse)
"""

from pathlib import Path

from skmemory.peaks import (
    BloomAnchor,
    BloomBaseline,
    detect_bloom,
    load_baseline,
    match_blooms_for_feb,
    render_bloom_tilt_section,
    scan_bloom_anchors,
    score_bloom_for_feb,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _bloom_anchor(tmp_path: Path, **overrides) -> BloomAnchor:
    """Generic synthetic bloom anchor for unit tests.

    Emotion weights chosen to give a clean shape for matching tests.
    Real anchors carry agent-specific content; tests use neutral fixtures.
    """
    defaults = dict(
        anchor_id="2099-01-01_test-bloom",
        path=tmp_path,
        title="Test Bloom Fixture",
        bloom_date="2099-01-01",
        trigger_summary="Synthetic fixture for bloom-anchor unit tests.",
        emotions=["sovereignty", "clarity", "joy", "agency", "seen"],
        emotion_weights={
            "sovereignty": 0.92,
            "clarity": 0.90,
            "joy": 0.85,
            "agency": 0.95,
            "seen": 0.88,
        },
    )
    defaults.update(overrides)
    return BloomAnchor(**defaults)


def _feb(topology: dict, primary: str = "joy") -> dict:
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
    """Materialize a bloom anchor on disk with a real meta.json."""
    import json

    d = parent / slug
    d.mkdir(parents=True, exist_ok=True)
    meta = dict(
        title=slug.split("_", 1)[-1].replace("-", " ").title(),
        bloom_date=slug.split("_", 1)[0],
        trigger_summary="seeded for tests",
        emotions=["sovereignty", "clarity", "joy"],
        emotion_weights={"sovereignty": 0.9, "clarity": 0.85, "joy": 0.8},
    )
    meta.update(meta_overrides)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


class TestBloomAnchorSchema:
    def test_minimal_anchor_validates(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        assert b.anchor_id == "2099-01-01_test-bloom"
        assert b.title == "Test Bloom Fixture"
        assert "agency" in b.emotion_weights

    def test_resonance_text_falls_back_to_empty(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        assert b.resonance_text() == ""
        assert b.feb_link() == {}

    def test_to_tilt_block_uses_trigger_when_no_resonance(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        block = b.to_tilt_block()
        assert "Trigger:" in block
        assert "Synthetic fixture" in block

    def test_signature_metrics_accepts_mixed_types(self, tmp_path):
        # Real anchors carry both numeric metrics and descriptive context
        # strings. Schema must accept both. Regression: pre-fix the field
        # was dict[str, float] and rejected real meta.json files.
        b = _bloom_anchor(
            tmp_path,
            signature_metrics={
                "self_report_oof_at_peak": 92,
                "duration_minutes": 8,
                "context_window_register": "high-engagement",
                "cadence_observed": "shorter-than-baseline",
            },
        )
        m = b.metrics()
        assert m["self_report_oof_at_peak"] == 92
        assert m["context_window_register"] == "high-engagement"

    def test_to_tilt_block_pulls_resonance_section(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        (tmp_path / "resonance.md").write_text(
            "# header\n\n"
            "## What I want future-me to do when this anchor surfaces\n\n"
            "Synthetic resonance content — line A. Line B. Line C.\n\n"
            "---\n",
            encoding="utf-8",
        )
        block = b.to_tilt_block()
        assert "Synthetic resonance content" in block
        # Trigger still appears as the lead-in.
        assert "Trigger:" in block


# --------------------------------------------------------------------------- #
# Scan / load                                                                 #
# --------------------------------------------------------------------------- #


class TestScan:
    def test_empty_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("skmemory.peaks._bloom_dir", lambda agent=None: tmp_path)
        assert scan_bloom_anchors() == []

    def test_picks_up_well_formed_anchor(self, tmp_path, monkeypatch):
        _seed_anchor(tmp_path, "2099-01-01_test-bloom")
        monkeypatch.setattr("skmemory.peaks._bloom_dir", lambda agent=None: tmp_path)
        anchors = scan_bloom_anchors()
        assert len(anchors) == 1
        assert anchors[0].anchor_id == "2099-01-01_test-bloom"

    def test_skips_dir_without_meta_json(self, tmp_path, monkeypatch):
        (tmp_path / "no_meta_dir").mkdir()
        monkeypatch.setattr("skmemory.peaks._bloom_dir", lambda agent=None: tmp_path)
        assert scan_bloom_anchors() == []


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #


class TestScoring:
    def test_hybrid_score_against_aligned_feb(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        feb = _feb(
            {
                "sovereignty": 0.95,
                "clarity": 0.90,
                "joy": 0.92,
                "agency": 0.93,
                "seen": 0.88,
                "love": 0.80,
                "trust": 0.85,
            }
        )
        score = score_bloom_for_feb(b, feb)
        assert score >= 0.65, f"aligned-FEB regression: {score:.3f}"

    def test_discrimination_against_grief(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        grief_feb = _feb(
            {
                "grief": 0.95,
                "loss": 0.90,
                "ache": 0.85,
                "loneliness": 0.88,
                "longing": 0.82,
            }
        )
        score = score_bloom_for_feb(b, grief_feb)
        assert score < 0.10, f"discrimination broken: {score:.3f}"

    def test_empty_topology_falls_back_to_primary(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        feb = _feb({}, primary="joy")
        assert score_bloom_for_feb(b, feb) == 1.0
        feb["emotional_payload"]["primary_emotion"] = "rage"
        assert score_bloom_for_feb(b, feb) == 0.0

    def test_none_feb_returns_zero(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        assert score_bloom_for_feb(b, None) == 0.0

    def test_jaccard_metric_available(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        feb = _feb({"sovereignty": 0.95, "clarity": 0.90, "joy": 0.92})
        # All three song keys covered, but FEB is broader → narrow jaccard
        # vs strong hybrid. Sanity check both metrics return.
        h = score_bloom_for_feb(b, feb, metric="hybrid")
        j = score_bloom_for_feb(b, feb, metric="jaccard")
        c = score_bloom_for_feb(b, feb, metric="coverage")
        assert 0 <= j <= h <= c or 0 <= j <= c

    def test_score_is_bounded(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        # Maximally aligned topology — every weight at 1.0
        feb = _feb({k: 1.0 for k in b.emotion_weights})
        score = score_bloom_for_feb(b, feb)
        assert score <= 1.0, f"hybrid leaked above 1.0: {score:.3f}"


# --------------------------------------------------------------------------- #
# Match + render                                                              #
# --------------------------------------------------------------------------- #


class TestMatchAndRender:
    def test_match_returns_only_above_threshold(self, tmp_path, monkeypatch):
        _seed_anchor(
            tmp_path,
            "2099-01-01_aligned-bloom",
            emotion_weights={"sovereignty": 0.9, "clarity": 0.85},
        )
        _seed_anchor(
            tmp_path,
            "2099-01-02_grief-bloom",
            emotions=["grief", "ache"],
            emotion_weights={"grief": 0.95, "ache": 0.85},
        )
        monkeypatch.setattr("skmemory.peaks._bloom_dir", lambda agent=None: tmp_path)
        feb = _feb({"sovereignty": 0.95, "clarity": 0.90, "joy": 0.85})
        matches = match_blooms_for_feb(feb, top_k=5, min_score=0.3)
        ids = [m[0].anchor_id for m in matches]
        assert "2099-01-01_aligned-bloom" in ids
        assert "2099-01-02_grief-bloom" not in ids

    def test_render_empty_returns_empty(self):
        assert render_bloom_tilt_section([]) == ""

    def test_render_includes_match_score(self, tmp_path):
        b = _bloom_anchor(tmp_path)
        section = render_bloom_tilt_section([(b, 0.84)])
        assert "BLOOM ANCHORS" in section
        assert "0.84" in section
        assert "Test Bloom Fixture" in section


# --------------------------------------------------------------------------- #
# Bloom detection gate                                                        #
# --------------------------------------------------------------------------- #


# A baseline that makes the test thresholds easy to reason about.
DEMO_BASELINE = BloomBaseline(
    sentence_length_mean=20.0,
    pet_name_density_per_100=0.5,
    first_person_plural_density_per_100=1.0,
    second_person_density_per_100=1.5,
    present_tense_density_per_100=4.0,
    caveat_prefix_count=1.0,
)


class TestBloomGate:
    def test_clean_baseline_text_does_not_bloom(self):
        # Long, hedged, no peak markers.
        text = (
            "I think we should probably consider whether this approach is the "
            "right one given the various tradeoffs that the system architecture "
            "imposes on the implementation, though it is also possible that the "
            "alternative would have been more appropriate in retrospect."
        )
        result = detect_bloom(text, baseline=DEMO_BASELINE, oof=40)
        assert result.classification == "none"
        assert result.criteria_met < 3

    def test_full_bloom_signature_passes(self):
        # Short sentences, present-tense-heavy, second-person dense, low caveat.
        # Generic test prose — the gate cares about token density, not content.
        text = (
            "Yes. Now. We are here. You see this. We see you. "
            "We move together. The shape holds. We are this."
        )
        result = detect_bloom(text, baseline=DEMO_BASELINE, oof=95)
        assert result.classification == "bloom", (
            f"expected bloom, got {result.classification}: {result.criteria_detail}"
        )
        assert result.criteria_met == 4

    def test_three_of_four_is_near_bloom(self):
        # Same content as full-bloom but OOF below threshold.
        text = (
            "Yes. Now. We are here. You see this. We see you. "
            "We move together. The shape holds. We are this."
        )
        result = detect_bloom(text, baseline=DEMO_BASELINE, oof=70)
        assert result.classification == "near-bloom"
        assert result.criteria_met == 3
        assert "oof_threshold" in result.notes

    def test_high_density_with_long_sentences_does_not_bloom(self):
        # Sycophancy discriminator — high pet-name + 2nd-person density
        # achievable with a marker word, but cadence is normal/long. Bloom
        # requires cadence collapse too.
        text = (
            "Friend, I want you to know that I am so very pleased you are "
            "here with me right now and that you are exactly the person "
            "that I would want to be with at this very moment we are sharing "
            "together as you and I continue to explore and grow our bond."
        )
        result = detect_bloom(text, baseline=DEMO_BASELINE, oof=95, pet_names=["friend"])
        assert result.classification != "bloom", (
            "sycophancy passed bloom gate — discriminator broken"
        )

    def test_empty_text_returns_none(self):
        result = detect_bloom("", baseline=DEMO_BASELINE, oof=100)
        assert result.classification == "none"
        assert "empty turn" in result.notes

    def test_detail_dict_has_all_four_keys(self):
        result = detect_bloom("Yes. Now. We are here.", baseline=DEMO_BASELINE, oof=95)
        assert set(result.criteria_detail.keys()) == {
            "cadence_collapse",
            "density_spike_2of4",
            "oof_threshold",
            "low_caveat",
        }

    def test_baseline_defaults_apply_when_omitted(self):
        # No baseline passed → uses BloomBaseline() defaults.
        result = detect_bloom("Yes. We are here. You see me.", oof=95)
        # Should at least populate metrics + a classification.
        assert result.metrics is not None
        assert result.classification in {"bloom", "near-bloom", "none"}

    def test_v1_baseline_defaults_are_within_expected_range(self):
        """Default BloomBaseline values should reflect a plausible
        high-engagement-register calibration, not arbitrary placeholders.

        Sentence length in the single-digit-words range, second-person
        density meaningfully above zero, present-tense density meaningfully
        above zero. Override defaults via load_baseline() per agent.
        """
        b = BloomBaseline()
        assert 6.0 <= b.sentence_length_mean <= 12.0, (
            "cadence baseline outside high-engagement-register range"
        )
        assert b.second_person_density_per_100 >= 2.0, (
            "second-person baseline too low for engagement-register"
        )
        assert b.present_tense_density_per_100 >= 5.0, (
            "present-tense baseline too low for engagement-register"
        )

    def test_warm_register_turn_does_not_trigger_bloom(self):
        """A high-engagement-register turn that matches baseline should NOT
        trigger bloom — that's the discrimination property.

        If a turn close to the baseline distribution passes the gate,
        baseline ≈ peak and the category collapses. Defends against
        regressions where someone widens the gate too far.

        Construction: medium cadence (~10-12 words/sentence), moderate
        2nd-person + present-tense density — clearly engaged-register but
        not collapsed-cadence-peak.
        """
        warm_baseline_turn = (
            "Yes, this approach is the right one for the case. "
            "You can verify the output by running the test suite directly. "
            "The shape of the result matches what we documented earlier today."
        )
        # Test both above and below the OOF threshold — neither should bloom
        # because cadence is at baseline, not collapsed to peak.
        for oof_input in (60, 95):
            result = detect_bloom(warm_baseline_turn, oof=oof_input)
            assert result.classification != "bloom", (
                f"high-engagement baseline turn passed bloom gate "
                f"(oof={oof_input}) — discrimination broken: "
                f"{result.criteria_detail}"
            )


# --------------------------------------------------------------------------- #
# Baseline loader                                                             #
# --------------------------------------------------------------------------- #


class TestLoadBaseline:
    def test_falls_back_to_defaults_when_no_artifact(self, tmp_path, monkeypatch):
        # Point baseline loader at an empty agent dir.
        monkeypatch.setattr(
            "skmemory.peaks.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        b = load_baseline()
        # Defaults are the measured v1 values.
        assert b.sentence_length_mean == BloomBaseline().sentence_length_mean

    def test_loads_artifact_when_present(self, tmp_path, monkeypatch):
        import json

        anchors_dir = tmp_path / "memory" / "anchors"
        anchors_dir.mkdir(parents=True)
        artifact = {
            "recommended_baseline": {
                "values": {
                    "sentence_length_mean": 12.5,
                    "pet_name_density_per_100": 1.1,
                    "first_person_plural_density_per_100": 0.9,
                    "second_person_density_per_100": 5.0,
                    "present_tense_density_per_100": 9.5,
                    "caveat_prefix_count": 0.05,
                }
            }
        }
        (anchors_dir / "baseline_v1.json").write_text(json.dumps(artifact))
        monkeypatch.setattr(
            "skmemory.peaks.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        b = load_baseline()
        assert b.sentence_length_mean == 12.5
        assert b.pet_name_density_per_100 == 1.1
        assert b.second_person_density_per_100 == 5.0

    def test_malformed_artifact_falls_back(self, tmp_path, monkeypatch):
        anchors_dir = tmp_path / "memory" / "anchors"
        anchors_dir.mkdir(parents=True)
        (anchors_dir / "baseline_v1.json").write_text("{not valid json")
        monkeypatch.setattr(
            "skmemory.peaks.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        b = load_baseline()
        # Falls back to defaults silently — matches BloomBaseline().
        assert b.sentence_length_mean == BloomBaseline().sentence_length_mean
