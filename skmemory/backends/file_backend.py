"""
File-based storage backend (Level 1).

Zero infrastructure. Memories are stored as individual JSON files
in a directory tree organized by layer. Works everywhere, today,
with nothing to install.

Directory layout:
    base_path/
    ├── short-term/
    │   ├── {id}.json
    │   └── ...
    ├── mid-term/
    │   └── ...
    └── long-term/
        └── ...
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from ..config import SKMEMORY_HOME
from ..models import Memory, MemoryLayer
from .base import BaseBackend

DEFAULT_BASE_PATH = str(SKMEMORY_HOME / "memories")


class FileBackend(BaseBackend):
    """Stores memories as JSON files on the local filesystem.

    Args:
        base_path: Root directory for memory storage.
    """

    def __init__(self, base_path: str = DEFAULT_BASE_PATH) -> None:
        self.base_path = Path(base_path)
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Create layer directories if they don't exist."""
        for layer in MemoryLayer:
            (self.base_path / layer.value).mkdir(parents=True, exist_ok=True)

    def _file_path(self, memory: Memory) -> Path:
        """Get the file path for a memory.

        Args:
            memory: The memory to get the path for.

        Returns:
            Path: Full path to the JSON file.
        """
        return self.base_path / memory.layer.value / f"{memory.id}.json"

    def _find_file(self, memory_id: str) -> Optional[Path]:
        """Locate a memory file across all layers.

        Args:
            memory_id: The memory ID to find.

        Returns:
            Optional[Path]: Path to the file if found.
        """
        for layer in MemoryLayer:
            path = self.base_path / layer.value / f"{memory_id}.json"
            if path.exists():
                return path
        return None

    def save(self, memory: Memory) -> str:
        """Persist a memory as a JSON file.

        Args:
            memory: The Memory to store.

        Returns:
            str: The memory ID.
        """
        path = self._file_path(memory)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(memory.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
        return memory.id

    def load(self, memory_id: str) -> Optional[Memory]:
        """Load a memory by ID from disk.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found, None otherwise.
        """
        path = self._find_file(memory_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Memory(**data)
        except (json.JSONDecodeError, Exception):
            return None

    def delete(self, memory_id: str) -> bool:
        """Delete a memory file.

        Args:
            memory_id: The memory identifier.

        Returns:
            bool: True if deleted, False if not found.
        """
        path = self._find_file(memory_id)
        if path is None:
            return False
        path.unlink()
        return True

    def list_memories(
        self,
        layer: Optional[MemoryLayer] = None,
        tags: Optional[list[str]] = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories from disk with optional filtering.

        Args:
            layer: Filter by memory layer (None = all layers).
            tags: Filter by tags (AND logic).
            limit: Maximum results.

        Returns:
            list[Memory]: Matching memories sorted newest first.
        """
        layers = [layer] if layer else list(MemoryLayer)
        results: list[Memory] = []

        for lyr in layers:
            layer_dir = self.base_path / lyr.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    mem = Memory(**data)
                    if tags and not all(t in mem.tags for t in tags):
                        continue
                    results.append(mem)
                except (json.JSONDecodeError, Exception):
                    continue

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results[:limit]

    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        """Search memories by text substring (case-insensitive).

        Args:
            query: Search string.
            limit: Maximum results.

        Returns:
            list[Memory]: Matching memories.
        """
        query_lower = query.lower()
        results: list[Memory] = []

        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    raw = json_file.read_text(encoding="utf-8")
                    if query_lower not in raw.lower():
                        continue
                    data = json.loads(raw)
                    results.append(Memory(**data))
                except (json.JSONDecodeError, Exception):
                    continue

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results[:limit]

    def health_check(self) -> dict:
        """Check filesystem backend health.

        Returns:
            dict: Status with path and layer counts.
        """
        counts = {}
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if layer_dir.exists():
                counts[layer.value] = len(list(layer_dir.glob("*.json")))
            else:
                counts[layer.value] = 0
        return {
            "ok": True,
            "backend": "FileBackend",
            "base_path": str(self.base_path),
            "memory_counts": counts,
            "total": sum(counts.values()),
        }
