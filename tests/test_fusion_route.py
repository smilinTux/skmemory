"""SKMEM-P1-08: deterministic query-shape router for the fusion pin."""

from __future__ import annotations

import pytest

from skmemory.fusion import ArmOutcome, fuse, route_query


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SKMEMORY_FUSION_ROUTE", raising=False)
    monkeypatch.delenv("SKMEMORY_FUSION_VECTOR_PIN", raising=False)


@pytest.mark.parametrize(
    "query",
    [
        "prb-6f069c5e",
        "chiap08",
        "error E1234 on boot",
        "skmem_pg",
        "skhub.skstack01.douno.it",
        "store.py",
    ],
)
def test_identifier_shapes(query):
    assert route_query(query).cls == "identifier"


@pytest.mark.parametrize(
    "query",
    [
        "what did we decide about the backup schedule?",
        "how do I restore the garden notes",
        "the thing we talked about regarding the old project plan",
        "why is the sync slow",
    ],
)
def test_natural_language(query):
    assert route_query(query).cls == "natural"


def test_dates_are_not_identifiers():
    assert route_query("what did we decide in 2026-10").cls == "natural"


def test_keyword_query_defaults_identifier():
    assert route_query("kubernetes ingress").cls == "identifier"


def test_rare_token_df_makes_a_question_identifier():
    q = "what is zorblax"
    assert route_query(q).cls == "natural"
    assert route_query(q, lambda t: 1 if t == "zorblax" else 500).cls == "identifier"
    assert route_query(q, lambda t: 0).cls == "natural"  # absent is not rare
    assert route_query(q, lambda t: 1 / 0).cls == "natural"  # df failure is not fatal


def test_df_not_consulted_for_stopwords():
    seen = []
    route_query("what is the thing", lambda t: seen.append(t) or 9999)
    assert "the" not in seen and "what" not in seen


def test_env_forces(monkeypatch):
    monkeypatch.setenv("SKMEMORY_FUSION_ROUTE", "vector")
    assert route_query("prb-6f069c5e").cls == "natural"
    monkeypatch.setenv("SKMEMORY_FUSION_ROUTE", "lexical")
    assert route_query("why is the sync slow").cls == "identifier"
    monkeypatch.setenv("SKMEMORY_FUSION_ROUTE", "bogus")
    assert route_query("prb-6f069c5e").reason == "id-shape"


def _arms():
    return [
        ArmOutcome("semantic", ("v1", "v2", "v3", "v4")),
        ArmOutcome("lexical_local", ("l1", "l2", "l3", "l4")),
    ]


def test_identifier_route_pins_lexical_head():
    r = fuse(_arms(), strategy="pinned_rrf", pin_arms=("lexical_local",), route=route_query("a_b"))
    assert r.ids[:3] == ["l1", "l2", "l3"] and r.route == "identifier"


def test_natural_route_pins_vector_head_then_fills_with_lexical():
    r = fuse(
        _arms(),
        strategy="pinned_rrf",
        pin_arms=("lexical_local",),
        route=route_query("why is the sync slow"),
    )
    assert r.ids[:2] == ["v1", "v2"] and r.route == "natural"
    assert "l1" in r.ids[:4]


def test_natural_route_without_vector_degrades_to_lexical_pin():
    arms = [ArmOutcome("semantic", (), status="failed"), _arms()[1]]
    r = fuse(
        arms,
        strategy="pinned_rrf",
        pin_arms=("lexical_local",),
        route=route_query("why is the sync slow"),
    )
    assert r.ids[:3] == ["l1", "l2", "l3"]


def test_vector_pin_env(monkeypatch):
    monkeypatch.setenv("SKMEMORY_FUSION_VECTOR_PIN", "3")
    r = fuse(
        _arms(),
        strategy="pinned_rrf",
        pin_arms=("lexical_local",),
        route=route_query("why is the sync slow"),
    )
    assert r.ids[:3] == ["v1", "v2", "v3"]


# SKMEM-P1-09: fusion must rank like the route's best single arm.
def _dilution_arms():
    # pg BM25 ("lexical") and the semantic arm disagree with lexical_local,
    # which alone has the target at rank 1. Decoys are shared across the others.
    return [
        ArmOutcome("semantic", ("d1", "d2", "d3", "d4", "d5", "target")),
        ArmOutcome("lexical", ("d1", "d2", "d3", "d4", "d5", "target")),
        ArmOutcome("lexical_local", ("target", "l2", "l3", "l4", "l5", "d1")),
    ]


def test_identifier_route_matches_lexical_local_order_with_pg_bm25_attached():
    # store passes ("lexical", "lexical_local") when skmem-pg is attached
    r = fuse(
        _dilution_arms(),
        strategy="pinned_rrf",
        pin_arms=("lexical", "lexical_local"),
        route=route_query("prb-6f069c5e"),
    )
    assert r.ids[:3] == ["target", "l2", "l3"]


def test_natural_route_keeps_vector_order_over_lexical_votes():
    arms = [
        ArmOutcome("semantic", ("target", "v2", "v3", "v4", "v5")),
        ArmOutcome("lexical", ("d1", "d2", "d3", "d4", "target")),
        ArmOutcome("lexical_local", ("d1", "d2", "d3", "d4", "v2")),
    ]
    r = fuse(
        arms,
        strategy="pinned_rrf",
        pin_arms=("lexical", "lexical_local"),
        route=route_query("why is the sync slow"),
    )
    assert r.ids[:3] == ["target", "v2", "v3"]
    assert "d1" in r.ids[:6]  # other arms still fill
