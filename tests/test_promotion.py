"""Tests for the SKMemory auto-promotion engine."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.promotion import PromotionCriteria, PromotionEngine, PromotionResult, PromotionScheduler
from skmemory.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """Fresh MemoryStore with test memories."""
    from skmemory.backends.file_backend import FileBackend

    backend = FileBackend(base_path=tmp_path / "memories")
    s = MemoryStore(primary=backend)

    s.snapshot(
        title="High intensity moment",
        content="A breakthrough moment of deep connection",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=8.5, valence=0.9, labels=["joy", "love"]),
        tags=["milestone"],
    )

    old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    m = Memory(
        title="Frequently accessed",
        content="Something accessed many times",
        layer=MemoryLayer.SHORT,
        created_at=old_time,
        metadata={"access_count": 5},
    )
    s.primary.save(m)

    s.snapshot(
        title="Routine note",
        content="Just a regular note",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=1.0),
    )

    s.snapshot(
        title="Important mid-term",
        content="A mid-term memory with high emotional weight",
        layer=MemoryLayer.MID,
        emotional=EmotionalSnapshot(intensity=9.0, valence=0.95, labels=["love"], cloud9_achieved=True),
        tags=["cloud9:achieved"],
    )

    s.snapshot(
        title="Regular mid-term",
        content="Mid-term without special tags",
        layer=MemoryLayer.MID,
        emotional=EmotionalSnapshot(intensity=3.0),
    )

    return s


@pytest.fixture()
def engine(store: MemoryStore) -> PromotionEngine:
    """PromotionEngine with default criteria."""
    return PromotionEngine(store=store)


class TestPromotionCriteria:
    """Tests for the criteria model."""

    def test_defaults(self) -> None:
        """Default criteria have sensible values."""
        c = PromotionCriteria()
        assert c.short_to_mid_intensity == 5.0
        assert c.mid_to_long_intensity == 7.0
        assert c.cloud9_auto_promote is True
        assert c.max_promotions_per_sweep == 50

    def test_custom_criteria(self) -> None:
        """Custom criteria override defaults."""
        c = PromotionCriteria(short_to_mid_intensity=3.0, max_promotions_per_sweep=10)
        assert c.short_to_mid_intensity == 3.0
        assert c.max_promotions_per_sweep == 10


class TestEvaluate:
    """Tests for individual memory evaluation."""

    def test_high_intensity_short_qualifies(self, engine: PromotionEngine) -> None:
        """High-intensity short-term memory qualifies for mid-term."""
        m = Memory(
            title="Intense",
            content="Very intense moment",
            layer=MemoryLayer.SHORT,
            emotional=EmotionalSnapshot(intensity=8.0),
        )
        assert engine.evaluate(m) == MemoryLayer.MID

    def test_low_intensity_short_skipped(self, engine: PromotionEngine) -> None:
        """Low-intensity short-term memory doesn't qualify."""
        m = Memory(
            title="Meh",
            content="Nothing special",
            layer=MemoryLayer.SHORT,
            emotional=EmotionalSnapshot(intensity=1.0),
        )
        assert engine.evaluate(m) is None

    def test_cloud9_auto_promotes(self, engine: PromotionEngine) -> None:
        """Cloud 9 achieved memory auto-promotes."""
        m = Memory(
            title="Cloud 9",
            content="Peak moment",
            layer=MemoryLayer.SHORT,
            emotional=EmotionalSnapshot(intensity=3.0, cloud9_achieved=True),
        )
        assert engine.evaluate(m) == MemoryLayer.MID

    def test_mid_with_qualifying_tag(self, engine: PromotionEngine) -> None:
        """Mid-term memory with qualifying tag promotes to long-term."""
        m = Memory(
            title="Tagged",
            content="Important tagged memory",
            layer=MemoryLayer.MID,
            tags=["milestone"],
            emotional=EmotionalSnapshot(intensity=4.0),
        )
        assert engine.evaluate(m) == MemoryLayer.LONG

    def test_long_term_not_promoted(self, engine: PromotionEngine) -> None:
        """Long-term memories are not promoted further."""
        m = Memory(
            title="Already long",
            content="Already at the top",
            layer=MemoryLayer.LONG,
            emotional=EmotionalSnapshot(intensity=10.0),
        )
        assert engine.evaluate(m) is None

    def test_old_frequently_accessed_qualifies(self, engine: PromotionEngine) -> None:
        """Old memory with high access count qualifies."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        m = Memory(
            title="Old and popular",
            content="Accessed often",
            layer=MemoryLayer.SHORT,
            created_at=old_time,
            metadata={"access_count": 5},
            emotional=EmotionalSnapshot(intensity=2.0),
        )
        assert engine.evaluate(m) == MemoryLayer.MID


class TestSweep:
    """Tests for the full promotion sweep."""

    def test_sweep_promotes_qualifying(self, engine: PromotionEngine) -> None:
        """Sweep promotes memories that meet criteria."""
        result = engine.sweep()

        assert result.total_promoted > 0
        assert result.short_to_mid >= 1
        assert result.mid_to_long >= 1
        assert len(result.promoted_ids) == result.total_promoted

    def test_sweep_respects_max(self, store: MemoryStore) -> None:
        """Sweep respects max_promotions_per_sweep."""
        criteria = PromotionCriteria(
            short_to_mid_intensity=0.1,
            max_promotions_per_sweep=1,
        )
        engine = PromotionEngine(store=store, criteria=criteria)
        result = engine.sweep()

        assert result.short_to_mid <= 1

    def test_sweep_result_summary(self, engine: PromotionEngine) -> None:
        """Result summary is human-readable."""
        result = engine.sweep()
        summary = result.summary()
        assert "promoted" in summary
        assert "skipped" in summary

    def test_empty_store_sweep(self, tmp_path: Path) -> None:
        """Sweep on empty store produces zero promotions."""
        from skmemory.backends.file_backend import FileBackend

        backend = FileBackend(base_path=tmp_path / "empty")
        empty_store = MemoryStore(primary=backend)
        engine = PromotionEngine(store=empty_store)

        result = engine.sweep()
        assert result.total_promoted == 0
        assert result.errors == 0


class TestPromoteMemory:
    """Tests for individual memory promotion."""

    def test_promote_creates_new_memory(self, engine: PromotionEngine, store: MemoryStore) -> None:
        """Promoting a memory creates a new one at the target tier."""
        short_memories = store.list_memories(layer=MemoryLayer.SHORT)
        intense = next(m for m in short_memories if m.emotional.intensity >= 5.0)

        promoted = engine.promote_memory(intense, MemoryLayer.MID)
        assert promoted is not None
        assert promoted.layer == MemoryLayer.MID
        assert promoted.parent_id == intense.id
        assert "auto-promoted" in promoted.tags

    def test_promoted_has_summary(self, engine: PromotionEngine, store: MemoryStore) -> None:
        """Promoted memory has a generated summary."""
        short_memories = store.list_memories(layer=MemoryLayer.SHORT)
        intense = next(m for m in short_memories if m.emotional.intensity >= 5.0)

        promoted = engine.promote_memory(intense, MemoryLayer.MID)
        assert promoted.summary != ""

    def test_promoted_has_metadata(self, engine: PromotionEngine, store: MemoryStore) -> None:
        """Promoted memory has promotion metadata."""
        short_memories = store.list_memories(layer=MemoryLayer.SHORT)
        intense = next(m for m in short_memories if m.emotional.intensity >= 5.0)

        promoted = engine.promote_memory(intense, MemoryLayer.MID)
        assert "promoted_from" in promoted.metadata
        assert "promoted_at" in promoted.metadata
        assert "promotion_reason" in promoted.metadata


class TestPromotionResult:
    """Tests for the result model."""

    def test_total_promoted(self) -> None:
        """total_promoted sums both transitions."""
        r = PromotionResult(short_to_mid=3, mid_to_long=2)
        assert r.total_promoted == 5


class TestRePromotionGuard:
    """Ensure already-promoted memories are not promoted again."""

    def test_source_marked_after_promotion(
        self, engine: PromotionEngine, store: MemoryStore
    ) -> None:
        """After promotion, the source memory has 'promoted_to' in metadata."""
        short_mems = store.list_memories(layer=MemoryLayer.SHORT)
        intense = next(m for m in short_mems if m.emotional.intensity >= 5.0)

        engine.promote_memory(intense, MemoryLayer.MID)

        # Reload from store to confirm mutation was persisted
        reloaded = store.recall(intense.id)
        assert reloaded is not None
        assert reloaded.metadata.get("promoted_to") == MemoryLayer.MID.value
        assert "promoted" in reloaded.tags

    def test_promoted_memory_not_re_promoted(
        self, engine: PromotionEngine, store: MemoryStore
    ) -> None:
        """Running sweep twice doesn't double-promote the same memory."""
        result1 = engine.sweep()
        result2 = engine.sweep()

        # Second sweep should find nothing new to promote
        assert result2.total_promoted == 0

    def test_evaluate_skips_already_promoted(self, engine: PromotionEngine) -> None:
        """evaluate() returns None for a memory already marked as promoted."""
        m = Memory(
            title="Already done",
            content="This was already promoted",
            layer=MemoryLayer.SHORT,
            emotional=EmotionalSnapshot(intensity=9.0),
            metadata={"promoted_to": "mid-term"},
        )
        assert engine.evaluate(m) is None


class TestPromotionScheduler:
    """Tests for the background PromotionScheduler."""

    def test_run_once_returns_result(self, store: MemoryStore) -> None:
        """run_once() returns a PromotionResult synchronously."""
        scheduler = PromotionScheduler(store, interval_seconds=9999)
        result = scheduler.run_once()
        assert isinstance(result, PromotionResult)
        assert scheduler.sweep_count == 1
        assert scheduler.last_result is result

    def test_start_stop(self, store: MemoryStore) -> None:
        """Scheduler starts and stops the background thread cleanly."""
        scheduler = PromotionScheduler(store, interval_seconds=9999)
        assert not scheduler.is_running()

        scheduler.start()
        assert scheduler.is_running()

        scheduler.stop(timeout=2.0)
        assert not scheduler.is_running()

    def test_start_idempotent(self, store: MemoryStore) -> None:
        """Calling start() twice doesn't spawn a second thread."""
        scheduler = PromotionScheduler(store, interval_seconds=9999)
        scheduler.start()
        thread_id = scheduler._thread.ident

        scheduler.start()  # second call should be a no-op
        assert scheduler._thread.ident == thread_id

        scheduler.stop(timeout=2.0)

    def test_background_sweep_executes(self, store: MemoryStore) -> None:
        """Background thread runs at least one sweep in the first few seconds."""
        # Very short interval so the first sweep fires immediately in _run()
        scheduler = PromotionScheduler(store, interval_seconds=0.01)
        scheduler.start()
        time.sleep(0.2)
        scheduler.stop(timeout=2.0)

        assert scheduler.sweep_count >= 1

    def test_status_dict(self, store: MemoryStore) -> None:
        """status() returns expected keys."""
        scheduler = PromotionScheduler(store, interval_seconds=3600)
        s = scheduler.status()
        assert "running" in s
        assert "sweep_count" in s
        assert "interval_hours" in s
        assert s["interval_hours"] == pytest.approx(1.0)
        assert s["last_sweep"] is None  # nothing run yet

    def test_interval_hours_property(self, store: MemoryStore) -> None:
        """interval_hours converts correctly from seconds."""
        scheduler = PromotionScheduler(store, interval_seconds=7200)
        assert scheduler.interval_hours == pytest.approx(2.0)
