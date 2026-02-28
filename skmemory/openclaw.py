"""
OpenClaw integration module for SKMemory.

Provides a single-call interface for OpenClaw (or any AI agent framework)
to load, snapshot, and manage memories without wiring up backends manually.

Usage from an agent context file or plugin::

    from skmemory.openclaw import SKMemoryPlugin

    plugin = SKMemoryPlugin()
    ctx    = plugin.load_context(max_tokens=3000)
    plugin.snapshot("Built the kingdom today", tags=["milestone"])
    plugin.export()

Or from the OpenClaw JS plugin (calls CLI under the hood).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .models import EmotionalSnapshot, MemoryLayer, MemoryRole
from .store import MemoryStore
from .backends.sqlite_backend import SQLiteBackend


OPENCLAW_BASE = Path.home() / ".openclaw"
SKMEMORY_OPENCLAW_DIR = OPENCLAW_BASE / "plugins" / "skmemory"
SKMEMORY_STATE_FILE = SKMEMORY_OPENCLAW_DIR / "state.json"


class SKMemoryPlugin:
    """Drop-in memory module for OpenClaw and other agent frameworks.

    Initializes skmemory with sensible defaults, exposes the most-used
    operations as simple method calls, and stores state in the OpenClaw
    plugin directory so other skills can discover it.

    Args:
        base_path: Override the memory storage directory.
        skvector_url: Optional SKVector server for semantic search.
        skvector_key: Optional SKVector API key.
    """

    def __init__(
        self,
        base_path: Optional[str] = None,
        skvector_url: Optional[str] = None,
        skvector_key: Optional[str] = None,
    ) -> None:
        vector = None
        if skvector_url:
            try:
                from .backends.skvector_backend import SKVectorBackend
                vector = SKVectorBackend(url=skvector_url, api_key=skvector_key)
            except Exception:
                pass

        primary = SQLiteBackend(base_path=base_path) if base_path else None
        self.store = MemoryStore(primary=primary, vector=vector)

        SKMEMORY_OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
        self._write_state({"status": "loaded"})

    def load_context(
        self,
        max_tokens: int = 3000,
        strongest: int = 5,
        recent: int = 5,
        include_seeds: bool = True,
    ) -> dict:
        """Load a token-efficient memory context for injection into a prompt.

        Args:
            max_tokens: Approximate token budget.
            strongest: Number of strongest emotional memories.
            recent: Number of most recent memories.
            include_seeds: Include Cloud 9 seed memories.

        Returns:
            dict: Compact memory context payload.
        """
        return self.store.load_context(
            max_tokens=max_tokens,
            strongest_count=strongest,
            recent_count=recent,
            include_seeds=include_seeds,
        )

    def snapshot(
        self,
        title: str,
        content: str = "",
        *,
        layer: str = "short-term",
        tags: Optional[list[str]] = None,
        intensity: float = 0.0,
        valence: float = 0.0,
        emotions: Optional[list[str]] = None,
        source: str = "openclaw",
    ) -> str:
        """Capture a memory snapshot.

        Args:
            title: Short label.
            content: Full content (defaults to title if empty).
            layer: Persistence tier (short-term, mid-term, long-term).
            tags: Searchable tags.
            intensity: Emotional intensity 0-10.
            valence: Sentiment -1 to +1.
            emotions: Named emotion labels.
            source: Origin identifier.

        Returns:
            str: The new memory's ID.
        """
        emotional = EmotionalSnapshot(
            intensity=intensity,
            valence=valence,
            labels=emotions or [],
        )
        memory = self.store.snapshot(
            title=title,
            content=content or title,
            layer=MemoryLayer(layer),
            tags=tags or [],
            emotional=emotional,
            source=source,
        )
        return memory.id

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search memories and return lightweight results.

        Args:
            query: Search string.
            limit: Max results.

        Returns:
            list[dict]: Matching memory summaries.
        """
        if isinstance(self.store.primary, SQLiteBackend):
            conn = self.store.primary._get_conn()
            q = f"%{query}%"
            rows = conn.execute(
                "SELECT * FROM memories "
                "WHERE title LIKE ? OR summary LIKE ? OR tags LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (q, q, q, limit),
            ).fetchall()
            return [
                self.store.primary._row_to_memory_summary(r) for r in rows
            ]
        results = self.store.search(query, limit=limit)
        return [
            {"id": m.id, "title": m.title, "layer": m.layer.value}
            for m in results
        ]

    def recall(self, memory_id: str) -> Optional[dict]:
        """Retrieve a full memory by ID.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[dict]: Full memory data, or None.
        """
        mem = self.store.recall(memory_id)
        if mem is None:
            return None
        return mem.model_dump()

    def ritual(self) -> dict:
        """Perform the full rehydration ritual.

        Returns:
            dict: Ritual result with context prompt and summary.
        """
        from .ritual import perform_ritual

        result = perform_ritual(store=self.store)
        return {
            "identity": result.identity_loaded,
            "seeds_imported": result.seeds_imported,
            "journal_loaded": result.journal_loaded,
            "context_prompt": result.context_prompt,
        }

    def export(self, output_path: Optional[str] = None) -> str:
        """Export all memories to a dated JSON backup.

        Args:
            output_path: Destination (default: ~/.skmemory/backups/).

        Returns:
            str: Path to the backup file.
        """
        return self.store.export_backup(output_path)

    def import_backup(self, backup_path: str) -> int:
        """Restore memories from a backup.

        Args:
            backup_path: Path to the JSON backup.

        Returns:
            int: Number of memories restored.
        """
        return self.store.import_backup(backup_path)

    def health(self) -> dict:
        """Check system health.

        Returns:
            dict: Health status of all backends.
        """
        return self.store.health()

    def _write_state(self, state: dict) -> None:
        """Persist plugin state for OpenClaw discovery.

        Args:
            state: State data to write.
        """
        try:
            from . import __version__
            state["skmemory_version"] = __version__
            SKMEMORY_STATE_FILE.write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
