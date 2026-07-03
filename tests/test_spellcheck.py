"""
Tests for skmemory.spellcheck — entity-gated offline typo correction
(ported from MemPalace's spellcheck.py, no third-party dependency).

No network, no services — pure function tests plus a couple of fakes for
protected_terms_from_store().
"""

from skmemory.spellcheck import (
    COMMON_TYPOS,
    correct_text,
    protected_terms_from_store,
)


class TestProtectedTermsNeverAltered:
    def test_protected_term_that_looks_like_a_typo_is_untouched(self):
        # "skmemory" is not an English word and would otherwise be a
        # candidate for "correction" — protection must win.
        text = "the skmemory server is up"
        result = correct_text(text, protected_terms={"skmemory"})
        assert "skmemory" in result.split()

    def test_protected_term_mxbai_untouched_even_with_typo_nearby(self):
        text = "teh mxbai embedding model"
        result = correct_text(text, protected_terms={"mxbai"})
        words = result.split()
        assert "mxbai" in words
        assert words[0] == "the"  # the real typo ("teh") still gets fixed

    def test_protection_is_case_insensitive(self):
        text = "Skmemory and skmemory both stay"
        result = correct_text(text, protected_terms={"skmemory"})
        # Capitalized token is untouched anyway (proper-noun guard), and the
        # lowercase one is untouched via protection — either way, unchanged.
        assert result == text


class TestClearTyposAreCorrected:
    def test_teh_becomes_the(self):
        assert correct_text("teh cat sat") == "the cat sat"

    def test_recieve_becomes_receive(self):
        assert correct_text("please recieve this") == "please receive this"

    def test_build_verify_example(self):
        result = correct_text("teh skmemory server", protected_terms={"skmemory"})
        assert result == "the skmemory server"

    def test_common_typos_table_round_trips(self):
        # Every entry in the curated table should correct cleanly in isolation.
        for typo, fix in COMMON_TYPOS.items():
            assert correct_text(typo) == fix

    def test_punctuation_is_preserved(self):
        assert correct_text("teh, really?") == "the, really?"

    def test_multiple_typos_in_one_string(self):
        result = correct_text("teh dog will recieve teh ball")
        assert result == "the dog will receive the ball"


class TestConservativeBehavior:
    def test_ambiguous_or_unknown_token_left_unchanged(self):
        # A nonsense/keyboard-mash token has no close, unambiguous
        # dictionary match — must be left alone rather than guessed at.
        text = "the zzxqvbnjk plan"
        result = correct_text(text)
        assert "zzxqvbnjk" in result.split()

    def test_capitalized_word_treated_as_proper_noun_not_touched(self):
        # Even a string that resembles a typo pattern is left alone when
        # capitalized (mirrors MemPalace's proper-noun guard).
        text = "Teh Company announced results"
        result = correct_text(text)
        assert result.split()[0] == "Teh"

    def test_technical_tokens_untouched(self):
        text = "bge-large-v1.5 top-10 NDCG@10 MAX_RESULTS ChromaDB"
        assert correct_text(text) == text

    def test_url_untouched(self):
        text = "see https://skworld.io/docs for teh details"
        result = correct_text(text)
        assert "https://skworld.io/docs" in result
        assert "the details" in result

    def test_already_valid_word_untouched(self):
        # "definite" is a real word and must never be altered even though
        # it's visually close to common typo "definately".
        assert correct_text("a definite answer") == "a definite answer"

    def test_short_tokens_not_fuzzy_matched(self):
        # Below the fuzzy-match floor and not in the curated table —
        # must be left alone, not guessed at.
        text = "a to it an"
        assert correct_text(text) == text

    def test_empty_string(self):
        assert correct_text("") == ""

    def test_no_protected_terms_defaults_to_empty(self):
        # Default protected_terms should behave like an empty set.
        assert correct_text("teh test") == correct_text("teh test", frozenset())


class TestProtectedTermsFromStore:
    def test_none_store_returns_empty_set(self):
        assert protected_terms_from_store(None) == set()

    def test_store_without_graph_attr_returns_empty_set(self):
        class NoGraphStore:
            pass

        assert protected_terms_from_store(NoGraphStore()) == set()

    def test_store_with_none_graph_returns_empty_set(self):
        class NoneGraphStore:
            graph = None

        assert protected_terms_from_store(NoneGraphStore()) == set()

    def test_store_whose_graph_raises_returns_empty_set_not_exception(self):
        class ExplodingGraph:
            def _ensure_initialized(self):
                raise RuntimeError("no connection")

        class ExplodingStore:
            graph = ExplodingGraph()

        assert protected_terms_from_store(ExplodingStore()) == set()

    def test_store_with_uninitialized_graph_returns_empty_set(self):
        class UninitGraph:
            def _ensure_initialized(self):
                return False

        class UninitStore:
            graph = UninitGraph()

        assert protected_terms_from_store(UninitStore()) == set()

    def test_store_with_working_graph_pulls_entity_names(self):
        class FakeResult:
            result_set = [["Riley"], ["skmemory"], [None]]

        class FakeRawGraph:
            def query(self, cypher):
                assert "Entity" in cypher
                return FakeResult()

        class WorkingGraph:
            _graph = FakeRawGraph()

            def _ensure_initialized(self):
                return True

        class WorkingStore:
            graph = WorkingGraph()

        names = protected_terms_from_store(WorkingStore())
        assert names == {"riley", "skmemory"}

    def test_names_from_store_actually_protect_correct_text(self):
        class FakeResult:
            result_set = [["skmemory"]]

        class FakeRawGraph:
            def query(self, cypher):
                return FakeResult()

        class WorkingGraph:
            _graph = FakeRawGraph()

            def _ensure_initialized(self):
                return True

        class WorkingStore:
            graph = WorkingGraph()

        protected = protected_terms_from_store(WorkingStore())
        result = correct_text("teh skmemory rocks", protected_terms=protected)
        assert result == "the skmemory rocks"
