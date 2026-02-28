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
from .backends.sqlite_backend import CONTENT_PREVIEW_LENGTH, SQLiteBackend
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
    A graph backend (optional) indexes relationships for traversal.

    Args:
        primary: The primary storage backend (default: FileBackend).
        vector: Optional vector search backend (e.g., SKVectorBackend).
        graph: Optional graph backend (e.g., SKGraphBackend) for relationship indexing.
    """

    def __init__(
        self,
        primary: Optional[BaseBackend] = None,
        vector: Optional[BaseBackend] = None,
        graph: Optional["SKGraphBackend"] = None,
        use_sqlite: bool = True,
    ) -> None:
        if primary is not None:
            self.primary = primary
        elif use_sqlite:
            self.primary = SQLiteBackend()
        else:
            self.primary = FileBackend()
        self.vector = vector
        self.graph = graph

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

        memory.seal()

        self.primary.save(memory)

        if self.vector:
            try:
                self.vector.save(memory)
            except Exception:
                pass  # Reason: vector indexing is best-effort, don't fail the write

        if self.graph:
            try:
                self.graph.index_memory(memory)
            except Exception:
                pass  # Reason: graph indexing is best-effort, don't fail the write

        return memory

    def recall(self, memory_id: str) -> Optional[Memory]:
        """Retrieve a specific memory by ID with integrity verification.

        Automatically checks the integrity hash on recall. If the
        memory has been tampered with, a warning is logged and the
        memory's metadata is flagged with 'integrity_warning'.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[Memory]: The memory if found.
        """
        import logging
        logger = logging.getLogger("skmemory.store")

        memory = self.primary.load(memory_id)
        if memory is None:
            return None

        if memory.integrity_hash and not memory.verify_integrity():
            logger.warning(
                "TAMPER ALERT: Memory %s failed integrity check! "
                "Content may have been modified since storage.",
                memory_id,
            )
            memory.metadata["integrity_warning"] = (
                f"Integrity check failed at {datetime.now(timezone.utc).isoformat()}. "
                "This memory may have been tampered with."
            )

        return memory

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
        if self.graph:
            try:
                self.graph.remove_memory(memory_id)
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

        if self.graph:
            try:
                self.graph.index_memory(promoted)
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

        if self.graph:
            try:
                self.graph.index_memory(memory)
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

    def load_context(
        self,
        max_tokens: int = 3000,
        strongest_count: int = 5,
        recent_count: int = 5,
        include_seeds: bool = True,
    ) -> dict:
        """Load a token-efficient memory context for agent injection.

        Uses the SQLite index to pull summaries without reading full files.
        Designed to fit within a reasonable context window.

        Args:
            max_tokens: Approximate token budget (1 token ~= 4 chars).
            strongest_count: How many top-intensity memories to include.
            recent_count: How many recent memories to include.
            include_seeds: Whether to include seed memories.

        Returns:
            dict: Token-efficient context with summaries and metadata.
        """
        char_budget = max_tokens * 4
        context: dict = {"memories": [], "seeds": [], "stats": {}}
        used = 0

        if isinstance(self.primary, SQLiteBackend):
            strongest = self.primary.list_summaries(
                limit=strongest_count,
                order_by="emotional_intensity",
                min_intensity=3.0,
            )
            recent = self.primary.list_summaries(
                limit=recent_count,
                order_by="created_at",
            )

            seen_ids: set[str] = set()
            for mem in strongest + recent:
                if mem["id"] in seen_ids:
                    continue
                seen_ids.add(mem["id"])

                entry_text = mem["title"] + (mem["summary"] or mem["content_preview"])
                entry_size = len(entry_text)
                if used + entry_size > char_budget:
                    break
                used += entry_size
                context["memories"].append(mem)

            if include_seeds:
                seeds = self.primary.list_summaries(
                    tags=["seed"],
                    limit=10,
                    order_by="emotional_intensity",
                )
                for seed in seeds:
                    if seed["id"] in seen_ids:
                        continue
                    entry_text = seed["title"] + seed["summary"]
                    entry_size = len(entry_text)
                    if used + entry_size > char_budget:
                        break
                    used += entry_size
                    context["seeds"].append(seed)

            stats = self.primary.stats()
            context["stats"] = stats
        else:
            # Reason: fallback for non-SQLite backends — uses full objects
            all_mems = self.primary.list_memories(limit=strongest_count + recent_count)
            for mem in all_mems:
                entry = {
                    "id": mem.id,
                    "title": mem.title,
                    "summary": mem.summary or mem.content[:CONTENT_PREVIEW_LENGTH],
                    "emotional_intensity": mem.emotional.intensity,
                    "layer": mem.layer.value,
                }
                entry_size = len(entry["title"] + entry["summary"])
                if used + entry_size > char_budget:
                    break
                used += entry_size
                context["memories"].append(entry)

        context["token_estimate"] = used // 4
        return context

    def export_backup(self, output_path: str | None = None) -> str:
        """Export all memories to a dated JSON backup.

        Args:
            output_path: Destination file. Defaults to
                ``~/.skmemory/backups/skmemory-backup-YYYY-MM-DD.json``.

        Returns:
            str: Path to the written backup file.

        Raises:
            RuntimeError: If the primary backend doesn't support export.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.export_all(output_path)
        if isinstance(self.primary, FileBackend):
            # Reason: wrap FileBackend in a temporary SQLiteBackend for export
            temp = SQLiteBackend(base_path=str(self.primary.base_path))
            temp.reindex()
            return temp.export_all(output_path)
        raise RuntimeError(
            f"Export not supported for backend: {type(self.primary).__name__}"
        )

    def import_backup(self, backup_path: str) -> int:
        """Restore memories from a JSON backup file.

        Args:
            backup_path: Path to the backup JSON.

        Returns:
            int: Number of memories restored.

        Raises:
            RuntimeError: If the primary backend doesn't support import.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.import_backup(backup_path)
        raise RuntimeError(
            f"Import not supported for backend: {type(self.primary).__name__}"
        )

    def reindex(self) -> int:
        """Rebuild the SQLite index from JSON files.

        Only works if the primary backend is SQLiteBackend.

        Returns:
            int: Number of memories indexed, or -1 if not applicable.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.reindex()
        return -1

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
        if self.graph:
            try:
                status["graph"] = self.graph.health_check()
            except Exception as e:
                status["graph"] = {"ok": False, "error": str(e)}
        return status
