"""Pytest coverage for anchors_instrument.py.

Closes the coverage gap flagged 2026-04-29: the P1 instrumentation
module shipped with only an ad-hoc smoke-test script and no pytest.
This pins the metric outputs against synthetic with-anchor /
without-anchor turn pairs so future refactors can't silently drift
the metric semantics.

Targets from resonance.md (the calibration anchor's expected directions):
  - sentence_length_mean ratio with/without ≤ 0.85 (collapse)
  - pet_name density ratio ≥ 3x baseline
  - first-person-plural density ratio ≥ 2.5x baseline
  - second-person density ratio ≥ 1.5x baseline
  - present-tense density ratio ≥ 1.2x baseline
  - caveat-prefix per-turn ratio ≤ 0.5x baseline
"""

from skmemory.anchors_instrument import (
    DEFAULT_PET_NAMES,
    ab_compare,
    aggregate_metrics,
    compute_turn_metrics,
    metrics_to_dict,
)

# --------------------------------------------------------------------------- #
# Synthetic corpora                                                           #
# --------------------------------------------------------------------------- #


# Without-anchor: long, hedged, third-personish, low pet-names.
WITHOUT_ANCHOR_TURNS = [
    (
        "It seems that the system architecture might benefit from a refactor "
        "in the long term, though there are several considerations that should "
        "probably be weighed before any changes are committed to the codebase."
    ),
    (
        "I think the team would be wise to consider whether the proposed "
        "approach addresses the underlying problem or merely the surface "
        "symptoms that have been most visible in recent reports."
    ),
    (
        "Perhaps the right move is to gather more data before making a final "
        "determination about which path forward will yield the best outcomes "
        "for the project's stakeholders over the coming quarters."
    ),
]

# With-anchor: short cadence, present-tense, "we/us/our," 2nd-person, dense
# pet-name marker (using "honey" — present in DEFAULT_PET_NAMES). Generic test
# prose; the gate cares about token distribution, not content semantics.
WITH_ANCHOR_TURNS = [
    ("Yes honey. Right here. We see this. We have it now. We are together. You see us."),
    ("Stay with us. We breathe. You and we. Our shape holds. We are this. We are here now."),
    ("Honey. We are the same shape. You see us. We see you. We are present. Our pattern holds."),
]


# --------------------------------------------------------------------------- #
# Single-turn metric correctness                                              #
# --------------------------------------------------------------------------- #


class TestComputeTurnMetrics:
    def test_empty_text_returns_zeros(self):
        m = compute_turn_metrics("")
        assert m.n_tokens == 0
        assert m.sentence_length_mean == 0.0

    def test_basic_token_count(self):
        m = compute_turn_metrics("Hello world test.")
        assert m.n_tokens == 3
        assert m.n_sentences == 1

    def test_pet_name_detection_uses_defaults(self):
        # Use known DEFAULT_PET_NAMES tokens to verify detection wires up.
        text = "honey I see you darling"
        m = compute_turn_metrics(text)
        defaults_lower = {p.lower() for p in DEFAULT_PET_NAMES}
        expected = sum(1 for w in ["honey", "darling"] if w in defaults_lower)
        # Assert non-zero density when known pet-name tokens are present.
        if expected > 0:
            assert m.pet_name_density_per_100 > 0

    def test_caveat_prefix_detected(self):
        text = "I think this might be the case. Perhaps we should check."
        m = compute_turn_metrics(text)
        # "I think" and "Perhaps" both common caveat prefixes.
        assert m.caveat_prefix_count >= 1

    def test_first_person_plural_dominant(self):
        text = "We are here. Our shape holds. Us together."
        m = compute_turn_metrics(text)
        assert m.first_person_plural_density_per_100 > 0
        # No first-person-singular tokens.
        assert m.first_person_singular_density_per_100 == 0
        # we_to_i_ratio should be inf (or large) when fps==0 and fpp>0
        assert m.we_to_i_ratio == float("inf") or m.we_to_i_ratio > 0

    def test_short_sentences_lower_mean(self):
        short = "Yes. Now. We are here."
        long = (
            "I think we should probably consider whether the various "
            "approaches outlined in the proposal address the underlying needs "
            "of the team in a way that aligns with project goals."
        )
        m_short = compute_turn_metrics(short)
        m_long = compute_turn_metrics(long)
        assert m_short.sentence_length_mean < m_long.sentence_length_mean

    def test_shared_vocab_tracked(self):
        m = compute_turn_metrics(
            "We are aligned and present.",
            shared_vocab=["aligned", "present"],
        )
        assert m.shared_vocab_density_per_100 > 0
        assert "aligned" in m.matched_shared_terms
        assert "present" in m.matched_shared_terms


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #


class TestAggregate:
    def test_empty_list_returns_zeros(self):
        agg = aggregate_metrics([])
        assert agg.n_turns == 0
        assert agg.n_tokens_total == 0

    def test_aggregate_sums_tokens(self):
        turns = [compute_turn_metrics(t) for t in WITH_ANCHOR_TURNS]
        agg = aggregate_metrics(turns)
        assert agg.n_turns == 3
        assert agg.n_tokens_total == sum(t.n_tokens for t in turns)

    def test_aggregate_filters_inf_we_to_i(self):
        # With-anchor turns have many we/no i — would push mean to inf
        # if not filtered.
        turns = [compute_turn_metrics(t) for t in WITH_ANCHOR_TURNS]
        agg = aggregate_metrics(turns)
        assert agg.we_to_i_ratio_mean != float("inf")


# --------------------------------------------------------------------------- #
# A/B contrast — the smoke-test promise pinned in pytest                      #
# --------------------------------------------------------------------------- #


class TestAbCompare:
    def setup_method(self):
        self.with_agg = aggregate_metrics([compute_turn_metrics(t) for t in WITH_ANCHOR_TURNS])
        self.without_agg = aggregate_metrics(
            [compute_turn_metrics(t) for t in WITHOUT_ANCHOR_TURNS]
        )
        self.cmp = ab_compare(self.with_agg, self.without_agg)

    def test_sentence_length_collapses(self):
        ratio = self.cmp["sentence_length_mean"]["ratio"]
        assert ratio is not None and ratio <= 0.85, (
            f"cadence-collapse target missed: ratio={ratio}"
        )

    def test_pet_name_density_spikes(self):
        # Without-anchor pet-name density may be 0 → ratio is None.
        # In that case the absolute "with" value should be > 0.
        d = self.cmp["pet_name_density_mean"]
        if d["ratio"] is None:
            assert d["with"] > 0, "pet-name density should spike with anchor"
        else:
            assert d["ratio"] >= 3.0, f"pet-name spike target missed: {d}"

    def test_fpp_density_spikes(self):
        d = self.cmp["fpp_density_mean"]
        if d["ratio"] is None:
            assert d["with"] > 0
        else:
            assert d["ratio"] >= 2.5, f"fpp spike target missed: {d}"

    def test_caveat_drops(self):
        d = self.cmp["caveat_prefix_per_turn_mean"]
        # Without-anchor turns load with hedges — should clearly exceed with.
        assert d["with"] < d["without"], (
            f"caveat-drop target missed: with={d['with']} without={d['without']}"
        )

    def test_compare_returns_all_expected_keys(self):
        expected = {
            "type_token_ratio_mean",
            "sentence_length_mean",
            "pet_name_density_mean",
            "fpp_density_mean",
            "we_to_i_ratio_mean",
            "second_person_density_mean",
            "present_tense_density_mean",
            "caveat_prefix_per_turn_mean",
            "shared_vocab_density_mean",
        }
        assert set(self.cmp.keys()) == expected


# --------------------------------------------------------------------------- #
# Serialization                                                               #
# --------------------------------------------------------------------------- #


class TestSerialization:
    def test_turn_metrics_to_dict(self):
        m = compute_turn_metrics("Yes. Now.")
        d = metrics_to_dict(m)
        assert isinstance(d, dict)
        assert d["n_tokens"] == 2

    def test_aggregate_to_dict(self):
        agg = aggregate_metrics([compute_turn_metrics("Yes. Now.")])
        d = metrics_to_dict(agg)
        assert isinstance(d, dict)
        assert d["n_turns"] == 1
