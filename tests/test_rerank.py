"""Tests for fused-retrieval reranking (R2).

The two load-bearing properties, both of which exist because of a measurement
rather than a hunch:

  * it ABSTAINS on a query with no identifier-shaped term, so a paraphrase
    query is not reordered by whichever decoy happens to carry a rare word;
  * it boosts in RANK space, so a candidate already at rank 1 can never be
    displaced. That is what keeps the entity class safe: "chiap08" looks exactly
    like an identifier, but the document containing it is the bridge, not the
    answer.
"""

from __future__ import annotations

import pytest

from skmemory.rerank import (
    DEFAULT_RANK_DIVISOR,
    ChainReranker,
    NoopReranker,
    RareTermReranker,
    discriminative_terms,
    identifier_shaped,
)

# ---------------------------------------------------------------------------
# identifier_shaped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term", ["6f069c5e", "zioap01", "a543c87b", "7d3e9fcc", "20260806", "41", "v0.11.42"]
)
def test_identifier_shaped_accepts_identifiers(term):
    assert identifier_shaped(term)


@pytest.mark.parametrize(
    "term", ["postgres", "rebuild", "remediation", "vanished", "suspend", "routing"]
)
def test_identifier_shaped_rejects_ordinary_words(term):
    assert not identifier_shaped(term)


def test_identifier_shaped_accepts_long_opaque_tokens():
    assert identifier_shaped("deadbeefcafebabe")
    assert not identifier_shaped("deadbeef", min_opaque_len=12)


# ---------------------------------------------------------------------------
# discriminative_terms
# ---------------------------------------------------------------------------


def test_discriminative_terms_finds_the_rare_identifier():
    docs = ["card prb-6f069c5e closed"] + ["per node postgres rebuild plan"] * 19
    terms = discriminative_terms("per node postgres rebuild plan prb-6f069c5e", docs)
    assert "6f069c5e" in terms


def test_discriminative_terms_ignores_a_term_spread_across_the_pool():
    """A term in every candidate cannot tell candidates apart, however rare it
    is globally."""
    docs = ["host zioap01 note"] * 20
    assert discriminative_terms("zioap01", docs) == []


def test_discriminative_terms_abstains_on_a_paraphrase_query():
    docs = ["an unclean shutdown dropped a pagecache write", "spreadsheet rows reprinted"]
    assert discriminative_terms("rows vanished after suspend", docs) == []


def test_discriminative_terms_handles_an_empty_pool():
    assert discriminative_terms("anything 123", []) == []


# ---------------------------------------------------------------------------
# RareTermReranker
# ---------------------------------------------------------------------------


def test_rerank_promotes_a_buried_identifier_match():
    """The measured case: on the real backend the correct answer for an
    identifier query sat at fused rank 8 behind seven topical neighbours."""
    pool = [f"per node postgres rebuild note {i}" for i in range(7)]
    pool.insert(7, "card prb-6f069c5e closed verified")
    pool += [f"unrelated filler {i}" for i in range(17)]

    reranker = RareTermReranker()
    outcome = reranker.rerank("per node postgres rebuild remediation plan prb-6f069c5e", pool)

    assert outcome.applied
    assert "6f069c5e" in outcome.boost_terms
    new_first = [pool[i] for i in outcome.order][:3]
    assert any("prb-6f069c5e" in doc for doc in new_first), (
        f"the identifier match should reach the head, got {new_first}"
    )


def test_rerank_abstains_without_an_identifier_and_returns_identity_order():
    """An abstaining reranker must be safe to apply unconditionally."""
    pool = ["an unclean shutdown dropped a pagecache write", "spreadsheet rows reprinted after"]
    outcome = RareTermReranker().rerank("rows vanished after suspend", pool)
    assert not outcome.applied
    assert outcome.order == [0, 1]
    assert "no identifier-shaped" in outcome.detail


def test_rerank_cannot_displace_a_rank_one_candidate():
    """The rank-space bound, stated as a test.

    This is what protects the entity class: the queried entity is
    identifier-shaped and the document carrying it is the BRIDGE, not the
    answer, so an absolute reorder would promote the wrong row. Dividing a rank
    cannot beat rank 1.
    """
    pool = [
        "the finding: repetition collapse traced to stale kv reuse",  # the answer, rank 1
        "topology: chiap08 hosts the beelama endpoint",  # the bridge, carries the term
    ]
    outcome = RareTermReranker(rank_divisor=25.0).rerank("chiap08", pool)
    assert outcome.order[0] == 0, "a rank-1 candidate was displaced by a boost"


def test_rerank_is_stable_for_unboosted_candidates():
    pool = ["alpha one", "beta two", "gamma three"]
    outcome = RareTermReranker().rerank("nothing matches here", pool)
    assert outcome.order == [0, 1, 2]


def test_rerank_abstains_on_a_pool_too_small_to_reorder():
    assert not RareTermReranker().rerank("prb-6f069c5e", ["only one"]).applied
    assert not RareTermReranker().rerank("prb-6f069c5e", []).applied


def test_rerank_order_is_a_permutation_of_the_input():
    pool = [f"doc {i} with prb-6f069c5e" if i == 9 else f"doc {i} plain" for i in range(20)]
    outcome = RareTermReranker().rerank("prb-6f069c5e", pool)
    assert sorted(outcome.order) == list(range(len(pool)))


def test_more_identifier_hits_outrank_fewer():
    """A candidate matching two identifiers outranks one matching a single one.

    Needs a pool large enough that the document-frequency cap (15% of the pool)
    admits both terms, and large enough that neither boosted key clamps at the
    rank-1 floor. The property is RELATIVE order between the two boosted
    candidates; neither can take rank 1 from an unboosted candidate already
    sitting there, which is the bound asserted in
    ``test_rerank_cannot_displace_a_rank_one_candidate``.
    """
    pool = [f"plain filler {i}" for i in range(18)]
    pool.append("mentions zioap01 only")
    pool.append("mentions zioap01 and prb-6f069c5e both")

    outcome = RareTermReranker().rerank("zioap01 prb-6f069c5e", pool)
    order = [pool[i] for i in outcome.order]
    two_hits = order.index("mentions zioap01 and prb-6f069c5e both")
    one_hit = order.index("mentions zioap01 only")
    assert two_hits < one_hit, f"two-hit candidate placed {two_hits}, one-hit {one_hit}"


def test_default_divisor_is_the_swept_value():
    """Pinned so a later edit has to re-run the sweep rather than nudge it."""
    assert DEFAULT_RANK_DIVISOR == 5.0


# ---------------------------------------------------------------------------
# Noop + Chain
# ---------------------------------------------------------------------------


def test_noop_reranker_abstains_and_says_so():
    outcome = NoopReranker().rerank("prb-6f069c5e", ["a", "b"])
    assert not outcome.applied
    assert outcome.order == [0, 1]
    assert "disabled" in outcome.detail


def test_chain_reranker_composes_and_reports_abstention():
    pool = ["plain", "has prb-6f069c5e"]
    chained = ChainReranker(stages=[NoopReranker(), RareTermReranker()])
    outcome = chained.rerank("prb-6f069c5e", pool)
    assert outcome.applied
    assert sorted(outcome.order) == [0, 1]

    all_noop = ChainReranker(stages=[NoopReranker(), NoopReranker()])
    assert not all_noop.rerank("prb-6f069c5e", pool).applied
