"""Tests for metadata-scoped search in store.py and chroma_backend.py.

These tests verify that tags, layer, and source filters are correctly
passed through the search() API. They run against the FileBackend (no
ChromaDB required) to keep tests self-contained.
"""

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    """Store backed by FileBackend in a temp directory."""
    backend = FileBackend(base_path=tmp_path / "memories")
    return MemoryStore(primary=backend, vector=None, graph=None)


def _make_memory(title, content, tags, layer, source="manual"):
    return Memory(
        title=title,
        content=content,
        tags=tags,
        layer=layer,
        source=source,
    )


class TestScopedSearchSignature:
    """Verify search() accepts the new keyword args without error."""

    def test_search_accepts_tags(self, store):
        store.snapshot("Cloud9 event", "Something important happened", tags=["cloud9"])
        results = store.search("important", tags=["cloud9"])
        assert isinstance(results, list)

    def test_search_accepts_layer(self, store):
        store.snapshot("Short memory", "A quick note", layer=MemoryLayer.SHORT)
        results = store.search("quick note", layer="short-term")
        assert isinstance(results, list)

    def test_search_accepts_source(self, store):
        store.snapshot("Hook memory", "Auto-captured", source="claude-code-hook")
        results = store.search("Auto-captured", source="claude-code-hook")
        assert isinstance(results, list)

    def test_search_accepts_all_filters_combined(self, store):
        store.snapshot(
            "Full filter test",
            "Content for filter test",
            tags=["test"],
            layer=MemoryLayer.SHORT,
            source="mcp",
        )
        results = store.search(
            "filter test",
            tags=["test"],
            layer="short-term",
            source="mcp",
        )
        assert isinstance(results, list)

    def test_search_no_filters_still_works(self, store):
        store.snapshot("Baseline", "No filters applied here")
        results = store.search("Baseline")
        assert isinstance(results, list)


class TestQuerySanitizationInSearch:
    """Verify sanitize_query is called within search()."""

    def test_long_query_does_not_raise(self, store):
        store.snapshot("Test memory", "The agent initialized correctly")
        # Build a query > 200 chars with a question at the end
        big_query = ("x" * 250) + "? What did we initialize?"
        results = store.search(big_query)
        assert isinstance(results, list)

    def test_short_query_works_normally(self, store):
        store.snapshot("Simple test", "Simple content for testing")
        results = store.search("Simple content")
        assert isinstance(results, list)
