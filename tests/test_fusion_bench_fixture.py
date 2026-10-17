"""Invariant tests for the fusion A/B fixture itself.

A benchmark fixture is code, and a mis-built one does not raise: it reports a
plausible-looking score. Both bugs found while building
``skmemory.eval.fusion_bench`` did exactly that.

  * The entity bridge derived its queried entity from the first token of the
    bridge text, which for one row yielded the entity ``"the"``. That query lost
    its only retrieval path and the class reported 5-of-6 instead of an error.
  * The semantic decoys were written using the query's own concept words, so
    they outranked every answer on BOTH arms. The class reported a flat 0.000
    that looked like a strategy result and was a fixture bug.

So the fixture's defining properties are asserted here. Without these the A/B
table is an opinion with decimal places.
"""

from __future__ import annotations

import skmemory.eval.fusion_bench as fb


def test_corpus_is_deterministic():
    """Two builds must be byte-identical, or an A/B compares two corpora."""
    a = fb.build_corpus()
    b = fb.build_corpus()
    assert [d["id"] for d in a] == [d["id"] for d in b]
    assert [d["content"] for d in a] == [d["content"] for d in b]


def test_corpus_ids_are_unique():
    ids = [d["id"] for d in fb.build_corpus()]
    assert len(ids) == len(set(ids))


def test_corpus_is_big_enough_to_discriminate():
    """The fixture this replaces had 10 documents, which is why every strategy
    scored 1.0 on it."""
    assert len(fb.build_corpus()) >= 100


def test_semantic_answers_have_no_lexical_overlap_with_their_queries():
    """The defining property of the semantic class.

    Each answer must share ZERO raw tokens with its query, so BM25 scores it at
    exactly 0 and cannot retrieve it at any k. If this ever passes by accident,
    the class stops testing dense retrieval and starts testing BM25 twice.
    """
    by_id = {d["id"]: d for d in fb.build_corpus()}
    for query, relevant in fb._SEMANTIC_QUERIES:
        q_tokens = set(fb._tokenize(query))
        for answer_id in relevant:
            doc = by_id[answer_id]
            a_tokens = set(fb._tokenize(f"{doc['title']} {doc['content']}"))
            overlap = q_tokens & a_tokens
            assert not overlap, (
                f"{answer_id} shares raw token(s) {sorted(overlap)} with its query "
                f"{query!r}; BM25 could then find it and the class no longer "
                f"isolates dense retrieval"
            )


def test_semantic_answers_do_share_concepts_with_their_queries():
    """The other half: zero lexical overlap is only useful if the CONCEPT
    overlap is real, otherwise the answer is simply unreachable and the class
    measures nothing."""
    by_id = {d["id"]: d for d in fb.build_corpus()}
    for query, relevant in fb._SEMANTIC_QUERIES:
        q_concepts = set(fb._concepts(query))
        for answer_id in relevant:
            doc = by_id[answer_id]
            a_concepts = set(fb._concepts(f"{doc['title']} {doc['content']}"))
            assert q_concepts & a_concepts, (
                f"{answer_id} shares no concept with query {query!r}, so no arm "
                f"can reach it and the class is vacuous"
            )


def test_entity_answers_are_only_reachable_through_the_bridge():
    """The entity class requires a real one-hop path, and requires that the
    answer NOT be directly findable, or the graph arm is not what is being
    measured."""
    by_id = {d["id"]: d for d in fb.build_corpus()}
    for _bridge_id, _text, queried, shared, answer_id, _ans in fb._ENTITY_TRIPLES:
        answer = by_id[answer_id]
        # The answer carries the shared entity (so the hop lands) ...
        assert shared in answer["entities"], f"{answer_id} is not on the bridge entity"
        # ... but never names the queried entity directly.
        assert queried not in answer["entities"]
        assert queried not in fb._tokenize(f"{answer['title']} {answer['content']}"), (
            f"{answer_id} names {queried!r} directly, so a text arm can reach it "
            f"and the class stops measuring graph expansion"
        )


def test_entity_queried_names_are_not_stopwords():
    """Pins the derived-entity bug: ``"the"`` as a queried entity matches
    nothing and silently costs a query its retrieval path."""
    for _b, _t, queried, _s, _a, _x in fb._ENTITY_TRIPLES:
        assert queried not in {"the", "a", "an", "of", "to"}
        assert len(queried) >= 4


def test_temporal_documents_never_name_their_month_in_the_text():
    """If the month is in the text, a text arm can answer a temporal query and
    the class no longer isolates the date predicate."""
    by_id = {d["id"]: d for d in fb.build_corpus()}
    for doc_id, _created in fb._TEMPORAL_MONTHS:
        text = f"{by_id[doc_id]['title']} {by_id[doc_id]['content']}".casefold()
        for month_name in (
            "january",
            "february",
            "march",
            "april",
            "june",
            "july",
            "august",
            "september",
        ):
            assert month_name not in text


def test_every_query_has_at_least_one_answer_in_the_corpus():
    ids = {d["id"] for d in fb.build_corpus()}
    for class_name, queries in fb.QUERY_CLASSES.items():
        for query, relevant in queries:
            assert relevant, f"{class_name}: {query!r} has no relevant id"
            for rid in relevant:
                assert rid in ids, f"{class_name}: {query!r} wants missing id {rid}"


def test_private_items_are_actually_reachable_when_unfiltered():
    """A leak trap that cannot reach a private row proves nothing, so
    ``leak_count == 0`` would be vacuous. The benchmark reports the unfiltered
    count for exactly this reason; assert it is non-zero."""
    res = fb.run_benchmark()
    assert any(data["leak_count_unfiltered"] > 0 for data in res["strategies"].values()), (
        "no strategy reaches a private row even unfiltered: the leak traps are dead"
    )


def test_no_strategy_leaks_a_private_row_through_the_filter():
    """The one result that must hold for every strategy, including new arms.

    Adding retrieval arms adds private-row reach, so this cannot be inherited
    from a previous run: it is re-proved per strategy.
    """
    res = fb.run_benchmark()
    for name, data in res["strategies"].items():
        assert data["leak_count"] == 0, f"{name} leaked {data['leak_count']} private row(s)"


def test_interleave_beats_the_current_production_path_on_every_class():
    """The R1 claim, pinned as a test.

    ``fallback_chain`` is what ``MemoryStore.search()`` did before R1. The new
    strategy must not regress ANY query class, which is the check that caught
    plain RRF destroying the semantic class while the overall number improved.
    """
    res = fb.run_benchmark()
    old = res["strategies"]["fallback_chain"]
    new = res["strategies"]["rrf_interleave"]
    for class_name in fb.QUERY_CLASSES:
        old_r = old["by_class"][class_name]["recall@5"]
        new_r = new["by_class"][class_name]["recall@5"]
        assert new_r >= old_r - 1e-9, (
            f"{class_name}: recall@5 regressed {old_r:.3f} -> {new_r:.3f}"
        )
    assert new["overall_recall@5"] > old["overall_recall@5"]
    assert new["overall_mrr"] > old["overall_mrr"]


def test_plain_rrf_regression_is_still_reproducible():
    """Keeps the WHY alive.

    If a future change makes plain RRF fine on the semantic class, this fails
    and tells whoever is reading that the motivation for rank-depth
    interleaving needs re-deriving rather than assumed.
    """
    res = fb.run_benchmark()
    plain = res["strategies"]["rrf"]["by_class"]["semantic"]["recall@5"]
    interleaved = res["strategies"]["rrf_interleave"]["by_class"]["semantic"]["recall@5"]
    assert plain < interleaved, (
        "plain RRF no longer loses the semantic class; re-derive why "
        "rank_interleave exists before trusting this harness"
    )


def test_no_two_documents_share_content():
    """Distinct content per document, because the real store de-dupes on a
    content hash.

    The temporal documents were originally byte-identical. Offline that reads
    as a clean "same subject, different month" design; against a real
    ``MemoryStore`` it is fatal and invisible, because ``snapshot()`` hashes
    content and drops the duplicates. The real-backend run ingested 128
    documents and stored 121, so the temporal class was being scored against a
    corpus that no longer contained seven of its own answers.

    An offline-only harness cannot catch this: it never writes through a store.
    """
    corpus = fb.build_corpus()
    seen: dict[str, str] = {}
    for doc in corpus:
        body = doc["content"]
        assert body not in seen, (
            f"{doc['id']} has identical content to {seen[body]}; a content-hash "
            f"de-dupe will silently drop one of them on ingest"
        )
        seen[body] = doc["id"]


def test_temporal_documents_still_hide_their_month_after_being_made_unique():
    """Making the temporal documents distinct must not leak the month into the
    text, or the fix for one bug would quietly disable the class."""
    by_id = {d["id"]: d for d in fb.build_corpus()}
    temporal_words = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "month",
        "quarter",
        "week",
        "year",
        "yesterday",
        "today",
    )
    for doc_id, _created in fb._TEMPORAL_MONTHS:
        text = f"{by_id[doc_id]['title']} {by_id[doc_id]['content']}".casefold()
        for word in temporal_words:
            assert word not in text, f"{doc_id} leaks the temporal word {word!r}"
