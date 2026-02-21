"""
MemoryStore - the main interface for storing and recalling memories.

This is the "camera" -- you point it at a moment, click, and it stores
a polaroid with full emotional context. Later, you recall by feeling
or by search, and the polaroid comes back with everything intact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .backends.base import BaseBackend
from .backends.file_backend import FileBackend
from .models import (
    EmotionalSnapshot,
    Memory,
    MemoryLayer,
    MemoryRole,
    SeedMemory,
)


class MemoryStore:
    """Main entry point for all memory operations.

    Delegates to one or more backends. The primary backend handles
    all CRUD. A vector backend (optional) handles semantic search.

    Args:
        primary: The primary storage backend (default: FileBackend).
        vector: Optional vector search backend (e.g., QdrantBackend).
    """

    def __init__(
        self,
        primary: Optional[BaseBackend] = None,
        vector: Optional[BaseBackend] = None,
    ) -> None:
        self.primary = primary or FileBackend()
        self.vector = vector

    def snapshot(
        self,
        title: str,
        content: str,
        *,
        layer: MemoryLayer = MemoryLayer.SHORT,
        role: MemoryRole = MemoryRole.GENERAL,
        tags: Optional[list[str]] = None,
        emotional: Optional[EmotionalSnapshot] = None,
        source: str = "manual",
        source_ref: str = "",
        related_ids: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Memory:
        """Take a polaroid -- capture a moment as a memory.

        This is the primary way to create memories. It stores to
        the primary backend and optionally indexes in the vector backend.

        Args:
            title: Short label for this memory.
            content: The full memory content.
            layer: Persistence tier.
            role: Role-based partition.
            tags: Searchable tags.
            emotional: Emotional context snapshot.
            source: Where this memory came from.
            source_ref: Reference to the source.
            related_ids: IDs of related memories.
            metadata: Additional key-value data.

        Returns:
            Memory: The stored memory with its assigned ID.
        """
        memory = Memory(
            title=title,
            content=content,
            layer=layer,
            role=role,
            tags=tags or [],
            emotional=emotional or EmotionalSnapshot(),
            source=source,
            source_ref=source_ref,
            related_ids=related_ids or [],
            metadata=metadata or {},
        )

        self.primary.save(memory)

        if self.vector:
            try:
                self.vector.save(memory)
            except Exception:
                pass  # Reason: vector indexing is best-effort, don't fail the write

        return memory

    def recall(self, memory_id: str) -> Optional[Memory]:
        """Retrieve a specific memory by ID.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[Memory]: The memory if found.
        """
        return self.primary.load(memory_id)

    def search(self, query: str, limit: int = 10) -> list[Memory]:
        """Search memories by text.

        Uses vector backend if available, falls back to text search.

        Args:
            query: Search query string.
            limit: Maximum results.

        Returns:
            list[Memory]: Matching memories ranked by relevance.
        """
        if self.vector:
            try:
                results = self.vector.search_text(query, limit=limit)
                if results:
                    return results
            except Exception:
                pass  # Reason: fall through to primary text search

        return self.primary.search_text(query, limit=limit)

    def forget(self, memory_id: str) -> bool:
        """Delete a memory from all backends.

        Args:
            memory_id: The memory to remove.

        Returns:
            bool: True if deleted from primary backend.
        """
        deleted = self.primary.delete(memory_id)
        if self.vector:
            try:
                self.vector.delete(memory_id)
            except Exception:
                pass
        return deleted

    def list_memories(
        self,
        layer: Optional[MemoryLayer] = None,
        tags: Optional[list[str]] = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories with optional filtering.

        Args:
            layer: Filter by layer.
            tags: Filter by tags (AND logic).
            limit: Max results.

        Returns:
            list[Memory]: Matching memories sorted newest first.
        """
        return self.primary.list_memories(layer=layer, tags=tags, limit=limit)

    def promote(
        self,
        memory_id: str,
        target: MemoryLayer,
        summary: str = "",
    ) -> Optional[Memory]:
        """Promote a memory to a higher persistence tier.

        Creates a new memory at the target layer linked to the original.
        The original stays in place as the detailed version.

        Args:
            memory_id: ID of the memory to promote.
            target: Target layer (should be higher than current).
            summary: Optional compressed summary.

        Returns:
            Optional[Memory]: The promoted memory, or None if source not found.
        """
        source = self.primary.load(memory_id)
        if source is None:
            return None

        promoted = source.promote(target, summary=summary)
        self.primary.save(promoted)

        if self.vector:
            try:
                self.vector.save(promoted)
            except Exception:
                pass

        return promoted

    def ingest_seed(self, seed: SeedMemory) -> Memory:
        """Import a Cloud 9 seed as a long-term memory.

        Converts a seed into a Memory and stores it. This is how
        seeds planted by one AI instance become retrievable memories
        for the next.

        Args:
            seed: The SeedMemory to import.

        Returns:
            Memory: The created long-term memory.
        """
        memory = seed.to_memory()
        self.primary.save(memory)

        if self.vector:
            try:
                self.vector.save(memory)
            except Exception:
                pass

        return memory

    def session_dump(self, session_id: str) -> list[Memory]:
        """Get all memories from a specific session.

        Args:
            session_id: The session identifier.

        Returns:
            list[Memory]: All memories tagged with this session.
        """
        return self.primary.list_memories(
            layer=MemoryLayer.SHORT,
            tags=[f"session:{session_id}"],
        )

    def consolidate_session(
        self,
        session_id: str,
        summary: str,
        emotional: Optional[EmotionalSnapshot] = None,
    ) -> Memory:
        """Compress a session's short-term memories into a single mid-term memory.

        This is the "end of day" operation: take all the short-term snapshots
        from a session and create one consolidated mid-term memory that captures
        the essence. Individual short-term memories are preserved.

        Args:
            session_id: The session to consolidate.
            summary: Human/AI-written summary of the session.
            emotional: Overall emotional snapshot for the session.

        Returns:
            Memory: The consolidated mid-term memory.
        """
        session_memories = self.session_dump(session_id)
        related = [m.id for m in session_memories]
        all_tags = set()
        for m in session_memories:
            all_tags.update(m.tags)
        all_tags.add(f"session:{session_id}")
        all_tags.add("consolidated")

        return self.snapshot(
            title=f"Session: {session_id}",
            content=summary,
            layer=MemoryLayer.MID,
            role=MemoryRole.AI,
            tags=list(all_tags),
            emotional=emotional or EmotionalSnapshot(),
            source="consolidation",
            source_ref=session_id,
            related_ids=related,
            metadata={
                "source_count": len(session_memories),
                "consolidated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def health(self) -> dict:
        """Check health of all backends.

        Returns:
            dict: Combined health status.
        """
        status = {"primary": self.primary.health_check()}
        if self.vector:
            try:
                status["vector"] = self.vector.health_check()
            except Exception as e:
                status["vector"] = {"ok": False, "error": str(e)}
        return status
