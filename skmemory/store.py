"""
MemoryStore - the main interface for storing and recalling memories.

This is the "camera" -- you point it at a moment, click, and it stores
a polaroid with full emotional context. Later, you recall by feeling
or by search, and the polaroid comes back with everything intact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .backends.base import BaseBackend

logger = logging.getLogger("skmemory.store")
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
            except Exception as exc:
                logger.warning("Vector indexing failed for memory %s: %s", memory.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(memory)
            except Exception as exc:
                logger.warning("Graph indexing failed for memory %s: %s", memory.id, exc)

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
            except Exception as exc:
                logger.warning("Vector search failed, falling back to text search: %s", exc)

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
            except Exception as exc:
                logger.warning("Vector delete failed for memory %s: %s", memory_id, exc)
        if self.graph:
            try:
                self.graph.remove_memory(memory_id)
            except Exception as exc:
                logger.warning("Graph delete failed for memory %s: %s", memory_id, exc)
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
            except Exception as exc:
                logger.warning("Vector indexing failed for promoted memory %s: %s", promoted.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(promoted)
            except Exception as exc:
                logger.warning("Graph indexing failed for promoted memory %s: %s", promoted.id, exc)

        return promoted

    def ingest_seed(self, seed: SeedMemory, *, validate: bool = True) -> Memory:
        """Import a Cloud 9 seed as a long-term memory.

        Converts a seed into a Memory and stores it. This is how
        seeds planted by one AI instance become retrievable memories
        for the next.

        When *validate* is True (default), basic integrity checks run
        before storage: seed_id must be non-empty and
        experience_summary must contain content.

        Args:
            seed: The SeedMemory to import.
            validate: Run pre-import validation (default True).

        Returns:
            Memory: The created long-term memory.

        Raises:
            ValueError: If validation is enabled and the seed is invalid.
        """
        if validate:
            errors: list[str] = []
            if not seed.seed_id or not seed.seed_id.strip():
                errors.append("seed_id is empty")
            if not seed.experience_summary or not seed.experience_summary.strip():
                errors.append("experience_summary is empty")
            if errors:
                raise ValueError(
                    f"Seed validation failed: {'; '.join(errors)}"
                )

        memory = seed.to_memory()
        self.primary.save(memory)

        if self.vector:
            try:
                self.vector.save(memory)
            except Exception as exc:
                logger.warning("Vector indexing failed for seed memory %s: %s", memory.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(memory)
            except Exception as exc:
                logger.warning("Graph indexing failed for seed memory %s: %s", memory.id, exc)

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
        max_tokens: int = 4000,
        strongest_count: int = 5,
        recent_count: int = 5,
        include_seeds: bool = True,
    ) -> dict:
        """Load tiered memory context for agent injection (lazy loading).

        Uses date-based tiers per memory-architecture.md:
        - Today's memories: full content (title + body)
        - Yesterday's memories: summary only (title + first 2 sentences)
        - Older than 2 days: reference count only

        Args:
            max_tokens: Approximate token budget (default: 4000).
                Uses word_count * 1.3 approximation for estimation.
            strongest_count: How many top-intensity memories to include.
            recent_count: How many recent memories to include.
            include_seeds: Whether to include seed memories.

        Returns:
            dict: Token-efficient tiered context with metadata.
        """
        context: dict = {
            "today": [],
            "yesterday": [],
            "older_summary": {},
            "seeds": [],
            "stats": {},
        }
        used_tokens = 0

        if isinstance(self.primary, SQLiteBackend):
            conn = self.primary._get_conn()

            # --- Tier 1: Today's memories (full content) ---
            today_rows = conn.execute(
                "SELECT * FROM memories WHERE DATE(created_at) = DATE('now') "
                "ORDER BY importance DESC, created_at DESC LIMIT 20"
            ).fetchall()

            for row in today_rows:
                summary_dict = self.primary._row_to_memory_summary(row)
                # Include full content for today
                content = summary_dict.get("summary") or summary_dict.get("content_preview") or ""
                entry = {
                    "id": summary_dict["id"],
                    "title": summary_dict["title"],
                    "content": content,
                    "tags": summary_dict["tags"],
                    "layer": summary_dict["layer"],
                    "emotional_intensity": summary_dict["emotional_intensity"],
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + content)
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["today"].append(entry)

            # --- Tier 2: Yesterday's memories (summary only: title + first 2 sentences) ---
            yesterday_rows = conn.execute(
                "SELECT * FROM memories WHERE DATE(created_at) = DATE('now', '-1 day') "
                "ORDER BY importance DESC, created_at DESC LIMIT 20"
            ).fetchall()

            for row in yesterday_rows:
                summary_dict = self.primary._row_to_memory_summary(row)
                raw_text = summary_dict.get("summary") or summary_dict.get("content_preview") or ""
                short_summary = _first_n_sentences(raw_text, 2)
                entry = {
                    "id": summary_dict["id"],
                    "title": summary_dict["title"],
                    "summary": short_summary,
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + short_summary)
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["yesterday"].append(entry)

            # --- Tier 3: Older memories (reference count only) ---
            mid_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'mid-term'"
            ).fetchone()[0]
            long_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'long-term'"
            ).fetchone()[0]
            short_old_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'short-term'"
            ).fetchone()[0]

            context["older_summary"] = {
                "mid_term_count": mid_count,
                "long_term_count": long_count,
                "short_term_count": short_old_count,
                "total": mid_count + long_count + short_old_count,
                "hint": (
                    f"{mid_count} mid-term memories, {long_count} long-term memories "
                    "available via memory_search"
                ),
            }
            used_tokens += _estimate_tokens(context["older_summary"]["hint"])

            # --- Seeds (titles only to save tokens) ---
            if include_seeds:
                seed_rows = self.primary.list_summaries(
                    tags=["seed"],
                    limit=10,
                    order_by="emotional_intensity",
                )
                seen_ids = {m["id"] for m in context["today"]}
                seen_ids.update(m["id"] for m in context["yesterday"])

                for seed in seed_rows:
                    if seed["id"] in seen_ids:
                        continue
                    entry = {
                        "id": seed["id"],
                        "title": seed["title"],
                    }
                    entry_tokens = _estimate_tokens(seed["title"])
                    if used_tokens + entry_tokens > max_tokens:
                        break
                    used_tokens += entry_tokens
                    context["seeds"].append(entry)

            stats = self.primary.stats()
            context["stats"] = stats
        else:
            # Fallback for non-SQLite backends: simple recent list
            all_mems = self.primary.list_memories(limit=strongest_count + recent_count)
            for mem in all_mems:
                content_text = mem.summary or mem.content[:CONTENT_PREVIEW_LENGTH]
                entry = {
                    "id": mem.id,
                    "title": mem.title,
                    "summary": _first_n_sentences(content_text, 2),
                    "emotional_intensity": mem.emotional.intensity,
                    "layer": mem.layer.value,
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + entry["summary"])
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["today"].append(entry)

        context["token_estimate"] = used_tokens
        context["token_budget"] = max_tokens
        return context

    def export_backup(self, output_path: str | None = None) -> str:
        """Export all memories to a dated JSON backup.

        Args:
            output_path: Destination file. Defaults to
                ``~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json``.

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

    def list_backups(self, backup_dir: str | None = None) -> list[dict]:
        """List all skmemory backup files, sorted newest first.

        Args:
            backup_dir: Directory to scan. Defaults to
                ``~/.skcapstone/backups/``.

        Returns:
            list[dict]: Backup entries with ``path``, ``name``,
                ``size_bytes``, and ``date`` keys.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.list_backups(backup_dir)
        return []

    def prune_backups(
        self, keep: int = 7, backup_dir: str | None = None
    ) -> list[str]:
        """Delete oldest backups, keeping only the N most recent.

        Args:
            keep: Number of backups to retain (default: 7).
            backup_dir: Directory to prune. Defaults to
                ``~/.skcapstone/backups/``.

        Returns:
            list[str]: Paths of deleted backup files.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.prune_backups(keep=keep, backup_dir=backup_dir)
        return []

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


def _estimate_tokens(text: str) -> int:
    """Estimate token count using word_count * 1.3 approximation.

    Args:
        text: The text to estimate.

    Returns:
        int: Approximate token count.
    """
    if not text:
        return 0
    word_count = len(text.split())
    return int(word_count * 1.3)


def _first_n_sentences(text: str, n: int = 2) -> str:
    """Extract the first N sentences from text.

    Args:
        text: Source text.
        n: Number of sentences to extract.

    Returns:
        str: The first N sentences, or the full text if fewer exist.
    """
    if not text:
        return ""
    # Split on sentence-ending punctuation followed by whitespace
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    result = " ".join(sentences[:n])
    # Cap at 200 chars as a safety net
    if len(result) > 200:
        result = result[:197] + "..."
    return result
