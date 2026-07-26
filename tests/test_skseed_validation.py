"""Tests for write-time SKSeed validation (card 9b72c6c2).

Covers the store/write-flow SKSeed truth-check:
    * default OFF - no annotation, standard write path unchanged;
    * skseed PRESENT + enabled - truth_score annotated on store;
    * skseed ABSENT - store still works (fail-open no-op);
    * collider ERROR - fail-open, write proceeds unannotated;
    * contradiction flagging against existing memories;
    * config default + env resolution.

The tests inject a stub collider (via monkeypatching ``_build_collider``) so
they run deterministically whether or not the real ``skseed`` package is
installed. One test exercises the real package but is skipped when absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skmemory import skseed_validation
from skmemory.backends.file_backend import FileBackend
from skmemory.config import SKMemoryConfig, SKSeedConfig
from skmemory.store import MemoryStore


class _StubResult:
    """Stand-in for a skseed SteelManResult."""

    def __init__(
        self,
        coherence_score: float = 0.82,
        truth_grade: str = "strong",
        invariants: list[str] | None = None,
    ) -> None:
        self.coherence_score = coherence_score
        self.truth_grade = truth_grade
        self.invariants = invariants or []
        self.collision_fragments: list[str] = []


class _StubCollider:
    """Minimal collider exposing the surface annotate_truth_score touches."""

    def __init__(
        self,
        result: _StubResult | None = None,
        cross: dict | None = None,
        raises: bool = False,
    ) -> None:
        self._result = result or _StubResult()
        self._cross = cross
        self._raises = raises
        self.calls = 0

    def truth_score_memory(self, content: str) -> _StubResult:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._result

    def cross_reference(self, results: list) -> dict:
        return self._cross or {"conflicts": [], "universal_invariants": {}}


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    """A MemoryStore on a temp file backend with SKSeed validation OFF."""
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend, skseed_auto_validate=False)


class TestDefaultOff:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        """When the flag resolves off, no truth_score is written."""
        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=False)
        mem = st.snapshot(title="t", content="the sky is blue")
        assert "truth_score" not in mem.metadata
        assert "skseed" not in mem.metadata
        # And it is recallable / write path intact.
        assert st.recall(mem.id) is not None

    def test_disabled_does_not_call_collider(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off -> the collider builder is never invoked (zero cost)."""
        called = {"n": 0}

        def _spy() -> None:
            called["n"] += 1
            return None

        monkeypatch.setattr(skseed_validation, "_build_collider", _spy)
        store.snapshot(title="t", content="content here")
        assert called["n"] == 0


class TestEnabledWithSkseed:
    def test_truth_score_annotated_on_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skseed present + enabled -> memory gets a truth_score on write."""
        stub = _StubCollider(_StubResult(coherence_score=0.77, truth_grade="strong"))
        monkeypatch.setattr(skseed_validation, "_build_collider", lambda: stub)

        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=True)
        mem = st.snapshot(title="t", content="water boils at 100C at sea level")

        assert stub.calls == 1
        assert mem.metadata["truth_score"] == 0.77
        assert mem.metadata["skseed"]["truth_grade"] == "strong"
        assert mem.metadata["skseed"]["validated_by"] == "skseed"

        # Annotation is durable through the write (persisted + recalled).
        recalled = st.recall(mem.id)
        assert recalled is not None
        assert recalled.metadata["truth_score"] == 0.77

    def test_integrity_seal_valid_after_annotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Annotating metadata before seal() must not break integrity."""
        monkeypatch.setattr(skseed_validation, "_build_collider", lambda: _StubCollider())
        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=True)
        mem = st.snapshot(title="t", content="a claim to verify")
        assert mem.verify_integrity() is True


class TestFailOpen:
    def test_store_works_when_skseed_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skseed absent (collider builder yields None) -> store still works."""
        monkeypatch.setattr(skseed_validation, "_build_collider", lambda: None)
        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=True)
        mem = st.snapshot(title="t", content="stored without skseed")
        assert "truth_score" not in mem.metadata
        assert st.recall(mem.id) is not None

    def test_collider_error_is_fail_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A collider that raises must not block the write."""
        monkeypatch.setattr(
            skseed_validation,
            "_build_collider",
            lambda: _StubCollider(raises=True),
        )
        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=True)
        mem = st.snapshot(title="t", content="this will error the collider")
        assert "truth_score" not in mem.metadata
        assert st.recall(mem.id) is not None


class TestContradictionFlagging:
    def test_contradictions_flagged_against_existing(self, store: MemoryStore) -> None:
        """When the collider reports conflicts, they are attached to metadata."""
        # Seed an existing memory to be found as a neighbour.
        existing = store.snapshot(title="old", content="the moon is made of cheese")

        result = _StubResult(coherence_score=0.6, invariants=["moon-composition"])
        cross = {"conflicts": ["moon is rock, not cheese"], "universal_invariants": {}}
        collider = _StubCollider(result=result, cross=cross)

        new_mem = store.snapshot(title="new", content="the moon is solid rock")
        detail = skseed_validation.annotate_truth_score(new_mem, store=store, collider=collider)
        assert detail is not None
        assert "contradictions" in detail
        assert detail["contradictions"][0]["memory_id"] == existing.id
        assert detail["contradictions"][0]["conflicts"] == ["moon is rock, not cheese"]

    def test_no_contradictions_without_invariants(self, store: MemoryStore) -> None:
        """No invariants (offline collider) -> no contradiction cross-ref."""
        store.snapshot(title="old", content="some prior memory")
        collider = _StubCollider(_StubResult(invariants=[]))
        new_mem = store.snapshot(title="new", content="another memory")
        detail = skseed_validation.annotate_truth_score(new_mem, store=store, collider=collider)
        assert detail is not None
        assert "contradictions" not in detail


class TestConfig:
    def test_config_default_off(self) -> None:
        assert SKMemoryConfig().skseed.auto_validate is False
        assert SKSeedConfig().auto_validate is False

    def test_config_roundtrips_nested_flag(self) -> None:
        cfg = SKMemoryConfig(skseed=SKSeedConfig(auto_validate=True))
        dumped = cfg.model_dump()
        assert dumped["skseed"]["auto_validate"] is True
        assert SKMemoryConfig(**dumped).skseed.auto_validate is True

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SKMEMORY_SKSEED_AUTO_VALIDATE", "true")
        assert skseed_validation.resolve_auto_validate() is True

    def test_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SKMEMORY_SKSEED_AUTO_VALIDATE", "0")
        assert skseed_validation.resolve_auto_validate() is False


class TestRealSkseed:
    @pytest.mark.skipif(
        not skseed_validation.skseed_available(),
        reason="skseed not installed (standalone mode)",
    )
    def test_real_collider_offline_is_ungraded_and_safe(self, tmp_path: Path) -> None:
        """The real, offline (no-LLM) collider annotates without raising."""
        backend = FileBackend(base_path=str(tmp_path / "m"))
        st = MemoryStore(primary=backend, skseed_auto_validate=True)
        mem = st.snapshot(title="t", content="a proposition to score")
        # Offline collider -> ungraded, score 0.0, but the field is present and
        # the write succeeded.
        assert "truth_score" in mem.metadata
        assert mem.metadata["skseed"]["validated_by"] == "skseed"
        assert st.recall(mem.id) is not None
