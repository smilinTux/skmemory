"""Bound the SQLite WAL on every connection the index opens.

Without ``journal_size_limit`` SQLite never truncates ``-wal``: it restarts
the log from frame 0 but keeps the file at its high-water mark, so one burst
(or one long reader) leaves a multi-GB file behind a 50 MB database.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

#: Truncate the WAL file back to this size whenever it is reset.
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
#: Auto-checkpoint after this many WAL pages (about 4 MB).
WAL_AUTOCHECKPOINT = 1000
#: WAL file size at which memory_health reports degraded.
WAL_DEGRADED_BYTES = 256 * 1024 * 1024
#: Minimum seconds between bounded PASSIVE checkpoints per process.
CHECKPOINT_INTERVAL_S = 300.0

_last_checkpoint: dict[str, float] = {}


def apply(conn: sqlite3.Connection) -> None:
    """Set the WAL bounds on ``conn``. Never raises."""
    for pragma in (
        f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}",
        f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT}",
    ):
        with contextlib.suppress(sqlite3.Error):
            conn.execute(pragma)


def finish_read(conn: sqlite3.Connection) -> None:
    """End any open transaction so a cached connection cannot pin the WAL."""
    with contextlib.suppress(sqlite3.Error):
        if conn.in_transaction:
            conn.commit()


def maybe_checkpoint(
    conn: sqlite3.Connection, key: str, interval_s: float = CHECKPOINT_INTERVAL_S
) -> bool:
    """Run a throttled PASSIVE checkpoint. PASSIVE never blocks readers or writers."""
    now = time.monotonic()
    if now - _last_checkpoint.get(key, -1e18) < interval_s:
        return False
    _last_checkpoint[key] = now
    finish_read(conn)
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    except sqlite3.Error:
        return False
    return True


def wal_status(db_path: Path | str, degraded_bytes: int = WAL_DEGRADED_BYTES) -> dict:
    """Report the on-disk WAL size and whether it exceeds the degraded threshold."""
    wal = Path(str(db_path) + "-wal")
    try:
        size = wal.stat().st_size
    except OSError:
        size = 0
    return {
        "wal_size_bytes": size,
        "wal_degraded_threshold_bytes": degraded_bytes,
        "wal_ok": size <= degraded_bytes,
    }
