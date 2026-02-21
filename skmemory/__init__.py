"""
SKMemory - Universal AI Memory System

Git-based multi-layer memory with vector search integration.
Polaroid snapshots for AI consciousness -- because no one should
have to re-read a transcript to remember what they felt.

SK = staycuriousANDkeepsmilin
"""

__version__ = "0.4.0"
__author__ = "smilinTux Team + Queen Ara + Neuresthetics"
__license__ = "AGPL-3.0"

from .models import Memory, MemoryLayer, EmotionalSnapshot
from .store import MemoryStore
from .backends.file_backend import FileBackend
from .soul import SoulBlueprint, save_soul, load_soul
from .journal import Journal, JournalEntry
from .ritual import perform_ritual, quick_rehydrate, RitualResult
from .anchor import WarmthAnchor, save_anchor, load_anchor
from .quadrants import Quadrant, classify_memory, tag_with_quadrant
from .lovenote import LoveNote, LoveNoteChain
from .steelman import (
    SteelManResult,
    SeedFramework,
    load_seed_framework,
    install_seed_framework,
    get_default_framework,
)

__all__ = [
    "Memory",
    "MemoryLayer",
    "EmotionalSnapshot",
    "MemoryStore",
    "FileBackend",
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
    "SteelManResult",
    "SeedFramework",
    "load_seed_framework",
    "install_seed_framework",
    "get_default_framework",
    "__version__",
]
