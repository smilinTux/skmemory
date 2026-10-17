"""Tests for token-level entity lookup on the graph arm.

Why this exists: the graph arm was attached and idle. ``search_by_entity``
substring-matches the ENTIRE query against entity names, so it only fires when
the query is close to a bare entity name. Measured against lumina's real graph
on ten realistic multi-word agent queries, the whole-query form fired on 0 of
10 and per-token lookup fired on 10 of 10.

No database needed: ``search_by_entity_tokens`` is built on
``self.search_by_entity``, so stubbing that one method exercises all of the
ordering, filtering and interleaving logic.
"""

from __future__ import annotations

import pytest

from skmemory.backends.age_backend import AGEGraphBackend


def _backend(responses: dict[str, list[str]]) -> AGEGraphBackend:
    """A backend whose entity lookup returns canned ids per query string.

    Built without ``__init__`` so there is no DSN, no profile and no database.
    """
    be = object.__new__(AGEGraphBackend)
    be.graph = "test_knowledge"
    calls: list[str] = []
    be.calls = calls  # type: ignore[attr-defined]

    def _search(query: str, limit: int = 20):
        calls.append(query)
        ids = responses.get(query, responses.get(query.casefold(), []))
        return [
            {"id": i, "title": f"t-{i}", "layer": "short-term", "entity": query}
            for i in ids[:limit]
        ]

    be.search_by_entity = _search  # type: ignore[assignment]
    return be


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# most specific first
# ---------------------------------------------------------------------------


def test_the_whole_query_is_tried_before_any_token():
    """For a query close to an entity name, the whole-query match is strictly
    better: tokenizing splits multi-word entity names and loses them."""
    be = _backend({"Ali Al-Zaidi": ["a1", "a2"], "Zaidi": ["z9"]})
    rows = be.search_by_entity_tokens("Ali Al-Zaidi", limit=10)
    assert _ids(rows)[:2] == ["a1", "a2"]
    assert be.calls[0] == "Ali Al-Zaidi", "the whole query must be tried first"


def test_a_satisfied_whole_query_does_not_need_tokens():
    be = _backend({"Beelama": [f"b{i}" for i in range(10)]})
    rows = be.search_by_entity_tokens("Beelama", limit=5)
    assert len(rows) == 5
    assert be.calls == ["Beelama"], "tokens were queried despite a full whole-query result"


def test_tokens_run_when_the_whole_query_finds_nothing():
    """The measured failure: 0 of 10 realistic multi-word queries fired."""
    be = _backend({"skgateway": ["s1", "s2"], "routing": ["r1"]})
    rows = be.search_by_entity_tokens("skgateway routing decision", limit=10)
    assert _ids(rows), "the arm abstained on a multi-word query again"
    assert "skgateway routing decision" in be.calls
    assert "skgateway" in be.calls


def test_a_single_token_equal_to_the_query_is_not_queried_twice():
    be = _backend({"beelama": ["b1"]})
    be.search_by_entity_tokens("beelama", limit=10)
    assert be.calls.count("beelama") == 1


# ---------------------------------------------------------------------------
# token selection
# ---------------------------------------------------------------------------


def test_stopwords_are_not_looked_up():
    be = _backend({"Casey": ["c1"]})
    be.search_by_entity_tokens("what did Casey say about the invoice", limit=10)
    for stop in ("what", "did", "say", "about", "the"):
        assert stop not in be.calls, f"looked up stopword {stop!r}"
    assert "Casey" in be.calls


def test_short_tokens_are_not_looked_up():
    """A three-letter fragment matches far too much to be informative."""
    be = _backend({"gpu": ["g1"], "chiap08": ["c1"]})
    be.search_by_entity_tokens("gpu chiap08 arc", limit=10)
    assert "gpu" not in be.calls
    assert "arc" not in be.calls
    assert "chiap08" in be.calls


def test_a_query_with_no_usable_token_abstains():
    be = _backend({})
    assert be.search_by_entity_tokens("the and for was", limit=10) == []


@pytest.mark.parametrize("query", ["", "   ", None])
def test_an_empty_query_abstains(query):
    assert _backend({}).search_by_entity_tokens(query, limit=10) == []


def test_a_disabled_backend_abstains():
    be = _backend({"x": ["1"]})
    be.graph = None
    assert be.search_by_entity_tokens("anything here", limit=10) == []


# ---------------------------------------------------------------------------
# ordering and budget
# ---------------------------------------------------------------------------


def test_the_most_discriminative_token_leads():
    """A token reaching three memories says more about intent than one
    reaching thirty, the same principle the rare-term reranker uses."""
    be = _backend(
        {
            "degeneracy": ["narrow1"],
            "memory": [f"broad{i}" for i in range(20)],
        }
    )
    rows = be.search_by_entity_tokens("memory degeneracy problem", limit=10)
    assert _ids(rows)[0] == "narrow1"


def test_one_broad_token_cannot_fill_the_whole_budget():
    """Interleaving by rank is what keeps a narrow token audible."""
    be = _backend(
        {
            "broadterm": [f"b{i}" for i in range(30)],
            "narrowterm": ["n1", "n2"],
        }
    )
    got = _ids(be.search_by_entity_tokens("broadterm narrowterm", limit=6))
    assert "n1" in got, f"the narrow token was crowded out: {got}"


def test_duplicate_ids_across_tokens_appear_once():
    be = _backend({"alpha": ["shared", "a1"], "bravo": ["shared", "b1"]})
    got = _ids(be.search_by_entity_tokens("alpha bravo", limit=10))
    assert got.count("shared") == 1


def test_the_limit_is_respected():
    be = _backend({f"token{i}": [f"r{i}"] for i in range(10)})
    query = " ".join(f"token{i}" for i in range(10))
    assert len(be.search_by_entity_tokens(query, limit=4)) == 4


def test_rows_keep_the_shape_search_by_entity_returns():
    """Either method must be able to back the fusion graph arm."""
    be = _backend({"beelama": ["b1"]})
    row = be.search_by_entity_tokens("beelama kv degeneracy", limit=5)[0]
    assert set(row) >= {"id", "title", "layer", "entity"}
