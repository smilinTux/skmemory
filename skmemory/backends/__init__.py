"""
Storage backends for SKMemory.

Level 0 (sqlite)   - SQLite index, zero infrastructure.
Level 0.5 (vault)  - SQLite + transparent AES-256-GCM at-rest encryption.
Level 1 (chroma)   - Local embedded vector search (powered by ChromaDB).
Level 1 (skvector) - Remote vector search for shared collections (powered by Qdrant).
Level 1 (pgvector) - Postgres + pgvector: syncable, hybrid (vector+BM25), remote embedding.
Level 2 (skgraph)  - Graph relationship traversal (powered by FalkorDB).
"""

from .base import BaseBackend
from .file_backend import FileBackend
from .skgraph_backend import SKGraphBackend

__all__ = [
    "BaseBackend",
    "SKGraphBackend",
    "FileBackend",
    "VaultedSQLiteBackend",
    "SKChromaBackend",
    "PGVectorBackend",
]

try:
    from .vaulted_backend import VaultedSQLiteBackend
except ImportError:
    VaultedSQLiteBackend = None  # type: ignore[assignment,misc]

try:
    from .chroma_backend import SKChromaBackend
except ImportError:
    SKChromaBackend = None  # type: ignore[assignment,misc]

try:
    from .pgvector_backend import PGVectorBackend
except ImportError:
    PGVectorBackend = None  # type: ignore[assignment,misc]
