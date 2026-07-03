"""Tests for advisory duplicate detection (MemPalace-ported pre-write dedup check).

Covers:
    - SKChromaBackend.find_similar() — similarity math + shape, via a
      monkeypatched ChromaDB collection (no real chromadb/embedding server
      required).
    - MemoryStore.check_duplicate() — threshold filtering + graceful
      no-backend / backend-without-find_similar behavior.

These tests never touch store.snapshot()'s existing exact-hash dedup —
check_duplicate() is a separate, read-only, advisory-only query path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skmemory.backends.chroma_backend import SKChromaBackend
from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory
from skmemory.store import MemoryStore


def _memory_doc(title: str, content: str) -> str:
    return Memory(title=title, content=content).model_dump_json()


@pytest.fixture
def chroma_backend() -> SKChromaBackend:
    """A SKChromaBackend with initialization + embedding faked out.

    Never touches a real ChromaDB client or embedding model — construction
    alone doesn't import chromadb, and we bypass _ensure_initialized/_embed
    directly so these tests run with no external services.
    """
    backend = SKChromaBackend()
    backend._ensure_initialized = lambda: True  # type: ignore[method-assign]
    backend._embed = lambda text: [0.1, 0.2, 0.3]  # type: ignore[method-assign]
    backend._collection = MagicMock()
    return backend


class TestFindSimilar:
    """Tests for SKChromaBackend.find_similar()."""

    def test_returns_similarity_sorted_matches(self, chroma_backend: SKChromaBackend) -> None:
        """Distances convert to similarity (1 - distance) and results sort descending."""
        chroma_backend._collection.query.return_value = {
            "ids": [["mem-a", "mem-b", "mem-c"]],
            "documents": [
                [
                    _memory_doc("A", "First thing that happened"),
                    _memory_doc("B", "Second thing that happened"),
                    _memory_doc("C", "Third thing that happened"),
                ]
            ],
            # 0.1 -> 0.9 similarity, 0.5 -> 0.5, 0.9 -> 0.1
            "distances": [[0.5, 0.1, 0.9]],
        }

        matches = chroma_backend.find_similar("First thing", k=3)

        assert [m["id"] for m in matches] == ["mem-b", "mem-a", "mem-c"]
        assert matches[0]["similarity"] == 0.9
        assert matches[1]["similarity"] == 0.5
        assert matches[2]["similarity"] == pytest.approx(0.1, abs=1e-9)

    def test_result_shape(self, chroma_backend: SKChromaBackend) -> None:
        """Each match has id, content_preview, and similarity keys."""
        chroma_backend._collection.query.return_value = {
            "ids": [["mem-a"]],
            "documents": [[_memory_doc("A", "Some content here")]],
            "distances": [[0.2]],
        }

        matches = chroma_backend.find_similar("Some content", k=5)

        assert len(matches) == 1
        match = matches[0]
        assert set(match.keys()) == {"id", "content_preview", "similarity"}
        assert match["id"] == "mem-a"
        assert match["content_preview"].startswith("Some content here")
        assert match["similarity"] == pytest.approx(0.8, abs=1e-9)

    def test_similarity_clamped_to_unit_range(self, chroma_backend: SKChromaBackend) -> None:
        """Distances outside [0, 2] (fp drift) never produce out-of-range similarity."""
        chroma_backend._collection.query.return_value = {
            "ids": [["mem-a", "mem-b"]],
            "documents": [[_memory_doc("A", "x"), _memory_doc("B", "y")]],
            "distances": [[-0.05, 1.2]],
        }

        matches = chroma_backend.find_similar("x", k=2)
        similarities = {m["id"]: m["similarity"] for m in matches}

        assert similarities["mem-a"] == 1.0
        assert similarities["mem-b"] == 0.0

    def test_uninitialized_backend_returns_empty(self) -> None:
        """If the backend can't initialize, find_similar never raises."""
        backend = SKChromaBackend()
        backend._ensure_initialized = lambda: False  # type: ignore[method-assign]

        assert backend.find_similar("anything") == []

    def test_embedding_failure_returns_empty(self, chroma_backend: SKChromaBackend) -> None:
        """If embedding the query yields nothing, find_similar returns []."""
        chroma_backend._embed = lambda text: []  # type: ignore[method-assign]

        assert chroma_backend.find_similar("anything") == []

    def test_query_exception_returns_empty(self, chroma_backend: SKChromaBackend) -> None:
        """A raising collection.query() is swallowed, not propagated."""
        chroma_backend._collection.query.side_effect = RuntimeError("boom")

        assert chroma_backend.find_similar("anything") == []


class TestCheckDuplicate:
    """Tests for MemoryStore.check_duplicate() — the advisory dedup query."""

    def _store(self, tmp_path: Path, vector) -> MemoryStore:
        return MemoryStore(primary=FileBackend(base_path=str(tmp_path / "memories")), vector=vector)

    def test_filters_by_threshold(self, tmp_path: Path) -> None:
        """Matches at/above threshold pass; matches below are excluded."""
        vector = MagicMock()
        vector.find_similar.return_value = [
            {"id": "high", "content_preview": "close paraphrase", "similarity": 0.9},
            {"id": "low", "content_preview": "unrelated note", "similarity": 0.5},
        ]
        store = self._store(tmp_path, vector)

        matches = store.check_duplicate("some candidate content", threshold=0.85)

        assert [m["id"] for m in matches] == ["high"]

    def test_boundary_equal_to_threshold_passes(self, tmp_path: Path) -> None:
        """A similarity exactly at the threshold counts as a match (>=)."""
        vector = MagicMock()
        vector.find_similar.return_value = [
            {"id": "exact", "content_preview": "...", "similarity": 0.85},
        ]
        store = self._store(tmp_path, vector)

        matches = store.check_duplicate("content", threshold=0.85)

        assert [m["id"] for m in matches] == ["exact"]

    def test_default_threshold_tuned_for_mxbai(self, tmp_path: Path) -> None:
        """The DEFAULT threshold (0.73) is empirically tuned for mxbai-embed-large.

        Uses the real boundary similarities measured by
        skmemory/eval/tune_dedup_threshold.py: near-duplicates clustered at
        0.76-0.94 and distinct content at 0.27-0.70 (clean gap 0.703-0.763).
        With NO explicit threshold, a near-dup at the dup-cluster floor (0.763)
        must be caught and distinct content at the non-dup ceiling (0.703) must
        be excluded. This regression-locks the 0.85->0.73 tuning: if the default
        drifts back up to 0.85 the near-dup is wrongly missed; if it drops to
        0.70 the distinct item wrongly matches — either way this test fails.
        """
        vector = MagicMock()
        vector.find_similar.return_value = [
            {"id": "near-dup", "content_preview": "paraphrase of a stored fact", "similarity": 0.763},
            {"id": "distinct", "content_preview": "unrelated different topic", "similarity": 0.703},
        ]
        store = self._store(tmp_path, vector)

        # No threshold kwarg -> exercises the tuned default (0.73).
        matches = store.check_duplicate("candidate content")

        assert [m["id"] for m in matches] == ["near-dup"]

    def test_no_vector_backend_returns_empty(self, tmp_path: Path) -> None:
        """No vector backend configured -> [] (never crashes)."""
        store = self._store(tmp_path, None)

        assert store.check_duplicate("content") == []

    def test_backend_without_find_similar_returns_empty(self, tmp_path: Path) -> None:
        """A vector backend lacking find_similar (e.g. an older/other backend) -> []."""
        vector = MagicMock(spec=["save", "search_text"])  # no find_similar attribute
        store = self._store(tmp_path, vector)

        assert store.check_duplicate("content") == []

    def test_find_similar_exception_returns_empty(self, tmp_path: Path) -> None:
        """A raising find_similar is swallowed, not propagated."""
        vector = MagicMock()
        vector.find_similar.side_effect = RuntimeError("boom")
        store = self._store(tmp_path, vector)

        assert store.check_duplicate("content") == []

    def test_passes_k_through_to_backend(self, tmp_path: Path) -> None:
        """The k param is forwarded to the backend's find_similar call."""
        vector = MagicMock()
        vector.find_similar.return_value = []
        store = self._store(tmp_path, vector)

        store.check_duplicate("content", k=7)

        vector.find_similar.assert_called_once_with("content", k=7)


class TestMCPToolRegistration:
    """Confirm memory_check_duplicate is wired into the MCP tool list."""

    def test_tool_is_registered(self) -> None:
        import asyncio

        from skmemory.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        names = [t.name for t in tools]

        assert "memory_check_duplicate" in names

    def test_tool_schema_requires_content(self) -> None:
        import asyncio

        from skmemory.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        tool = next(t for t in tools if t.name == "memory_check_duplicate")

        assert tool.inputSchema["required"] == ["content"]
        assert "content" in tool.inputSchema["properties"]
        assert "threshold" in tool.inputSchema["properties"]
        assert "k" in tool.inputSchema["properties"]
