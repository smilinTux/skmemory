"""
Abstract base class for all SKMemory storage backends.

Every backend must implement these operations. The MemoryStore
delegates to whichever backend(s) are configured.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

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
    def load(self, memory_id: str) -> Optional[Memory]:
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
        layer: Optional[MemoryLayer] = None,
        tags: Optional[list[str]] = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories with optional filtering.

        Args:
            layer: Filter by memory layer.
            tags: Filter by tags (AND logic).
            limit: Maximum number of results.

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
