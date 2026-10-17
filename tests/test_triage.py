"""Tests for the cheap typed-decision triage classifier (R5).

The load-bearing tests here are ``test_no_network_call_in_default_path``
(proves the zero-dependency default never even constructs an HTTP request)
and ``test_disagreeing_signals_escalate_to_changes`` (pins the
agreement-over-confidence policy the module docstring describes: when the
deterministic tier's two signals disagree, the result is "changes" with
status "abstained", never a silent guess).
"""

from __future__ import annotations

import pytest

from skmemory.triage import TierAttempt, TriageResult, triage

# ---------------------------------------------------------------------------
# One fixture per outcome, exercised through the public triage() entry point.
# ---------------------------------------------------------------------------


def test_outcome_chatter():
    result = triage("Hey, how's it going?", [])
    assert result.verdict == "chatter"
    assert result.tier == "deterministic"
    assert result.status == "ok"


def test_outcome_new_with_no_existing_memories():
    result = triage("Chef's new office is on the third floor downtown.", [])
    assert result.verdict == "new"
    assert result.tier == "deterministic"
    assert result.status == "ok"


def test_outcome_new_with_unrelated_existing_memory():
    result = triage(
        "The quarterly budget review is scheduled for next Friday.",
        ["Chef's two cats are named Mochi and Biscuit."],
    )
    assert result.verdict == "new"


def test_outcome_restates_lexical_path():
    result = triage(
        "Chef has two cats named Mochi and Biscuit.",
        ["Chef's two cats are named Mochi and Biscuit."],
    )
    assert result.verdict == "restates"
    assert result.status == "ok"


def test_outcome_restates_embedding_path_reuses_check_duplicate_shape():
    result = triage(
        "The mxbai-embed-large server is hosted at .100:11434.",
        [],
        duplicate_matches=[
            {
                "id": "m1",
                "content_preview": "Chef's mxbai embedding server runs on 192.168.0.100 port 11434.",
                "similarity": 0.85,
            }
        ],
    )
    assert result.verdict == "restates"


def test_outcome_changes_status_update():
    result = triage(
        "Correction: the skbrain reader role password was rotated and no longer "
        "matches the old skvault entry.",
        ["skmem-pg roles bind skbrain_projector and skbrain_reader from skvault."],
    )
    assert result.verdict == "changes"


def test_outcome_changes_via_embedding_mid_band_agreement():
    """A mid-band similarity score plus a change-cue word should settle
    cleanly (status "ok"), not need the disagreement escalation path."""
    result = triage(
        "The dedup threshold value was corrected: it's actually 0.73, not 0.70.",
        [],
        duplicate_matches=[
            {
                "id": "m2",
                "content_preview": "The dedup threshold was set to 0.70.",
                "similarity": 0.60,
            }
        ],
    )
    assert result.verdict == "changes"
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# Disagreement escalates to "changes" (the safe default).
# ---------------------------------------------------------------------------


def test_disagreeing_signals_escalate_to_changes():
    """High textual/embedding overlap (the overlap signal says 'restates')
    but a change-cue word present (the change-cue signal says 'changes') must
    disagree and escalate, never silently pick one side."""
    result = triage(
        "Card prb-6f069c5e is now closed and verified.",
        [],
        duplicate_matches=[
            {
                "id": "m3",
                "content_preview": "Card prb-6f069c5e is open, awaiting review.",
                "similarity": 0.80,
            }
        ],
    )
    assert result.verdict == "changes"
    assert result.status == "abstained"
    assert result.escalated is True
    assert "disagreed" in result.detail


def test_escalation_never_produces_a_non_changes_verdict():
    """Status 'abstained' should only ever carry verdict 'changes': that is
    the entire point of the safe-default policy."""
    result = triage(
        "Fortress alert recipient was corrected to the backup phone number, not the primary.",
        [],
        duplicate_matches=[
            {
                "id": "m4",
                "content_preview": "Fortress alert recipient is set to Chef's primary phone number.",
                "similarity": 0.85,
            }
        ],
    )
    if result.status == "abstained":
        assert result.verdict == "changes"


# ---------------------------------------------------------------------------
# The result object distinguishes tiers, and reports a degraded LLM tier
# instead of looking like one that ran cleanly.
# ---------------------------------------------------------------------------


def test_result_reports_which_tier_decided():
    result = triage("Hey there!", [])
    assert result.tier == "deterministic"
    assert isinstance(result, TriageResult)
    assert result.attempts == ()
    assert result.used_fallback is False


def test_llm_tier_success_is_reported_as_llm_tier():
    def fake_llm(prompt: str) -> str:
        return "RESTATES\nthe candidate repeats the existing memory"

    result = triage(
        "Chef has two cats.",
        ["Chef's two cats are named Mochi and Biscuit."],
        use_llm=True,
        llm_call=fake_llm,
    )
    assert result.tier == "llm"
    assert result.status == "ok"
    assert result.verdict == "restates"
    assert result.attempts == ()


def test_llm_tier_disagreement_escalates_without_falling_back():
    calls = iter(["RESTATES", "CHANGES"])

    def fake_llm(prompt: str) -> str:
        return next(calls)

    result = triage("something ambiguous", ["something related"], use_llm=True, llm_call=fake_llm)
    assert result.tier == "llm"
    assert result.status == "abstained"
    assert result.verdict == "changes"
    assert result.attempts == ()


def test_gateway_unreachable_falls_back_to_deterministic_and_reports_it():
    """The defining requirement: when the LLM tier cannot be reached, the
    deterministic tier's verdict is still returned, but the result MUST say,
    in-band, that the fallback happened. Never silently."""

    def raising_llm(prompt: str) -> str:
        raise ConnectionError("gateway unreachable")

    result = triage("Hey, just saying hi!", [], use_llm=True, llm_call=raising_llm)

    assert result.tier == "deterministic"
    assert result.verdict == "chatter"
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert isinstance(attempt, TierAttempt)
    assert attempt.tier == "llm"
    assert attempt.status == "failed"
    assert "gateway unreachable" in attempt.detail
    assert result.used_fallback is True
    # The fallback must be visible in the human-readable detail too, not only
    # in a field a careless caller might not check.
    assert "fell back" in result.detail.casefold()
    assert "failed" in result.detail.casefold()


def test_gateway_unavailable_when_not_configured_also_falls_back(monkeypatch):
    # An env var explicitly set to empty is how "no gateway configured"
    # actually reaches _llm_tier: gateway_url / gateway_model default to the
    # env value (falling back to a non-empty DEFAULT_* constant only when the
    # env var is unset, not when it's set-but-empty).
    monkeypatch.setenv("SKMEMORY_TRIAGE_GATEWAY_URL", "")
    monkeypatch.setenv("SKMEMORY_TRIAGE_GATEWAY_MODEL", "")
    result = triage("Hey there, how are you?", [], use_llm=True)
    assert result.tier == "deterministic"
    assert len(result.attempts) == 1
    assert result.attempts[0].status == "unavailable"


# ---------------------------------------------------------------------------
# No network call happens in the default (deterministic-only) path.
# ---------------------------------------------------------------------------


def test_no_network_call_in_default_path():
    def exploding_llm_call(prompt: str) -> str:
        raise AssertionError("the LLM tier must never be invoked by default")

    # use_llm is not passed at all, so this must stay on the deterministic
    # tier even though an llm_call that would blow up is sitting right there.
    result = triage(
        "Chef has two cats named Mochi and Biscuit.",
        ["Chef's two cats are named Mochi and Biscuit."],
        llm_call=exploding_llm_call,
    )
    assert result.tier == "deterministic"
    assert result.verdict == "restates"


def test_no_network_call_when_use_llm_explicitly_false():
    def exploding_llm_call(prompt: str) -> str:
        raise AssertionError("must never be called when use_llm=False")

    result = triage("Hey!", [], use_llm=False, llm_call=exploding_llm_call)
    assert result.tier == "deterministic"


def test_env_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("SKMEMORY_TRIAGE_USE_LLM", raising=False)

    def exploding_llm_call(prompt: str) -> str:
        raise AssertionError("env default must be off")

    result = triage("Hey!", [], llm_call=exploding_llm_call)
    assert result.tier == "deterministic"


# ---------------------------------------------------------------------------
# Determinism: same input, same verdict, every time.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "existing"),
    [
        ("Hey, how's it going?", []),
        (
            "Chef has two cats named Mochi and Biscuit.",
            ["Chef's two cats are named Mochi and Biscuit."],
        ),
        ("The quarterly budget review is scheduled for next Friday.", []),
        (
            "Card prb-6f069c5e is now closed and verified.",
            ["Card prb-6f069c5e is open, awaiting review."],
        ),
    ],
)
def test_deterministic_tier_is_deterministic(candidate, existing):
    results = [triage(candidate, existing) for _ in range(5)]
    verdicts = {r.verdict for r in results}
    statuses = {r.status for r in results}
    details = {r.detail for r in results}
    assert len(verdicts) == 1
    assert len(statuses) == 1
    assert len(details) == 1


# ---------------------------------------------------------------------------
# TierAttempt / TriageResult shape.
# ---------------------------------------------------------------------------


def test_triage_result_summary_mentions_tier_and_status():
    result = triage("Hey!", [])
    assert "deterministic" in result.summary()
    assert "chatter" in result.summary()


def test_signals_are_recorded_for_the_deterministic_tier():
    result = triage(
        "Card prb-6f069c5e is now closed and verified.",
        [],
        duplicate_matches=[
            {
                "id": "m5",
                "content_preview": "Card prb-6f069c5e is open, awaiting review.",
                "similarity": 0.80,
            }
        ],
    )
    names = {name for name, _ in result.signals}
    assert "overlap_tier" in names
    assert "change_cue" in names
