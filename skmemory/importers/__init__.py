"""
Chat history importers for SKMemory.

Converts exports from messaging platforms into searchable,
emotionally-indexed memories. Each importer reads a specific
export format and feeds it through MemoryStore.snapshot().
"""

from .telegram import import_telegram

__all__ = ["import_telegram"]
