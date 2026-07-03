"""Tests for schema-validated writes via pluggable pre-write hooks."""

from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore
from skmemory.validation import (
    SchemaValidationError,
    default_pre_write_hooks,
    run_pre_write_hooks,
    schema_validator,
)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    backend = FileBackend(base_path=str(tmp_path / "memories"))
    return MemoryStore(primary=backend)


class TestSchemaValidator:
    """Unit tests for the default schema validator hook."""

    def test_valid_memory_passes(self) -> None:
        mem = Memory(title="Good", content="well-formed body")
        # Should not raise.
        schema_validator(mem)

    def test_non_memory_rejected(self) -> None:
        with pytest.raises(SchemaValidationError, match="expected a Memory instance"):
            schema_validator({"title": "not a memory"})  # type: ignore[arg-type]

    def test_malformed_empty_title_rejected(self) -> None:
        # model_construct bypasses validation, producing a malformed memory
        # that only the write-boundary validator can catch.
        bad = Memory.model_construct(title="   ", content="body")
        with pytest.raises(SchemaValidationError) as exc:
            schema_validator(bad)
        assert "title" in str(exc.value)
        assert "malformed against Memory schema" in str(exc.value)

    def test_malformed_bad_enum_rejected(self) -> None:
        bad = Memory.model_construct(title="T", content="body", layer="not-a-layer")
        with pytest.raises(SchemaValidationError) as exc:
            schema_validator(bad)
        assert "layer" in str(exc.value)

    def test_malformed_out_of_range_emotion_rejected(self) -> None:
        good = Memory(title="T", content="body")
        # Tamper post-construction: intensity above the 0-10 ceiling.
        object.__setattr__(good.emotional, "intensity", 99.0)
        with pytest.raises(SchemaValidationError) as exc:
            schema_validator(good)
        assert "intensity" in str(exc.value)


class TestPluggableHooks:
    """The hook chain is pluggable and enforced at the write boundary."""

    def test_valid_write_succeeds(self, store: MemoryStore) -> None:
        mem = store.snapshot(title="Valid", content="stored fine")
        assert store.recall(mem.id) is not None

    def test_custom_hook_can_reject(self, store: MemoryStore) -> None:
        class Rejected(ValueError):
            pass

        def no_secrets(memory: Memory) -> None:
            if "secret" in memory.content.lower():
                raise Rejected("content contains a forbidden token")

        store.register_pre_write_hook(no_secrets)

        with pytest.raises(Rejected, match="forbidden token"):
            store.snapshot(title="Leak", content="this is a SECRET")

        # And a clean write still goes through with the custom hook installed.
        ok = store.snapshot(title="Clean", content="nothing sensitive")
        assert store.recall(ok.id) is not None

    def test_custom_hook_runs_in_order(self) -> None:
        calls: list[str] = []

        def hook_a(memory: Memory) -> None:
            calls.append("a")

        def hook_b(memory: Memory) -> None:
            calls.append("b")

        mem = Memory(title="T", content="c")
        run_pre_write_hooks(mem, [hook_a, hook_b])
        assert calls == ["a", "b"]

    def test_hooks_can_be_disabled(self, tmp_path: Path) -> None:
        # Passing an explicit empty list overrides the defaults.
        backend = FileBackend(base_path=str(tmp_path / "m"))
        store = MemoryStore(primary=backend, pre_write_hooks=[])
        assert store.pre_write_hooks == []

    def test_default_hooks_installed(self, store: MemoryStore) -> None:
        assert schema_validator in store.pre_write_hooks

    def test_default_pre_write_hooks_fresh_list(self) -> None:
        # Each call returns an independent list so stores don't share state.
        a = default_pre_write_hooks()
        b = default_pre_write_hooks()
        assert a == b
        assert a is not b


class TestWriteBoundaryEnforcement:
    """Malformed memories injected at the store boundary are rejected."""

    def test_rejected_memory_not_persisted(self, store: MemoryStore) -> None:
        def reject_all(memory: Memory) -> None:
            raise SchemaValidationError("nope")

        store.register_pre_write_hook(reject_all)
        with pytest.raises(SchemaValidationError):
            store.snapshot(title="Doomed", content="never stored")

        # Nothing landed in the backend.
        assert store.list_memories(limit=50) == []
