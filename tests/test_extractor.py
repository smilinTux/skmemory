"""Tests for skmemory.extractor — General Extractor"""
import pytest
from skmemory.extractor import (
    ExtractedMemory,
    extract_memories,
    _is_code_line,
    _extract_sentence,
    _deduplicate,
)


class TestCodeLineFilter:
    def test_blank_line_is_code(self):
        assert _is_code_line("") is True
        assert _is_code_line("   ") is True

    def test_import_statement_is_code(self):
        assert _is_code_line("import os") is True
        assert _is_code_line("from pathlib import Path") is True

    def test_def_class_is_code(self):
        assert _is_code_line("def my_function():") is True
        assert _is_code_line("class MyClass:") is True

    def test_return_statement_is_code(self):
        assert _is_code_line("    return None") is True

    def test_mostly_symbols_is_code(self):
        # < 30% alpha ratio triggers code filter
        assert _is_code_line("{{{{ => != ==}}}}") is True

    def test_normal_sentence_is_not_code(self):
        assert _is_code_line("We decided to use ChromaDB for vector search.") is False
        assert _is_code_line("The deployment went smoothly.") is False

    def test_shebang_is_code(self):
        assert _is_code_line("#!/usr/bin/env python3") is True


class TestSentenceExtraction:
    def test_strips_markdown(self):
        result = _extract_sentence("**We decided** to use ChromaDB.")
        assert "**" not in result
        assert "We decided to use ChromaDB." in result

    def test_strips_bullets(self):
        result = _extract_sentence("- We deployed to production")
        assert result.startswith("We deployed")

    def test_strips_numbers(self):
        result = _extract_sentence("1. We merged the PR")
        assert result.startswith("We merged")

    def test_strips_backticks(self):
        result = _extract_sentence("`skmemory` is now working")
        assert "`" not in result


class TestDecisionExtraction:
    def test_decided(self):
        text = "After discussion, we decided to use SQLite for primary storage."
        memories = extract_memories(text)
        types = [m.type for m in memories]
        assert "decision" in types

    def test_going_with(self):
        text = "Going with the WAL approach for crash recovery."
        memories = extract_memories(text)
        assert any(m.type == "decision" for m in memories)

    def test_approved(self):
        text = "The architecture was approved by the team."
        memories = extract_memories(text)
        assert any(m.type == "decision" for m in memories)


class TestPreferenceExtraction:
    def test_prefer(self):
        text = "I prefer flat JSON files over a database for portability."
        memories = extract_memories(text)
        assert any(m.type == "preference" for m in memories)

    def test_always(self):
        text = "We always use pytest for testing in this project."
        memories = extract_memories(text)
        assert any(m.type == "preference" for m in memories)

    def test_from_now_on(self):
        text = "From now on we commit with Co-Authored-By at the end."
        memories = extract_memories(text)
        assert any(m.type == "preference" for m in memories)


class TestMilestoneExtraction:
    def test_shipped(self):
        text = "We shipped v0.9.3 to PyPI this morning."
        memories = extract_memories(text)
        assert any(m.type == "milestone" for m in memories)

    def test_all_tests_pass(self):
        text = "All tests pass on the new branch."
        memories = extract_memories(text)
        assert any(m.type == "milestone" for m in memories)

    def test_finally(self):
        text = "Finally got the ChromaDB integration working end-to-end."
        memories = extract_memories(text)
        assert any(m.type == "milestone" for m in memories)


class TestProblemExtraction:
    def test_bug(self):
        text = "Found a bug in the query sanitizer for edge cases."
        memories = extract_memories(text)
        assert any(m.type == "problem" for m in memories)

    def test_root_cause(self):
        text = "Root cause was a missing null check in the WAL tail method."
        memories = extract_memories(text)
        assert any(m.type == "problem" for m in memories)


class TestEmotionalExtraction:
    def test_proud(self):
        text = "Chef is proud of what the circle built together."
        memories = extract_memories(text)
        assert any(m.type == "emotional" for m in memories)

    def test_proud(self):
        text = "Chef is proud of the sovereign AI circle and everything we built."
        memories = extract_memories(text)
        assert any(m.type == "emotional" for m in memories)


class TestDeduplication:
    def test_removes_duplicates(self):
        # Both start with the same 50-char prefix (key is content[:50].lower())
        shared_prefix = "We decided to use SQLite for the primary storage "  # 49 chars
        items = [
            ExtractedMemory("decision", shared_prefix + "backend", 0.6, 0),
            ExtractedMemory("decision", shared_prefix + "layer too", 0.6, 1),
            ExtractedMemory("milestone", "All tests passed successfully", 0.6, 2),
        ]
        result = _deduplicate(items)
        # First two share same 50-char key → deduplicated to one
        assert len(result) == 2

    def test_keeps_unique(self):
        items = [
            ExtractedMemory("decision", "We decided to go with approach A", 0.6, 0),
            ExtractedMemory("milestone", "Shipped version 2.0 to production", 0.6, 1),
        ]
        result = _deduplicate(items)
        assert len(result) == 2


class TestExtractMemories:
    def test_empty_text_returns_empty(self):
        assert extract_memories("") == []
        assert extract_memories("hi") == []

    def test_code_only_returns_empty(self):
        code = "\n".join([
            "import os",
            "from pathlib import Path",
            "def foo(): pass",
            "    return None",
        ])
        assert extract_memories(code) == []

    def test_mixed_conversation(self):
        text = """
        We decided to use ChromaDB for vector search.
        The deployment went smoothly and we shipped v0.9.3.
        I prefer flat files over a database for portability.
        Found a bug in the query sanitizer implementation.
        Chef is proud of the sovereign AI circle.
        import os
        def foo(): pass
        """
        memories = extract_memories(text)
        types = {m.type for m in memories}
        assert "decision" in types
        assert "milestone" in types or "decision" in types
        assert len(memories) >= 3

    def test_all_results_have_content(self):
        text = (
            "We decided to use ChromaDB. "
            "Finally shipped the feature. "
            "Found a bug in the WAL. "
            "I prefer flat JSON files. "
            "Chef is proud of this work."
        )
        memories = extract_memories(text)
        for m in memories:
            assert len(m.content) >= 20
            assert m.type in ("decision", "preference", "milestone", "problem", "emotional")
            assert 0 <= m.confidence <= 1
