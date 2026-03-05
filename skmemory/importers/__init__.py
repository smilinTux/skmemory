"""
Chat history importers for SKMemory.

Converts exports from messaging platforms into searchable,
emotionally-indexed memories. Each importer reads a specific
export format and feeds it through MemoryStore.snapshot().
"""

from .telegram import import_telegram

try:
    from .telegram_api import check_setup as check_telegram_setup
    from .telegram_api import import_telegram_api
except ImportError:
    # telethon not installed — API import unavailable
    import_telegram_api = None  # type: ignore[assignment]
    check_telegram_setup = None  # type: ignore[assignment]

__all__ = ["import_telegram", "import_telegram_api", "check_telegram_setup"]
