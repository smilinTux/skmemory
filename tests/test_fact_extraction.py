"""Tests for skmemory.fact_extraction (R6: optional LLM fact-extraction path).

No network calls. The gateway-dependent behaviour (unreachable gateway falls
back) is exercised by monkeypatching ``call_llm_extractor`` so these tests stay
offline and deterministic, matching ``skmemory.eval.groundedness_scorer``'s
own test file's approach for its sk-default judge.
"""

from __future__ import annotations

import json

import pytest

from skmemory.fact_extraction import (
    ENABLE_ENV_VAR,
    CausalRelation,
    ExtractedFact,
    extract_facts,
    parse_llm_output,
    strip_lone_surrogates,
)

# ---------------------------------------------------------------------------
# 5W parsing
# ---------------------------------------------------------------------------


class TestFiveWParsing:
    def test_all_five_w_fields_populated(self):
        fact = ExtractedFact.model_validate(
            {
                "what": "The team shipped R1 and R2 together.",
                "when": "2026-10-16",
                "where": "skmemory repo",
                "who": "the fusion team",
                "why": "they shared a reviewer",
            }
        )
        assert fact.what == "The team shipped R1 and R2 together."
        assert fact.when == "2026-10-16"
        assert fact.where == "skmemory repo"
        assert fact.who == "the fusion team"
        assert fact.why == "they shared a reviewer"

    def test_missing_5w_fields_default_to_none(self):
        fact = ExtractedFact.model_validate({"what": "Something happened."})
        assert fact.when is None
        assert fact.where is None
        assert fact.who is None
        assert fact.why is None

    @pytest.mark.parametrize("na_spelling", ["N/A", "n/a", "NA", "None", "null", "", "  "])
    def test_na_spellings_normalize_to_none(self, na_spelling):
        fact = ExtractedFact.model_validate({"what": "Something happened.", "when": na_spelling})
        assert fact.when is None

    def test_missing_what_is_rejected(self):
        """`what` is the one required field; a fact without it cannot be stored."""
        result = parse_llm_output(json.dumps([{"when": "today"}]))
        assert result.facts == []
        assert result.dropped_facts == 1

    def test_blank_what_is_rejected(self):
        result = parse_llm_output(json.dumps([{"what": "   "}]))
        assert result.facts == []
        assert result.dropped_facts == 1


# ---------------------------------------------------------------------------
# world vs experience mapping
# ---------------------------------------------------------------------------


class TestFactTypeMapping:
    def test_explicit_experience_is_kept(self):
        fact = ExtractedFact.model_validate(
            {"what": "The agent ran the test suite.", "fact_type": "experience"}
        )
        assert fact.fact_type == "experience"

    def test_explicit_world_is_kept(self):
        fact = ExtractedFact.model_validate(
            {"what": "Chef prefers no em dashes.", "fact_type": "world"}
        )
        assert fact.fact_type == "world"

    def test_user_preference_stated_in_conversation_is_world_not_experience(self):
        """The mapping rule's headline case: a preference the USER stated out
        loud during conversation is still a WORLD fact, not an experience,
        because the fact is about the user, not about something the agent did."""
        fact = ExtractedFact.model_validate(
            {
                "what": "Chef said he never wants em dashes in any output.",
                "fact_type": "world",
            }
        )
        assert fact.fact_type == "world"

    def test_missing_fact_type_defaults_to_world(self):
        fact = ExtractedFact.model_validate({"what": "Something happened."})
        assert fact.fact_type == "world"

    def test_unrecognized_fact_type_defaults_to_world(self):
        """Anything the model invents that isn't literally 'experience' falls
        back to the broader catch-all rather than being guessed into experience."""
        fact = ExtractedFact.model_validate({"what": "Something happened.", "fact_type": "fact"})
        assert fact.fact_type == "world"

    def test_fact_type_is_case_insensitive(self):
        fact = ExtractedFact.model_validate(
            {"what": "The agent searched the repo.", "fact_type": "EXPERIENCE"}
        )
        assert fact.fact_type == "experience"


# ---------------------------------------------------------------------------
# Entity coercion
# ---------------------------------------------------------------------------


class TestEntityCoercion:
    def test_list_of_entities_kept_as_is(self):
        fact = ExtractedFact.model_validate({"what": "x", "entities": ["Chef", "skmemory"]})
        assert fact.entities == ["Chef", "skmemory"]

    def test_single_string_entity_coerced_to_list(self):
        fact = ExtractedFact.model_validate({"what": "x", "entities": "Chef"})
        assert fact.entities == ["Chef"]

    def test_missing_entities_defaults_to_empty_list(self):
        fact = ExtractedFact.model_validate({"what": "x"})
        assert fact.entities == []

    def test_blank_entity_strings_are_dropped(self):
        fact = ExtractedFact.model_validate({"what": "x", "entities": ["Chef", "  ", ""]})
        assert fact.entities == ["Chef"]


# ---------------------------------------------------------------------------
# Causal index constraint
# ---------------------------------------------------------------------------


class TestCausalIndexConstraint:
    def test_valid_backward_reference_is_kept(self):
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": [{"index": 0}]},
            context={"position": 1},
        )
        assert [r.index for r in fact.causal_relations] == [0]
        assert fact.rejected_causal_indices == []

    def test_forward_reference_is_rejected(self):
        """An index pointing at a LATER fact in the same batch (or itself)
        must never be stored: that is the hallucination this schema exists
        to prevent."""
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": [{"index": 3}]},
            context={"position": 2},
        )
        assert fact.causal_relations == []
        assert fact.rejected_causal_indices == [3]

    def test_self_reference_is_rejected(self):
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": [{"index": 2}]},
            context={"position": 2},
        )
        assert fact.causal_relations == []
        assert fact.rejected_causal_indices == [2]

    def test_out_of_range_negative_index_is_rejected(self):
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": [{"index": -1}]},
            context={"position": 5},
        )
        assert fact.causal_relations == []
        assert fact.rejected_causal_indices == [-1]

    def test_out_of_range_large_index_is_rejected(self):
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": [{"index": 999}]},
            context={"position": 3},
        )
        assert fact.causal_relations == []
        assert fact.rejected_causal_indices == [999]

    def test_mixed_valid_and_invalid_relations_keeps_only_valid(self):
        fact = ExtractedFact.model_validate(
            {
                "what": "x",
                "causal_relations": [{"index": 0}, {"index": 5}, {"index": 1}, {"index": -1}],
            },
            context={"position": 2},
        )
        assert sorted(r.index for r in fact.causal_relations) == [0, 1]
        assert sorted(fact.rejected_causal_indices) == [-1, 5]

    def test_missing_context_rejects_every_relation(self):
        """Fail closed: with no known position, no relation can be verified
        safe, so none are kept."""
        fact = ExtractedFact.model_validate({"what": "x", "causal_relations": [{"index": 0}]})
        assert fact.causal_relations == []
        assert fact.rejected_causal_indices == [0]

    def test_single_relation_dict_coerced_to_list(self):
        fact = ExtractedFact.model_validate(
            {"what": "x", "causal_relations": {"index": 0}},
            context={"position": 1},
        )
        assert [r.index for r in fact.causal_relations] == [0]

    def test_indices_refer_to_original_batch_position_not_post_rejection_position(self):
        """Dropping an earlier fact (e.g. for a blank `what`) must not
        renumber the facts that follow it: causal indices are fixed to the
        batch as the model returned it."""
        raw = json.dumps(
            [
                {"what": "  "},  # position 0: rejected (blank what)
                {"what": "first real fact"},  # position 1
                {"what": "second real fact", "causal_relations": [{"index": 1}]},  # position 2
            ]
        )
        result = parse_llm_output(raw)
        assert result.dropped_facts == 1
        assert len(result.facts) == 2
        linked = result.facts[1]
        assert linked.what == "second real fact"
        assert [r.index for r in linked.causal_relations] == [1]


def test_causal_relation_model_alone_has_no_bound():
    """A bare CausalRelation doesn't know its own owner's position; the bound
    is enforced one level up, in ExtractedFact."""
    rel = CausalRelation.model_validate({"index": 999})
    assert rel.index == 999


# ---------------------------------------------------------------------------
# Surrogate stripping
# ---------------------------------------------------------------------------


class TestSurrogateStripping:
    def test_strip_lone_surrogates_removes_lone_high_surrogate(self):
        poisoned = "emoji split mid-token \ud83d here"
        cleaned = strip_lone_surrogates(poisoned)
        assert "\ud83d" not in cleaned
        assert "emoji split mid-token" in cleaned

    def test_strip_lone_surrogates_is_noop_on_clean_text(self):
        clean = "Chef prefers no em dashes."
        assert strip_lone_surrogates(clean) == clean

    def test_surrogate_stripped_from_what_field(self):
        fact = ExtractedFact.model_validate({"what": "core fact \ud83d text"})
        assert "\ud83d" not in fact.what

    def test_surrogate_stripped_from_entity_names(self):
        fact = ExtractedFact.model_validate({"what": "x", "entities": ["Ch\ud83def"]})
        assert fact.entities == ["Chef"]

    def test_surrogate_stripped_during_batch_parse(self):
        raw = json.dumps({"what": "fact with \ud83d broken emoji", "entities": ["A\ud83db"]})
        # json.dumps of a lone surrogate round-trips as \udXXX escape; parse it back.
        result = parse_llm_output(f"[{raw}]")
        assert len(result.facts) == 1
        assert "\ud83d" not in result.facts[0].what
        assert result.facts[0].entities == ["Ab"]

    def test_surrogate_write_to_utf8_would_have_crashed_without_stripping(self):
        """Proves the bug this guards against is real: an unstripped lone
        surrogate cannot be encoded to UTF-8 at all."""
        poisoned = "text \ud83d more"
        with pytest.raises(UnicodeEncodeError):
            poisoned.encode("utf-8")
        cleaned = strip_lone_surrogates(poisoned)
        cleaned.encode("utf-8")  # must not raise


# ---------------------------------------------------------------------------
# Unreachable gateway falls back to regex, and reports it
# ---------------------------------------------------------------------------


class TestFallbackBehaviour:
    def test_default_off_path_is_byte_identical_to_todays_extractor(self):
        from skmemory.extractor import extract_memories

        text = "We decided to use pytest for everything. I love this plan."
        outcome = extract_facts(text, enabled=False)
        assert outcome.status == "disabled"
        assert outcome.source == "regex"
        assert outcome.facts == []
        today = extract_memories(text)
        assert [m.content for m in outcome.fallback_memories] == [m.content for m in today]
        assert [m.type for m in outcome.fallback_memories] == [m.type for m in today]

    def test_env_var_unset_means_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
        outcome = extract_facts("We decided to use pytest for everything.")
        assert outcome.status == "disabled"
        assert outcome.source == "regex"

    def test_env_var_truthy_opts_in(self, monkeypatch):
        monkeypatch.setenv(ENABLE_ENV_VAR, "1")
        calls = {}

        def fake_call(text, **kwargs):
            calls["called"] = True
            return None, "simulated unreachable"

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts("some text")
        assert calls.get("called") is True
        assert outcome.status == "unavailable"

    def test_unreachable_gateway_falls_back_to_regex_and_reports_it(self, monkeypatch):
        from skmemory.extractor import extract_memories

        text = "We decided to use pytest. The build is broken and I'm frustrated."

        def fake_call(_text, **_kwargs):
            return None, "Connection refused"

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts(text, enabled=True)

        assert outcome.source == "regex"
        assert outcome.status == "unavailable"
        assert "Connection refused" in outcome.detail
        assert "fell back" in outcome.detail.lower()
        today = extract_memories(text)
        assert [m.content for m in outcome.fallback_memories] == [m.content for m in today]

    def test_unparseable_model_output_falls_back_and_reports_it(self, monkeypatch):
        def fake_call(_text, **_kwargs):
            return "I cannot comply with this request, sorry!", ""

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts("We decided to use pytest.", enabled=True)

        assert outcome.source == "regex"
        assert outcome.status == "failed"
        assert "fell back" in outcome.detail.lower()

    def test_every_candidate_fact_failing_validation_falls_back(self, monkeypatch):
        def fake_call(_text, **_kwargs):
            return json.dumps([{"when": "today"}, {"where": "here"}]), ""

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts("We decided to use pytest.", enabled=True)

        assert outcome.source == "regex"
        assert outcome.status == "failed"
        assert outcome.dropped_facts == 2

    def test_legitimate_empty_batch_is_ok_not_failed(self, monkeypatch):
        """The model looking at text with no facts and saying so (`[]`) is a
        real, correct result, not a failure to fall back from."""

        def fake_call(_text, **_kwargs):
            return "[]", ""

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts("hello", enabled=True)

        assert outcome.source == "llm"
        assert outcome.status == "ok"
        assert outcome.facts == []

    def test_successful_llm_extraction_reports_ok_and_source_llm(self, monkeypatch):
        def fake_call(_text, **_kwargs):
            return json.dumps(
                [
                    {
                        "what": "Chef prefers no em dashes.",
                        "fact_type": "world",
                        "entities": ["Chef"],
                    },
                    {
                        "what": "The agent read the style guide.",
                        "fact_type": "experience",
                        "causal_relations": [{"index": 0}],
                    },
                ]
            ), ""

        monkeypatch.setattr("skmemory.fact_extraction.call_llm_extractor", fake_call)
        outcome = extract_facts("some conversation text", enabled=True)

        assert outcome.source == "llm"
        assert outcome.status == "ok"
        assert len(outcome.facts) == 2
        assert outcome.facts[0].fact_type == "world"
        assert outcome.facts[1].fact_type == "experience"
        assert [r.index for r in outcome.facts[1].causal_relations] == [0]


# ---------------------------------------------------------------------------
# Repairs are recorded, not silent (BatchParseResult.repairs)
# ---------------------------------------------------------------------------


class TestRepairsAreRecorded:
    """A coercion must leave a trace in `repairs`, never just happen quietly.

    This is what lets skmemory.eval.extraction_bench tell "repaired" apart
    from "accepted with no issue" instead of guessing from a fact count.
    """

    def test_entity_string_coercion_is_recorded(self):
        result = parse_llm_output(json.dumps([{"what": "x", "entities": "Chef"}]))
        assert len(result.facts) == 1
        assert any("entities" in note for note in result.repairs)

    def test_unrecognized_fact_type_is_recorded(self):
        result = parse_llm_output(json.dumps([{"what": "x", "fact_type": "guess"}]))
        assert len(result.facts) == 1
        assert result.facts[0].fact_type == "world"
        assert any("fact_type" in note for note in result.repairs)

    def test_unparseable_timestamp_is_recorded(self):
        result = parse_llm_output(
            json.dumps([{"what": "x", "occurred_start": "sometime last week"}])
        )
        assert len(result.facts) == 1
        assert result.facts[0].occurred_start is None
        assert any("occurred_start" in note for note in result.repairs)

    def test_well_formed_na_timestamp_is_not_recorded_as_a_repair(self):
        """ "N/A" is a documented absent-value spelling, not a malformed one:
        normalizing it to None should not be reported as a repair."""
        result = parse_llm_output(json.dumps([{"what": "x", "occurred_start": "N/A"}]))
        assert len(result.facts) == 1
        assert result.facts[0].occurred_start is None
        assert result.repairs == []

    def test_surrogate_strip_is_recorded(self):
        result = parse_llm_output(json.dumps([{"what": "fact with \ud83d broken emoji"}]))
        assert len(result.facts) == 1
        assert any("surrogate" in note for note in result.repairs)

    def test_clean_input_has_no_repairs(self):
        result = parse_llm_output(
            json.dumps([{"what": "x", "fact_type": "world", "entities": ["a", "b"]}])
        )
        assert result.repairs == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_parse_llm_output_is_deterministic(self):
        raw = json.dumps(
            [
                {"what": "a", "entities": "Chef"},
                {"what": "b", "causal_relations": [{"index": 0}]},
            ]
        )
        first = parse_llm_output(raw)
        second = parse_llm_output(raw)
        assert [f.model_dump() for f in first.facts] == [f.model_dump() for f in second.facts]
        assert first.dropped_facts == second.dropped_facts
        assert first.repairs == second.repairs

    def test_disabled_path_is_deterministic(self):
        text = "We decided to ship it. I love this plan. It works!"
        a = extract_facts(text, enabled=False)
        b = extract_facts(text, enabled=False)
        assert [m.content for m in a.fallback_memories] == [m.content for m in b.fallback_memories]
