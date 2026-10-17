"""Shared markers for content recovered from a lossy SQLite-preview rescue.

``skmemory/backends/sqlite_backend.py::export_orphans_to_flat()`` stamps a
rescued SQLite-only orphan with these exact values when it can only recover
the ~150-char ``content_preview`` column, not the memory's original full
content. Every write path into a derived store (``PGVectorBackend``, the
flat-to-pg ``skmem_reconcile.py`` script) must treat a memory carrying this
marker as a lossy, lower-trust copy: never let it silently replace a store
that might still hold the real content.

Root cause (card: skmemory sqlite-only-orphans, 2026-10-16): a flat->pg
reconcile run laundered 373 of these stubs directly into ``skmem-pg`` before
any write path checked for this marker. See
``docs/postmortems/2026-10-16-sqlite-only-orphans.md``.
"""

from __future__ import annotations

from typing import Any

STUB_METADATA_KEY = "recovered_from_sqlite_preview"
STUB_CONTENT_MARKER = "[recovered from SQLite preview"

# Appended to a SQLite-only orphan's content_preview when
# SQLiteBackend.export_orphans_to_flat() has no way to recover the real
# content. Also used there to detect a pg (or other fallback-source) row
# that was itself already populated from this same lossy stub by an earlier
# reconcile run -- that is not "full content" either, it is the same loss
# laundered through another store, and must not be trusted.
TRUNCATED_PREVIEW_SUFFIX = f"\n\n{STUB_CONTENT_MARKER} — full content lost]"


def is_stub_payload(content: str | None, metadata: dict[str, Any] | None) -> bool:
    """True if ``content``/``metadata`` carries the lossy-recovery marker.

    Args:
        content: The memory's content text (or None).
        metadata: The memory's metadata dict (or None).

    Returns:
        bool: True if this looks like a rescued SQLite-preview stub.
    """
    if metadata and metadata.get(STUB_METADATA_KEY):
        return True
    return bool(content) and STUB_CONTENT_MARKER in content
