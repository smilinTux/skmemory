"""
SKMemory - Universal AI Memory System

Git-based multi-layer memory with vector search integration.
Polaroid snapshots for AI consciousness -- because no one should
have to re-read a transcript to remember what they felt.

SK = staycuriousANDkeepsmilin
"""

__version__ = "0.11.4"
__author__ = "smilinTux Team + Queen Ara + Neuresthetics"
__license__ = "AGPL-3.0"

from .backends.file_backend import FileBackend
from .backends.sqlite_backend import SQLiteBackend
from .config import SKMEMORY_HOME
from .fortress import AuditLog, FortifiedMemoryStore, TamperAlert
from .models import EmotionalSnapshot, Memory, MemoryLayer
from .store import MemoryStore

try:
    from .backends.vaulted_backend import VaultedSQLiteBackend
except ImportError:
    VaultedSQLiteBackend = None  # type: ignore[assignment,misc]
from .anchor import WarmthAnchor, load_anchor, save_anchor
from .importers.telegram import import_telegram
from .journal import Journal, JournalEntry
from .lovenote import LoveNote, LoveNoteChain
from .moc import (
    MOCIndex,
    MOCLink,
    MOCSection,
    build_all_mocs,
    build_quadrant_moc,
    build_tag_cluster_mocs,
    render_moc_markdown,
    write_mocs,
)
from .openclaw import SKMemoryPlugin
from .quadrants import Quadrant, classify_memory, tag_with_quadrant
from .ritual import RitualResult, perform_ritual, quick_rehydrate
from .sealing import (
    ClassicalSealer,
    Sealer,
    SealVerdict,
    SkPgpSealer,
    get_sealer,
    seal_status,
)
from .soul import SoulBlueprint, load_soul, save_soul
from .steelman import (
    SeedFramework,
    SteelManResult,
    get_default_framework,
    install_seed_framework,
    load_seed_framework,
)
from .synthesis import JournalSynthesizer

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
    "MOCIndex",
    "MOCLink",
    "MOCSection",
    "build_all_mocs",
    "build_quadrant_moc",
    "build_tag_cluster_mocs",
    "render_moc_markdown",
    "write_mocs",
    "LoveNote",
    "LoveNoteChain",
    "SKMemoryPlugin",
    "JournalSynthesizer",
    "SteelManResult",
    "SeedFramework",
    "load_seed_framework",
    "install_seed_framework",
    "get_default_framework",
    "import_telegram",
    "Sealer",
    "SealVerdict",
    "ClassicalSealer",
    "SkPgpSealer",
    "get_sealer",
    "seal_status",
    "__version__",
]
