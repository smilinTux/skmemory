"""
Storage backends for SKMemory.

Level 1 (file)   - JSON files on disk, zero infrastructure.
Level 2 (qdrant) - Vector search via Qdrant for semantic recall.
Level 3 (graph)  - FalkorDB graph relationships between memories.
"""

from .base import BaseBackend
from .file_backend import FileBackend

__all__ = ["BaseBackend", "FileBackend"]
