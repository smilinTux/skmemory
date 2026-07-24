"""Tests for skmemory.wal — Write-Ahead Log"""

import pytest

from skmemory.wal import WriteAheadLog


@pytest.fixture
def wal(tmp_path):
    return WriteAheadLog(tmp_path / "wal" / "write_log.jsonl")


class TestWALCreation:
    def test_creates_parent_dir(self, tmp_path):
        wal = WriteAheadLog(tmp_path / "deep" / "nested" / "wal.jsonl")
        wal.log_pending("snapshot", "mem-001", "Test title", "short-term")
        assert (tmp_path / "deep" / "nested" / "wal.jsonl").exists()

    def test_file_initially_empty(self, wal):
        assert wal.tail() == []


class TestWALLogging:
    def test_log_pending(self, wal):
        wal.log_pending("snapshot", "mem-001", "Test Memory", "short-term")
        entries = wal.tail()
        assert len(entries) == 1
        e = entries[0]
        assert e["op"] == "snapshot"
        assert e["memory_id"] == "mem-001"
        assert e["title"] == "Test Memory"
        assert e["layer"] == "short-term"
        assert e["status"] == "pending"
        assert "ts" in e

    def test_log_done(self, wal):
        wal.log_pending("snapshot", "mem-001", "T", "short-term")
        wal.log_done("snapshot", "mem-001")
        entries = wal.tail()
        assert len(entries) == 2
        assert entries[-1]["status"] == "done"
        assert entries[-1]["memory_id"] == "mem-001"

    def test_log_failed(self, wal):
        wal.log_pending("snapshot", "mem-002", "T", "mid-term")
        wal.log_failed("snapshot", "mem-002", "disk full")
        entries = wal.tail()
        assert entries[-1]["status"] == "failed"
        assert entries[-1]["error"] == "disk full"

    def test_log_pending_with_metadata(self, wal):
        wal.log_pending("snapshot", "mem-003", "T", "long-term", metadata={"agent": "opus"})
        entries = wal.tail()
        assert entries[0]["meta"] == {"agent": "opus"}

    def test_multiple_operations(self, wal):
        for i in range(5):
            wal.log_pending("snapshot", f"mem-{i:03d}", f"Memory {i}", "short-term")
            wal.log_done("snapshot", f"mem-{i:03d}")
        entries = wal.tail()
        assert len(entries) == 10


class TestWALTail:
    def test_tail_returns_last_n(self, wal):
        for i in range(20):
            wal.log_done("snapshot", f"mem-{i:03d}")
        result = wal.tail(5)
        assert len(result) == 5
        # Last entry should be mem-019
        assert result[-1]["memory_id"] == "mem-019"

    def test_tail_all_when_fewer_than_n(self, wal):
        wal.log_done("snapshot", "mem-001")
        wal.log_done("snapshot", "mem-002")
        result = wal.tail(50)
        assert len(result) == 2

    def test_tail_nonexistent_file(self, tmp_path):
        wal = WriteAheadLog(tmp_path / "nonexistent" / "wal.jsonl")
        assert wal.tail() == []


class TestWALPendingWrites:
    def test_no_pending_when_all_done(self, wal):
        wal.log_pending("snapshot", "mem-001", "T", "short-term")
        wal.log_done("snapshot", "mem-001")
        assert wal.pending_writes() == []

    def test_detects_incomplete_write(self, wal):
        wal.log_pending("snapshot", "mem-crash", "Crash Memory", "short-term")
        # Simulate crash — no log_done follows
        incomplete = wal.pending_writes()
        assert len(incomplete) == 1
        assert incomplete[0]["memory_id"] == "mem-crash"

    def test_failed_does_not_show_as_pending(self, wal):
        wal.log_pending("snapshot", "mem-fail", "T", "short-term")
        wal.log_failed("snapshot", "mem-fail", "oops")
        assert wal.pending_writes() == []

    def test_mixed_scenario(self, wal):
        # mem-001: completed
        wal.log_pending("snapshot", "mem-001", "T", "short-term")
        wal.log_done("snapshot", "mem-001")
        # mem-002: crashed
        wal.log_pending("snapshot", "mem-002", "Crashed", "mid-term")
        # mem-003: failed
        wal.log_pending("snapshot", "mem-003", "T", "short-term")
        wal.log_failed("snapshot", "mem-003", "error")

        incomplete = wal.pending_writes()
        assert len(incomplete) == 1
        assert incomplete[0]["memory_id"] == "mem-002"
