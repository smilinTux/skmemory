"""
Abstract base class for all SKMemory storage backends.

Every backend must implement these operations. The MemoryStore
delegates to whichever backend(s) are configured.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..models import Memory, MemoryLayer

logger = logging.getLogger(__name__)


def reseal_api_edit(backend: BaseBackend, memory: Memory) -> bool:
    """Keep the seal true when a writer edits a sealed field and saves.

    The seal covers id, title, content and the emotional signature. A writer that
    loads a memory, changes one of those and calls ``save`` without ``seal()`` would
    leave a hash no load can verify, and the nightly ``fortress verify`` reports it
    as tampered. This runs inside ``save`` and re-seals only when ALL of these hold:

    * the memory carries a hash that no longer matches its own sealed fields;
    * the same id is stored, and that stored copy verifies;
    * the hash on the memory is the stored copy's hash.

    That is exactly "a descendant of the verified on-disk record that had a sealed
    field edited in process". It never launders: a new memory, an unsealed one, an
    already failing predecessor (the file was edited outside the API) or a
    replacement carrying a different hash is saved exactly as given, so
    ``verify_integrity`` keeps reporting it. Edits made to the file itself never
    pass through ``save`` and stay detectable.

    Returns:
        bool: True when the memory was re-sealed.
    """
    if not memory.integrity_hash or memory.verify_integrity():
        return False
    try:
        stored = backend.load(memory.id)
    except Exception:  # noqa: BLE001 - an unreadable predecessor is "do not launder"
        return False
    if (
        stored is None
        or not stored.integrity_hash
        or stored.integrity_hash != memory.integrity_hash
        or not stored.verify_integrity()
    ):
        return False
    changed = [
        name
        for name, now, before in (
            ("title", memory.title.strip(), stored.title.strip()),
            ("content", memory.content, stored.content),
            ("emotional", memory.emotional.signature(), stored.emotional.signature()),
        )
        if now != before
    ]
    memory.seal()
    logger.warning(
        "re-sealed %s: a writer changed sealed field(s) %s without sealing",
        memory.id,
        ", ".join(changed) or "(none visible)",
    )
    return True


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
