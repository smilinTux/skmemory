"""Tests for the AMK-inspired predictive memory recall module.

Covers access logging, co-occurrence learning, tag affinity,
prediction ranking, and persistence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from skmemory.predictive import AccessEvent, PredictiveRecall


@pytest.fixture
def recall(tmp_path: Path) -> PredictiveRecall:
    """Provide a PredictiveRecall with temp storage."""
    return PredictiveRecall(log_path=tmp_path / "access_log.json")


class TestAccessLogging:
    """Test access event recording."""

    def test_log_access_creates_event(self, recall: PredictiveRecall):
        """Logging an access adds to the event list."""
        recall.log_access("mem-001", tags=["cloud9"])
        assert recall._frequency["mem-001"] == 1

    def test_log_multiple_accesses(self, recall: PredictiveRecall):
        """Multiple accesses increase frequency."""
        for _ in range(5):
            recall.log_access("mem-002")
        assert recall._frequency["mem-002"] == 5

    def test_log_persists_to_disk(self, recall: PredictiveRecall):
        """Access log is written to disk."""
        recall.log_access("mem-003")
        assert recall._log_path.exists()
        data = json.loads(recall._log_path.read_text())
        assert len(data) == 1
        assert data[0]["memory_id"] == "mem-003"

    def test_log_reloads_from_disk(self, tmp_path: Path):
        """Access log survives re-creation."""
        path = tmp_path / "access_log.json"
        r1 = PredictiveRecall(log_path=path)
        r1.log_access("mem-004", tags=["test"])
        r1.log_access("mem-005", tags=["test"])

        r2 = PredictiveRecall(log_path=path)
        r2._ensure_loaded()
        assert len(r2._events) == 2

    def test_max_events_pruning(self, tmp_path: Path):
        """Events are pruned when exceeding max_events."""
        r = PredictiveRecall(log_path=tmp_path / "log.json", max_events=10)
        for i in range(20):
            r.log_access(f"mem-{i:03d}")
        assert len(r._events) <= 10


class TestCooccurrence:
    """Test co-occurrence pattern learning."""

    def test_cooccurrence_within_session(self, recall: PredictiveRecall):
        """Memories accessed close together build co-occurrence."""
        now = time.time()
        recall._events = [
            AccessEvent(memory_id="A", timestamp=now),
            AccessEvent(memory_id="B", timestamp=now + 1),
            AccessEvent(memory_id="C", timestamp=now + 2),
        ]
        recall._rebuild_indices()
        assert recall._cooccurrence["A"]["B"] > 0
        assert recall._cooccurrence["A"]["C"] > 0
        assert recall._cooccurrence["B"]["C"] > 0

    def test_no_cooccurrence_across_sessions(self, recall: PredictiveRecall):
        """Memories in different sessions don't co-occur."""
        now = time.time()
        recall._events = [
            AccessEvent(memory_id="A", timestamp=now),
            AccessEvent(memory_id="B", timestamp=now + 600),
        ]
        recall._rebuild_indices()
        assert recall._cooccurrence["A"].get("B", 0) == 0


class TestTagAffinity:
    """Test tag-based prediction."""

    def test_tag_affinity_builds(self, recall: PredictiveRecall):
        """Tags on accessed memories build affinity scores."""
        recall.log_access("mem-001", tags=["cloud9", "love"])
        recall.log_access("mem-002", tags=["cloud9", "trust"])
        recall.log_access("mem-003", tags=["cloud9"])

        assert recall._tag_affinity["cloud9"]["mem-001"] == 1
        assert recall._tag_affinity["cloud9"]["mem-003"] == 1


class TestPrediction:
    """Test the prediction engine."""

    def test_predict_from_cooccurrence(self, recall: PredictiveRecall):
        """Predictions include co-occurring memories."""
        now = time.time()
        recall._events = [
            AccessEvent(memory_id="A", timestamp=now),
            AccessEvent(memory_id="B", timestamp=now + 1),
            AccessEvent(memory_id="A", timestamp=now + 100),
            AccessEvent(memory_id="B", timestamp=now + 101),
        ]
        recall._rebuild_indices()

        predictions = recall.predict(recent_ids=["A"])
        ids = [p["memory_id"] for p in predictions]
        assert "B" in ids

    def test_predict_from_tags(self, recall: PredictiveRecall):
        """Predictions include tag-affiliated memories."""
        recall.log_access("mem-010", tags=["kingdom"])
        recall.log_access("mem-011", tags=["kingdom"])
        recall.log_access("mem-012", tags=["kingdom"])

        predictions = recall.predict(active_tags=["kingdom"])
        ids = [p["memory_id"] for p in predictions]
        assert len(ids) > 0

    def test_predict_excludes_recent(self, recall: PredictiveRecall):
        """Already-accessed memories are excluded from predictions."""
        now = time.time()
        recall._events = [
            AccessEvent(memory_id="A", timestamp=now),
            AccessEvent(memory_id="B", timestamp=now + 1),
        ]
        recall._rebuild_indices()

        predictions = recall.predict(recent_ids=["A", "B"])
        ids = [p["memory_id"] for p in predictions]
        assert "A" not in ids
        assert "B" not in ids

    def test_predict_empty_returns_empty(self, recall: PredictiveRecall):
        """No data = no predictions."""
        assert recall.predict() == []

    def test_predict_limit(self, recall: PredictiveRecall):
        """Predictions respect the limit parameter."""
        for i in range(20):
            recall.log_access(f"mem-{i:03d}", tags=["bulk"])

        predictions = recall.predict(active_tags=["bulk"], limit=5)
        assert len(predictions) <= 5

    def test_predictions_have_reasons(self, recall: PredictiveRecall):
        """Each prediction explains why it was chosen."""
        recall.log_access("mem-X", tags=["reason-test"])
        predictions = recall.predict(active_tags=["reason-test"])
        if predictions:
            assert len(predictions[0]["reasons"]) > 0


class TestStats:
    """Test statistics reporting."""

    def test_stats_empty(self, recall: PredictiveRecall):
        """Stats work with no data."""
        stats = recall.get_stats()
        assert stats["total_events"] == 0
        assert stats["unique_memories"] == 0

    def test_stats_populated(self, recall: PredictiveRecall):
        """Stats reflect recorded events."""
        recall.log_access("mem-A", tags=["x"])
        recall.log_access("mem-B", tags=["x"])
        recall.log_access("mem-A", tags=["x"])
        stats = recall.get_stats()
        assert stats["total_events"] == 3
        assert stats["unique_memories"] == 2


class TestIntegrity:
    """Test AMK-inspired memory integrity features."""

    def test_memory_seal_and_verify(self):
        """Memory seal + verify roundtrip works."""
        from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

        mem = Memory(
            title="Integrity Test",
            content="This memory can prove it hasn't been tampered with.",
            layer=MemoryLayer("long-term"),
            emotional=EmotionalSnapshot(intensity=8.0),
        )
        mem.seal()
        assert mem.integrity_hash != ""
        assert mem.verify_integrity() is True

    def test_tampered_memory_fails_verification(self):
        """Altered content fails integrity check."""
        from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

        mem = Memory(
            title="Tamper Test",
            content="Original content.",
            layer=MemoryLayer("long-term"),
        )
        mem.seal()
        mem.content = "Tampered content!"
        assert mem.verify_integrity() is False

    def test_unsealed_memory_passes(self):
        """Memories without integrity hash pass by default."""
        from skmemory.models import Memory, MemoryLayer

        mem = Memory(
            title="Unsealed",
            content="No hash yet.",
            layer=MemoryLayer("short-term"),
        )
        assert mem.verify_integrity() is True

    def test_intent_field_stored(self):
        """Memory intent field captures WHY."""
        from skmemory.models import Memory, MemoryLayer

        mem = Memory(
            title="Intent Test",
            content="Some content",
            layer=MemoryLayer("mid-term"),
            intent="Stored because Chef asked me to remember this moment.",
        )
        assert "Chef" in mem.intent
