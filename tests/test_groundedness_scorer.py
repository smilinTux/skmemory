"""Regression tests for the groundedness / citation-faithfulness scorer.

All tests run OFFLINE with a deterministic MOCK judge - no LLM, no network.
The default judge (sk_default_judge, which would hit sk-default at
localhost:18780) is NEVER exercised here; that is the whole point of the
pluggable ``judge_fn`` seam.

Card acceptance covered:
  * an answer fully supported by its citations -> high score, ZERO flags.
  * an answer with a claim absent from the citations -> that claim is FLAGGED.
  * empty citations -> every claim flagged (score 0).
"""

from __future__ import annotations

from skmemory.eval.groundedness_scorer import (
    ClaimVerdict,
    GroundednessResult,
    lexical_overlap_judge,
    score_groundedness,
    split_claims,
)


# ── a deterministic mock judge ────────────────────────────────────────────
#
# "supported" iff every content word of the claim appears in some citation.
# This is intentionally strict + fully deterministic so the assertions below
# are exact, and it exercises the string / ClaimVerdict return shapes.


def _make_mock_judge(return_shape: str = "verdict"):
    """Build a mock judge that returns a chosen shape (verdict / bool / str).

    Proves the scorer normalizes all three accepted judge_fn return types.
    """

    def judge(claim: str, citations):
        hay = " ".join(citations).casefold()
        words = [w for w in claim.casefold().replace(".", " ").split() if len(w) >= 3]
        supported = bool(words) and all(w in hay for w in words)
        if return_shape == "bool":
            return supported
        if return_shape == "str":
            return "SUPPORTED" if supported else "UNSUPPORTED"
        return ClaimVerdict(claim=claim, supported=supported)

    return judge


SUPPORTED_ANSWER = (
    "The server runs on port 11434. skmem-pg uses pgvector."
)
CITATIONS = [
    "The server runs on port 11434 for embeddings.",
    "skmem-pg uses pgvector and BM25.",
]


# ── claim splitting ───────────────────────────────────────────────────────


def test_split_claims_sentences():
    claims = split_claims("The sky is blue. Grass is green.")
    assert claims == ["The sky is blue.", "Grass is green."]


def test_split_claims_bullets():
    text = "- first fact\n- second fact\n- third fact"
    assert split_claims(text) == ["first fact", "second fact", "third fact"]


def test_split_claims_protects_abbrev_and_decimals():
    # "e.g." and "3.5" must NOT split the claim.
    claims = split_claims("The threshold is 0.73 e.g. for dedup.")
    assert claims == ["The threshold is 0.73 e.g. for dedup."]


def test_split_claims_empty():
    assert split_claims("") == []
    assert split_claims("   \n  ") == []


# ── fully supported answer -> high score, zero flags ──────────────────────


def test_supported_answer_scores_high_zero_flags():
    result = score_groundedness(
        SUPPORTED_ANSWER, CITATIONS, judge_fn=_make_mock_judge("verdict")
    )
    assert isinstance(result, GroundednessResult)
    assert result.score == 1.0
    assert result.num_claims == 2
    assert result.num_supported == 2
    assert result.flagged_claims == []
    assert result.grounded is True


def test_supported_answer_all_return_shapes_agree():
    """bool / str / ClaimVerdict judge returns all yield the same result."""
    for shape in ("verdict", "bool", "str"):
        result = score_groundedness(
            SUPPORTED_ANSWER, CITATIONS, judge_fn=_make_mock_judge(shape)
        )
        assert result.score == 1.0, shape
        assert result.flagged_claims == [], shape


# ── unsupported claim -> flagged ──────────────────────────────────────────


def test_unsupported_claim_is_flagged():
    answer = (
        "The server runs on port 11434. "
        "It also trains a brandnew nightly transformer model."  # absent from citations
    )
    result = score_groundedness(answer, CITATIONS, judge_fn=_make_mock_judge("verdict"))
    assert result.num_claims == 2
    assert result.num_supported == 1
    assert len(result.flagged_claims) == 1
    assert "brandnew nightly transformer" in result.flagged_claims[0]
    assert 0.0 < result.score < 1.0
    assert result.grounded is False


def test_flagged_claim_via_string_verdict():
    answer = "skmem-pg uses pgvector. The moon is made of cheese."
    result = score_groundedness(answer, CITATIONS, judge_fn=_make_mock_judge("str"))
    assert len(result.flagged_claims) == 1
    assert "moon" in result.flagged_claims[0].casefold()


# ── empty citations -> everything flagged ─────────────────────────────────


def test_empty_citations_flags_everything():
    result = score_groundedness(SUPPORTED_ANSWER, [], judge_fn=_make_mock_judge("verdict"))
    assert result.score == 0.0
    assert result.num_claims == 2
    assert result.num_supported == 0
    assert result.flagged_claims == split_claims(SUPPORTED_ANSWER)
    assert result.grounded is False


def test_blank_only_citations_flags_everything():
    # Whitespace-only citation strings are dropped -> treated as empty.
    result = score_groundedness(
        SUPPORTED_ANSWER, ["   ", ""], judge_fn=_make_mock_judge("verdict")
    )
    assert result.score == 0.0
    assert result.flagged_claims == split_claims(SUPPORTED_ANSWER)


def test_empty_citations_does_not_call_judge():
    """With no citations the judge must never be invoked (no-network guarantee)."""
    called = {"n": 0}

    def counting_judge(claim, citations):
        called["n"] += 1
        return True

    score_groundedness("A fact here.", [], judge_fn=counting_judge)
    assert called["n"] == 0


# ── empty answer -> vacuously grounded ────────────────────────────────────


def test_empty_answer_is_vacuously_grounded():
    result = score_groundedness("", CITATIONS, judge_fn=_make_mock_judge("verdict"))
    assert result.score == 1.0
    assert result.num_claims == 0
    assert result.flagged_claims == []
    assert result.grounded is True


# ── verdict normalization edge cases ──────────────────────────────────────


def test_unknown_string_verdict_fails_closed():
    # A judge that returns gibberish must be treated as UNSUPPORTED (fail closed).
    result = score_groundedness(
        "One claim only.", CITATIONS, judge_fn=lambda c, s: "maybe-ish nonsense"
    )
    assert result.flagged_claims == ["One claim only."]
    assert result.score == 0.0


def test_dict_verdict_shape_supported():
    result = score_groundedness(
        "One claim only.",
        CITATIONS,
        judge_fn=lambda c, s: {"supported": True, "reason": "ok"},
    )
    assert result.flagged_claims == []
    assert result.score == 1.0


# ── the bundled offline lexical judge behaves sensibly ────────────────────


def test_lexical_overlap_judge_offline():
    supported = lexical_overlap_judge("server runs port 11434", CITATIONS)
    assert supported.supported is True
    missing = lexical_overlap_judge("quantum teleportation blockchain", CITATIONS)
    assert missing.supported is False


def test_score_with_lexical_judge_flags_hallucination():
    answer = "The server runs on port 11434. Quantum teleportation blockchain synergy."
    result = score_groundedness(answer, CITATIONS, judge_fn=lexical_overlap_judge)
    assert len(result.flagged_claims) == 1
    assert "quantum" in result.flagged_claims[0].casefold()
