"""SKMEM-P0-21: the index WAL must stay bounded and visible."""

from __future__ import annotations

import sqlite3

from skmemory import wal_guard
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.intentions import IntentionStore


def _pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_backend_connection_is_bounded(tmp_path):
    b = SQLiteBackend(base_path=str(tmp_path))
    c = b._get_conn()
    assert _pragma(c, "journal_size_limit") == wal_guard.JOURNAL_SIZE_LIMIT
    assert _pragma(c, "wal_autocheckpoint") == wal_guard.WAL_AUTOCHECKPOINT
    b.close()


def test_intentions_connection_is_bounded(tmp_path):
    s = IntentionStore(tmp_path)
    c = s._db()
    assert _pragma(c, "journal_size_limit") == wal_guard.JOURNAL_SIZE_LIMIT
    assert _pragma(c, "wal_autocheckpoint") == wal_guard.WAL_AUTOCHECKPOINT
    s.close()


def test_oversized_wal_file_is_truncated(tmp_path):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    wal_guard_limit = 1024 * 1024
    c.execute(f"PRAGMA journal_size_limit={wal_guard_limit}")
    c.execute("CREATE TABLE t (v BLOB)")
    for _ in range(20):
        c.execute("INSERT INTO t VALUES (zeroblob(500000))")
    c.commit()
    assert (tmp_path / "x.db-wal").stat().st_size > 5 * wal_guard_limit
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert (tmp_path / "x.db-wal").stat().st_size <= wal_guard_limit
    c.close()


def test_maybe_checkpoint_throttled_and_ends_txn(tmp_path):
    c = sqlite3.connect(tmp_path / "y.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (v)")
    c.execute("INSERT INTO t VALUES (1)")  # leaves an open txn
    assert c.in_transaction
    assert wal_guard.maybe_checkpoint(c, "k", interval_s=60) is True
    assert not c.in_transaction
    assert wal_guard.maybe_checkpoint(c, "k", interval_s=60) is False
    c.close()


def test_health_reports_wal_and_degrades(tmp_path):
    b = SQLiteBackend(base_path=str(tmp_path))
    h = b.health_check()
    assert h["wal"]["wal_ok"] is True
    assert "wal_size_bytes" in h["wal"]
    wal = tmp_path / "index.db-wal"
    with open(wal, "ab") as f:
        f.truncate(wal_guard.WAL_DEGRADED_BYTES + 1)
    s = wal_guard.wal_status(tmp_path / "index.db")
    assert s["wal_ok"] is False
    b.close()
