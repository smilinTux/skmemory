"""
Abstract base class for all SKMemory storage backends.

Every backend must implement these operations. The MemoryStore
delegates to whichever backend(s) are configured.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Memory, MemoryLayer


class BaseBackend(ABC):
    """Interface that all storage backends must implement."""

    @abstractmethod
    def save(self, memory: Memory) -> str:
        """Persist a memory.

        Args:
            memory: The Memory object to store.

        Returns:
            str: The memory ID.
        """

    @abstractmethod
    def load(self, memory_id: str) -> Memory | None:
        """Retrieve a single memory by ID.

        Args:
            memory_id: The unique memory identifier.

        Returns:
            Optional[Memory]: The memory if found, None otherwise.
        """

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """Remove a memory by ID.

        Args:
            memory_id: The unique memory identifier.

        Returns:
            bool: True if deleted, False if not found.
        """

    @abstractmethod
    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        created_after: str | None = None,
        created_before: str | None = None,
        source: str | None = None,
    ) -> list[Memory]:
        """List memories with optional filtering.

        Args:
            layer: Filter by memory layer.
            tags: Filter by tags (AND logic).
            limit: Maximum number of results.
            source: Filter by the memory's ``source`` field (e.g.
                ``"dreaming-engine"``), applied by the backend's own query
                before ``limit`` truncates the result -- same reasoning as
                ``created_after``/``created_before``: a caller scoping to
                one source in a store dominated by other sources must not
                rely on a client-side filter after an unscoped fetch.
            created_after: ISO 8601 timestamp; only memories with
                ``created_at >= created_after`` are returned. Applied by
                the backend's own query, BEFORE ``limit`` truncates the
                result -- callers scoping a query to a historical date
                range (not just "the N most recent") must pass this
                instead of filtering the returned list themselves (card:
                skmemory synthesis-silent-empty-on-historical-dates,
                2026-10-17: fetching the N most recent then filtering
                client-side silently returns nothing for any date outside
                that window in a large store).
            created_before: ISO 8601 timestamp; only memories with
                ``created_at < created_before`` are returned. Same
                ordering rule as ``created_after``.

        Returns:
            list[Memory]: Matching memories, newest first.
        """

    @abstractmethod
    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        """Simple text search across memory content.

        Args:
            query: Text to search for (case-insensitive substring).
            limit: Maximum results.

        Returns:
            list[Memory]: Memories matching the query.
        """

    def health_check(self) -> dict:
        """Check if this backend is operational.

        Returns:
            dict: Status dict with at least 'ok' boolean key.
        """
        return {"ok": True, "backend": self.__class__.__name__}
