"""Atomic, durable flat-file writes.

Root cause (card: skmemory sqlite-only-orphans, 2026-10-16): every flat-file
write that backs a memory's source-of-truth storage used ``Path.write_text``/
``write_bytes``, which only guarantees the bytes reached the OS page cache.
On an unclean shutdown (sleep, battery cutoff, forced reboot -- exactly the
failure mode a laptop hits far more often than a server) that buffered write
can simply vanish, while the SQLite index commit for the same memory (flushed
at its own WAL checkpoint cadence) survives. The result is an index.db row
with no backing flat file: a SQLite-only orphan that never syncs to any other
node, because index.db is local and unsynced while the flat files are the
thing Syncthing carries between nodes.

This module closes that gap. ``atomic_write_bytes`` writes to a temp file in
the same directory, fsyncs the file descriptor, atomically renames it over
the target, then best-effort fsyncs the directory entry so the rename itself
survives a crash too.

Every backend's ``save()`` must call this (or ``atomic_write_text``) for the
flat file **before** touching any index, and must let any exception from it
propagate uncaught -- a flat write that didn't durably land must never be
indexed.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Durably write ``data`` to ``path``: atomic rename + fsync.

    On return, the bytes at ``path`` are guaranteed to be either the full new
    content or (if this raised) the prior content -- never a partial write,
    and never silently lost on a crash immediately after return.

    Args:
        path: Destination file path. Parent directories are created if
            missing.
        data: Raw bytes to write.

    Raises:
        OSError: If the write, fsync, or rename fails. Callers MUST treat
            this as "the flat file was not durably written" and must not
            proceed to index the memory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise

    # Best-effort: fsync the containing directory so the rename entry itself
    # is durable, not just the file content. Some filesystems/platforms don't
    # support fsync-ing a directory fd; never let that block a write that
    # already landed.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Text convenience wrapper around :func:`atomic_write_bytes`.

    Args:
        path: Destination file path.
        text: Text content to write.
        encoding: Text encoding (default ``utf-8``).

    Raises:
        OSError: See :func:`atomic_write_bytes`.
    """
    atomic_write_bytes(path, text.encode(encoding))
