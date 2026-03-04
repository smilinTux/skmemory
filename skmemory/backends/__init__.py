"""
Storage backends for SKMemory.

Level 0 (sqlite)   - SQLite index, zero infrastructure.
Level 0.5 (vault)  - SQLite + transparent AES-256-GCM at-rest encryption.
Level 1 (skvector) - Semantic vector search (powered by Qdrant).
Level 2 (skgraph)  - Graph relationship traversal (powered by FalkorDB).
"""

from .base import BaseBackend
from .skgraph_backend import SKGraphBackend
from .file_backend import FileBackend

__all__ = ["BaseBackend", "SKGraphBackend", "FileBackend", "VaultedSQLiteBackend"]

try:
    from .vaulted_backend import VaultedSQLiteBackend
except ImportError:
    VaultedSQLiteBackend = None  # type: ignore[assignment,misc]
