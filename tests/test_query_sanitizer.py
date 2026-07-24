"""Tests for skmemory.query_sanitizer"""

from skmemory.query_sanitizer import sanitize_query


class TestPassthrough:
    def test_short_query_unchanged(self):
        q = "What did we decide about the auth system?"
        assert sanitize_query(q) == q.strip()

    def test_exactly_200_chars_unchanged(self):
        q = "a" * 200
        assert sanitize_query(q) == q

    def test_strips_whitespace_on_short_query(self):
        assert sanitize_query("  find the cloud9 memory  ") == "find the cloud9 memory"


class TestQuestionExtraction:
    def test_extracts_last_question_from_system_prompt(self):
        system_prompt = (
            "You are an AI assistant with access to memory tools. "
            "Your job is to help the user recall important information. "
            "You have access to a vector database. "
            "The user has many memories stored. "
            "Always be helpful and accurate. "
            "Today is 2026-04-12. "
            "The user is Chef, creator of the SKCapstone system. "
            "What did we decide about the database migration?"
        )
        result = sanitize_query(system_prompt)
        assert "database migration" in result
        assert len(result) <= 500

    def test_extracts_question_with_newlines(self):
        text = ("line one\n" * 30) + "What is the Cloud 9 protocol?"
        result = sanitize_query(text)
        assert "Cloud 9 protocol" in result

    def test_returns_full_question_sentence(self):
        prefix = "x" * 250 + ". "
        question = "How does the WAL recovery work?"
        result = sanitize_query(prefix + question)
        assert "WAL recovery" in result


class TestSentenceExtraction:
    def test_extracts_last_meaningful_sentence_no_question(self):
        # Long text with no '?', should get last sentence
        sentences = [
            "The agent initialized the memory store.",
            "It connected to ChromaDB successfully.",
            "All 18 agents were indexed into the vector database.",
        ]
        text = " ".join(sentences) + " " + ("filler text. " * 20)
        result = sanitize_query(text)
        assert len(result) <= 500
        assert len(result) >= 10


class TestTailTruncation:
    def test_very_long_query_truncated_to_500(self):
        long_query = "important memory search " * 100  # ~2400 chars, no ? or sentences
        result = sanitize_query(long_query)
        assert len(result) <= 500

    def test_result_never_exceeds_500(self):
        for length in [201, 500, 1000, 5000]:
            q = "a" * length
            result = sanitize_query(q)
            assert len(result) <= 500, f"Failed for length {length}"
