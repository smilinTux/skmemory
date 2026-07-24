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
from pathlib import Path
from typing import Any

from .. import sealing as _sealing
from ..config import SKMEMORY_HOME
from ..models import Memory, MemoryLayer
from ..sealing import SealVerdict
from .base import BaseBackend

DEFAULT_BASE_PATH = str(SKMEMORY_HOME / "memory")


class FileBackend(BaseBackend):
    """Stores memories as JSON files on the local filesystem.

    Args:
        base_path: Root directory for memory storage.
        seal_config: Optional at-rest *sealing* config (Stage-2 of the PQC
            migration). When ``None`` (the default) the backend resolves to the
            classical sealer, which produces no signature -- so ``save`` writes
            **no sidecar** and the on-disk JSON is byte-for-byte identical to
            prior behaviour, and ``load`` round-trips exactly as before. Only
            when a real ``sk_pgp`` backend is *explicitly* selected **and** a
            key is present does ``save`` additionally write a ``<id>.json.sig``
            composite (ML-DSA-87 + Ed448) detached signature, which ``load``
            verifies on read. If sk_pgp is requested but unavailable, sealing
            honestly falls back to classical -- persistence never breaks.
        strict_verify: When ``True``, ``load`` treats a *failed* signature
            (``signature_ok is False``) as a hard tamper event and returns
            ``None``. An *unverifiable* signature (``signature_ok is None`` --
            e.g. a sidecar present but no cert/key to check it) is **not** a
            rejection, even in strict mode. Default ``False``.
    """

    def __init__(
        self,
        base_path: str = DEFAULT_BASE_PATH,
        *,
        seal_config: dict[str, Any] | None = None,
        strict_verify: bool = False,
    ) -> None:
        self.base_path = Path(base_path)
        self._seal_config = seal_config
        self._strict_verify = strict_verify
        # Verdict from the most recent verify-on-read (None if no sidecar).
        self.last_verdict: SealVerdict | None = None
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

    def _find_file(self, memory_id: str) -> Path | None:
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
        # Stage-2 (opt-in, gated): write a detached PQC signature sidecar when a
        # real sk_pgp backend is configured + ready. Classical default => no-op,
        # so the on-disk result above is byte-for-byte today's behaviour.
        _sealing.write_seal(memory, path, config=self._seal_config)
        return memory.id

    def load(self, memory_id: str) -> Memory | None:
        """Load a memory by ID from disk.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found, None otherwise.
        """
        path = self._find_file(memory_id)
        if path is None:
            self.last_verdict = None
            return None
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            memory = Memory(**data)
        except (json.JSONDecodeError, Exception):
            self.last_verdict = None
            return None
        # Stage-2 verify-on-read (gated): if a detached-signature sidecar exists,
        # verify it over the exact on-disk bytes and record an honest verdict. No
        # sidecar => verdict is None and behaviour is unchanged. In strict mode a
        # *failed* signature (tamper) rejects the load; an *unverifiable* one does
        # not.
        verdict = _sealing.verify_seal(raw, path, config=self._seal_config)
        self.last_verdict = verdict
        if self._strict_verify and verdict is not None and verdict.signature_ok is False:
            return None
        return memory

    def verify_at_rest(self, memory_id: str) -> SealVerdict | None:
        """Verify-on-read helper: return the seal verdict for a stored memory.

        Returns ``None`` when the memory is missing or has no signature sidecar
        (today's classical memories). When a ``<id>.json.sig`` sidecar exists,
        returns a :class:`~skmemory.sealing.SealVerdict`. Purely additive and
        side-effect free apart from updating :attr:`last_verdict`; never raises
        on a missing/unverifiable signature.
        """
        path = self._find_file(memory_id)
        if path is None:
            return None
        try:
            raw = path.read_bytes()
        except Exception:  # pragma: no cover - filesystem dependent
            return None
        verdict = _sealing.verify_seal(raw, path, config=self._seal_config)
        self.last_verdict = verdict
        return verdict

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
        # Remove any detached-signature sidecar alongside it (no-op if absent).
        sidecar = Path(_sealing.sidecar_path_for(path))
        if sidecar.exists():
            sidecar.unlink()
        return True

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
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
        words = [w.lower() for w in query.split()]
        if not words:
            return []
        results: list[Memory] = []
        scored: list[tuple[int, Memory]] = []

        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    mem = Memory(**data)
                    searchable = mem.to_embedding_text().lower()
                    hits = sum(1 for w in words if w in searchable)
                    if hits == 0:
                        continue
                    scored.append((hits, mem))
                except (json.JSONDecodeError, Exception):
                    continue

        # Sort by match count desc, then recency
        scored.sort(key=lambda t: (t[0], t[1].created_at), reverse=True)
        results = [m for _, m in scored]
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
