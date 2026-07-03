"""Tests for the fresh-context runner seam used by consolidation/promotion.

Verifies that promotion/consolidation passes run *through* an injectable
fresh-context runner (mocked here), that the runner is honoured, and that the
pass's result is returned/applied unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skmemory.fresh_context import (
    FreshContextRunner,
    SubprocessRunner,
    in_process_runner,
    resolve_runner,
)
from skmemory.models import EmotionalSnapshot, MemoryLayer
from skmemory.promotion import PromotionEngine, PromotionResult, PromotionScheduler
from skmemory.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """Fresh MemoryStore with one clearly-promotable short-term memory."""
    from skmemory.backends.file_backend import FileBackend

    backend = FileBackend(base_path=tmp_path / "memories")
    s = MemoryStore(primary=backend)
    s.snapshot(
        title="High intensity moment",
        content="A breakthrough moment of deep connection",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=8.5, valence=0.9, labels=["joy"]),
        tags=["milestone"],
    )
    return s


# ── the seam primitives ──────────────────────────────────────────────────────


def test_in_process_runner_is_identity() -> None:
    """The default runner just calls the pass and returns its result."""
    assert in_process_runner(lambda: 42) == 42


def test_resolve_runner_defaults_to_in_process() -> None:
    assert resolve_runner(None) is in_process_runner
    custom = lambda fn: fn()  # noqa: E731
    assert resolve_runner(custom) is custom


def test_subprocess_runner_delegates_to_injected_spawn() -> None:
    """SubprocessRunner routes the pass through the injected spawner, no LLM."""
    calls: list = []

    def spawn(pass_fn):
        calls.append(pass_fn)
        return pass_fn()

    runner = SubprocessRunner(spawn)
    assert isinstance(runner, FreshContextRunner)  # runtime_checkable protocol
    assert runner(lambda: "ok") == "ok"
    assert len(calls) == 1


# ── engine wiring ────────────────────────────────────────────────────────────


def test_engine_runs_pass_via_injected_runner(store: MemoryStore) -> None:
    """run_pass() dispatches the sweep through the injected fresh-context runner."""
    seen: dict = {"called": 0, "pass_fn": None}

    def mock_runner(pass_fn):
        seen["called"] += 1
        seen["pass_fn"] = pass_fn
        return pass_fn()  # simulate running in a fresh context

    engine = PromotionEngine(store, runner=mock_runner)
    result = engine.run_pass()

    assert seen["called"] == 1
    assert callable(seen["pass_fn"])
    assert isinstance(result, PromotionResult)
    # Result is applied: the promotable memory was actually promoted.
    assert result.short_to_mid == 1
    assert store.list_memories(layer=MemoryLayer.MID)


def test_engine_runner_result_is_returned_unmodified(store: MemoryStore) -> None:
    """A mocked runner can substitute a canned result without touching memory."""
    canned = PromotionResult(short_to_mid=7, mid_to_long=3)

    def mock_runner(pass_fn):  # note: does NOT call pass_fn
        return canned

    engine = PromotionEngine(store, runner=mock_runner)
    result = engine.run_pass()

    assert result is canned
    assert result.total_promoted == 10
    # Since the pass never ran, nothing was actually promoted.
    assert not store.list_memories(layer=MemoryLayer.MID)


def test_engine_default_runner_matches_direct_sweep(store: MemoryStore) -> None:
    """Default (in-process) run_pass is behaviourally identical to sweep()."""
    engine = PromotionEngine(store)
    result = engine.run_pass()
    assert isinstance(result, PromotionResult)
    assert result.short_to_mid == 1


def test_engine_runner_exceptions_propagate(store: MemoryStore) -> None:
    def boom(pass_fn):
        raise RuntimeError("spawn failed")

    engine = PromotionEngine(store, runner=boom)
    with pytest.raises(RuntimeError, match="spawn failed"):
        engine.run_pass()


# ── scheduler wiring ─────────────────────────────────────────────────────────


def test_scheduler_run_once_uses_fresh_context_runner(store: MemoryStore) -> None:
    """Scheduler.run_once routes its sweep through the injected runner."""
    calls: list = []

    def mock_runner(pass_fn):
        calls.append(pass_fn)
        return pass_fn()

    scheduler = PromotionScheduler(store, runner=mock_runner)
    result = scheduler.run_once()

    assert len(calls) == 1
    assert scheduler.sweep_count == 1
    assert result.short_to_mid == 1
    assert scheduler.last_result is result
