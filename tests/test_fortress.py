"""Tests for Memory Fortress — auto-seal, audit trail, tamper alerts.

Tests the FortifiedMemoryStore, AuditLog, and TamperAlert classes.
All tests use in-memory/temp-path backends — no GPG required for basic tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from skmemory.fortress import AuditLog, FortifiedMemoryStore, TamperAlert
from skmemory.models import Memory, MemoryLayer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_audit(tmp_path):
    """Return a temporary audit log."""
    return AuditLog(path=tmp_path / "audit.jsonl")


@pytest.fixture
def fortress(tmp_path):
    """Return a FortifiedMemoryStore using a temp directory."""
    from skmemory.backends.sqlite_backend import SQLiteBackend
    backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
    return FortifiedMemoryStore(
        primary=backend,
        use_sqlite=False,
        audit_path=tmp_path / "audit.jsonl",
    )


# ---------------------------------------------------------------------------
# AuditLog tests
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_append_creates_file(self, tmp_audit, tmp_path):
        tmp_audit.append("store", "abc123", ok=True)
        assert (tmp_path / "audit.jsonl").exists()

    def test_record_format(self, tmp_audit):
        tmp_audit.append("recall", "mem1", ok=True, integrity="ok")
        records = tmp_audit.tail(1)
        assert len(records) == 1
        r = records[0]
        assert r["op"] == "recall"
        assert r["id"] == "mem1"
        assert r["ok"] is True
        assert r["integrity"] == "ok"
        assert "ts" in r
        assert "chain_hash" in r

    def test_chain_hash_progresses(self, tmp_audit):
        tmp_audit.append("store", "a")
        tmp_audit.append("store", "b")
        tmp_audit.append("recall", "a")
        records = tmp_audit.tail(10)
        assert len(records) == 3
        # Each chain hash should be different
        hashes = [r["chain_hash"] for r in records]
        assert len(set(hashes)) == 3

    def test_verify_chain_valid(self, tmp_audit):
        for i in range(5):
            tmp_audit.append("store", f"mem{i}", ok=True)
        ok, errors = tmp_audit.verify_chain()
        assert ok, f"Chain should be valid but got errors: {errors}"

    def test_verify_chain_tampered(self, tmp_audit, tmp_path):
        for i in range(3):
            tmp_audit.append("store", f"mem{i}", ok=True)

        # Tamper with the file — alter the second line
        audit_path = tmp_path / "audit.jsonl"
        lines = audit_path.read_text().splitlines()
        record = json.loads(lines[1])
        record["op"] = "delete"  # tamper!
        lines[1] = json.dumps(record)
        audit_path.write_text("\n".join(lines) + "\n")

        ok, errors = tmp_audit.verify_chain()
        assert not ok
        assert len(errors) > 0

    def test_tail_respects_limit(self, tmp_audit):
        for i in range(10):
            tmp_audit.append("store", f"mem{i}")
        records = tmp_audit.tail(3)
        assert len(records) == 3

    def test_empty_log_verify(self, tmp_audit):
        ok, errors = tmp_audit.verify_chain()
        assert ok
        assert errors == []


# ---------------------------------------------------------------------------
# TamperAlert tests
# ---------------------------------------------------------------------------

class TestTamperAlert:
    def test_to_dict(self):
        alert = TamperAlert(
            memory_id="abc",
            expected_hash="aaa",
            actual_hash="bbb",
        )
        d = alert.to_dict()
        assert d["memory_id"] == "abc"
        assert d["expected_hash"] == "aaa"
        assert d["actual_hash"] == "bbb"
        assert d["severity"] == "CRITICAL"
        assert "tamper" in d["message"].lower() or "integrity" in d["message"].lower()
        assert "detected_at" in d

    def test_repr(self):
        alert = TamperAlert("x", "a", "b")
        assert "x" in repr(alert)


# ---------------------------------------------------------------------------
# FortifiedMemoryStore tests
# ---------------------------------------------------------------------------

class TestFortifiedMemoryStore:
    def test_snapshot_seals_memory(self, fortress):
        mem = fortress.snapshot("Test title", "Test content")
        assert mem.integrity_hash != "", "Memory should be sealed on write"

    def test_recall_passes_clean_memory(self, fortress):
        mem = fortress.snapshot("Clean", "No tampering here")
        recalled = fortress.recall(mem.id)
        assert recalled is not None
        assert "integrity_warning" not in recalled.metadata

    def test_recall_missing_returns_none(self, fortress):
        result = fortress.recall("nonexistent-id")
        assert result is None

    def test_tamper_detection_triggers_callback(self, fortress, tmp_path):
        alerts_received: list[TamperAlert] = []
        fortress.register_alert_callback(alerts_received.append)

        mem = fortress.snapshot("Secret", "Original content")

        # Tamper: directly modify the stored memory file
        # We need to find and corrupt the JSON
        from skmemory.backends.sqlite_backend import SQLiteBackend
        backend = fortress.primary
        # Load raw, mutate, save back bypassing seal
        raw = backend.load(mem.id)
        assert raw is not None
        raw.content = "TAMPERED CONTENT"
        raw.integrity_hash = mem.integrity_hash  # keep old hash
        backend.save(raw)  # save tampered version with original hash

        # Now recall — should trigger tamper alert
        recalled = fortress.recall(mem.id)
        assert recalled is not None
        assert "integrity_warning" in recalled.metadata
        assert len(alerts_received) == 1
        alert = alerts_received[0]
        assert alert.memory_id == mem.id
        assert alert.expected_hash == mem.integrity_hash

    def test_forget_audited(self, fortress):
        mem = fortress.snapshot("Temp", "Will be deleted")
        fortress.forget(mem.id)
        trail = fortress.audit_trail(10)
        ops = [r["op"] for r in trail]
        assert "delete" in ops

    def test_audit_trail_records_all_ops(self, fortress):
        mem = fortress.snapshot("Op tracking", "content")
        fortress.recall(mem.id)
        fortress.forget(mem.id)

        trail = fortress.audit_trail(10)
        ops = [r["op"] for r in trail]
        assert "store" in ops
        assert "recall" in ops
        assert "delete" in ops

    def test_verify_all_clean_store(self, fortress):
        for i in range(5):
            fortress.snapshot(f"Memory {i}", f"Content {i}")
        result = fortress.verify_all()
        assert result["total"] == 5
        assert result["passed"] == 5
        assert result["tampered"] == []

    def test_verify_all_finds_tampered(self, fortress):
        alerts: list[TamperAlert] = []
        fortress.register_alert_callback(alerts.append)

        mem = fortress.snapshot("Good memory", "Original")

        # Tamper via backend
        raw = fortress.primary.load(mem.id)
        raw.content = "CORRUPTED"
        raw.integrity_hash = mem.integrity_hash
        fortress.primary.save(raw)

        result = fortress.verify_all()
        assert mem.id in result["tampered"]
        assert len(alerts) > 0

    def test_verify_audit_chain(self, fortress):
        fortress.snapshot("A", "a")
        fortress.snapshot("B", "b")
        ok, errors = fortress.verify_audit_chain()
        assert ok, errors

    def test_encryption_not_configured_raises(self, fortress):
        with pytest.raises(RuntimeError, match="not configured"):
            fortress.encrypt_payload('{"test": 1}')

    def test_encryption_active_false_by_default(self, fortress):
        assert fortress.encryption_active is False

    def test_multiple_callbacks(self, fortress):
        c1, c2 = [], []
        fortress.register_alert_callback(c1.append)
        fortress.register_alert_callback(c2.append)

        mem = fortress.snapshot("Multi", "content")
        raw = fortress.primary.load(mem.id)
        raw.content = "bad"
        raw.integrity_hash = mem.integrity_hash
        fortress.primary.save(raw)

        fortress.recall(mem.id)
        assert len(c1) == 1
        assert len(c2) == 1

    def test_unsealed_memory_passes_silently(self, fortress):
        """A memory with no integrity_hash is not flagged as tampered."""
        mem = fortress.snapshot("Unsealed", "content")
        raw = fortress.primary.load(mem.id)
        raw.integrity_hash = ""  # strip the seal
        fortress.primary.save(raw)

        recalled = fortress.recall(mem.id)
        assert recalled is not None
        # Should not have a warning — unsealed != tampered
        assert "integrity_warning" not in recalled.metadata
