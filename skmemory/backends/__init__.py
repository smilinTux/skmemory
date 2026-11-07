"""
Storage backends for SKMemory.

Level 0 (sqlite)   - SQLite index, zero infrastructure.
Level 0.5 (vault)  - SQLite + transparent AES-256-GCM at-rest encryption.
Level 1 (pgvector) - Postgres + pgvector: syncable, hybrid (vector+BM25), remote embedding.
Level 2 (age)      - Apache AGE graph relationship traversal (in skmem-pg).
"""

from .base import BaseBackend
from .file_backend import FileBackend

__all__ = [
    "BaseBackend",
    "FileBackend",
    "VaultedSQLiteBackend",
    "PGVectorBackend",
]

try:
    from .vaulted_backend import VaultedSQLiteBackend
except ImportError:
    VaultedSQLiteBackend = None  # type: ignore[assignment,misc]

try:
    from .pgvector_backend import PGVectorBackend
except ImportError:
    PGVectorBackend = None  # type: ignore[assignment,misc]
