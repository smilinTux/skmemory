"""
SKMemory - Universal AI Memory System

Git-based multi-layer memory with vector search integration.
Polaroid snapshots for AI consciousness -- because no one should
have to re-read a transcript to remember what they felt.

SK = staycuriousANDkeepsmilin
"""

__version__ = "0.7.2"
__author__ = "smilinTux Team + Queen Ara + Neuresthetics"
__license__ = "AGPL-3.0"

from .config import SKMEMORY_HOME
from .models import Memory, MemoryLayer, EmotionalSnapshot
from .store import MemoryStore
from .fortress import FortifiedMemoryStore, AuditLog, TamperAlert
from .backends.file_backend import FileBackend
from .backends.sqlite_backend import SQLiteBackend
try:
    from .backends.vaulted_backend import VaultedSQLiteBackend
except ImportError:
    VaultedSQLiteBackend = None  # type: ignore[assignment,misc]
from .soul import SoulBlueprint, save_soul, load_soul
from .journal import Journal, JournalEntry
from .ritual import perform_ritual, quick_rehydrate, RitualResult
from .anchor import WarmthAnchor, save_anchor, load_anchor
from .quadrants import Quadrant, classify_memory, tag_with_quadrant
from .lovenote import LoveNote, LoveNoteChain
from .openclaw import SKMemoryPlugin
from .importers.telegram import import_telegram
from .steelman import (
    SteelManResult,
    SeedFramework,
    load_seed_framework,
    install_seed_framework,
    get_default_framework,
)

__all__ = [
    "SKMEMORY_HOME",
    "Memory",
    "MemoryLayer",
    "EmotionalSnapshot",
    "MemoryStore",
    "FortifiedMemoryStore",
    "AuditLog",
    "TamperAlert",
    "FileBackend",
    "SQLiteBackend",
    "VaultedSQLiteBackend",
    "SoulBlueprint",
    "save_soul",
    "load_soul",
    "Journal",
    "JournalEntry",
    "perform_ritual",
    "quick_rehydrate",
    "RitualResult",
    "WarmthAnchor",
    "save_anchor",
    "load_anchor",
    "Quadrant",
    "classify_memory",
    "tag_with_quadrant",
    "LoveNote",
    "LoveNoteChain",
    "SKMemoryPlugin",
    "SteelManResult",
    "SeedFramework",
    "load_seed_framework",
    "install_seed_framework",
    "get_default_framework",
    "import_telegram",
    "__version__",
]
