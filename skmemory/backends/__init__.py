"""
Storage backends for SKMemory.

There are exactly two storage tiers. Flat JSON files are the source of truth in both and
every index is derived and rebuildable.

Local tier (always on, offline capable, no services):
    FileBackend            - plain JSON files, no index.
    SQLiteBackend          - SQLite ``index.db`` beside the files, with an FTS5 lexical
                             index (the ``lexical_local`` search arm). The default primary.
    VaultedSQLiteBackend   - SQLite plus transparent AES-256-GCM at-rest encryption
                             (FTS is disabled for it).

Shared tier (skmem-pg, one Postgres container per node, optional):
    PGVectorBackend        - pgvector semantic search (``mxbai-embed-large``, 1024
                             dimensions) and pg_search BM25 (the ``semantic`` and
                             ``lexical`` arms).
    AGEGraphBackend        - Apache AGE graph, ``<agent>_knowledge`` (``age_backend``).
                             Supplementary: never the primary store, and a search arm
                             only when ``SKMEMORY_GRAPH_ARM=on``.

ChromaDB, Qdrant (skvector) and FalkorDB (skgraph) are retired and removed.
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
