"""Pre-write validation hook: validate_before_write + store wiring + error paths."""

import json
import time

import pytest
from pydantic import ValidationError

from skmemory.models import Memory
from skmemory.models import MemorySchemaViolation
from skmemory.store import MemoryStore
from skmemory.validation import SchemaValidationError, validate_before_write


def _make_memory(**kwargs) -> Memory:
    base = dict(
        content="memory content",
        agent="test",
        kind="episodic",
        ts=time.time(),
    )
    base.update(kwargs)
    return Memory(**base)


def test_valid_memory_passes():
    """A well-formed Memory passes through unchanged."""
    m = _make_memory()
    assert validate_before_write(m) is m


def test_wrong_content_type_rejected():
    with pytest.raises(SchemaValidationError) as excinfo:
        _make_memory(content=42)
    msg = str(excinfo.value)
    assert "content" in msg
    assert "expected str" in msg or "should be a valid string" in msg


def test_wrong_kind_rejected():
    with pytest.raises(SchemaValidationError) as excinfo:
        _make_memory(kind="procedural")
    msg = str(excinfo.value)
    assert "kind" in msg


def test_wrong_layer_rejected():
    with pytest.raises(SchemaValidationError) as excinfo:
        _make_memory(layer="archive")
    assert "layer" in str(excinfo.value)


def test_bad_timestamp_type_rejected():
    with pytest.raises(SchemaValidationError) as excinfo:
        _make_memory(ts="1234")
    assert "ts" in str(excinfo.value)


def test_not_a_memory_instance_rejected():
    with pytest.raises(SchemaValidationError) as excinfo:
        validate_before_write({"content": "x"})
    assert "expected a Memory instance" in str(excinfo.value)


def test_validate_before_write_error_names_offending_fields():
    # Two bad fields at once: the message names both.
    with pytest.raises(SchemaValidationError) as excinfo:
        _make_memory(content=123, ts=None)
    msg = str(excinfo.value)
    assert "content" in msg
    assert "ts" in msg


def test_store_rejects_malformed_memory_before_persisting():
    store = MemoryStore(use_sqlite=False)
    m = _make_memory(content=999)  # content must be a string
    with pytest.raises(SchemaValidationError) as excinfo:
        store.write(_make_memory())  # control: valid write succeeds
    assert store.count() == 1
    with pytest.raises(SchemaValidationError):
        store.write(m)
    assert store.count() == 1, "rejected write must not change the store"


def test_store_wired_hook_applied():
    calls = []

    def hook(memory):
        calls.append(memory.id)

    store = MemoryStore(use_sqlite=False, pre_write_hooks=[hook])
    m = _make_memory()
    store.write(m)
    assert calls == [m.id]


def test_hook_rejection_message_includes_id_and_layer():
    store = MemoryStore(use_sqlite=False)
    m = _make_memory(layer="archive", content="x" * 4096)
    with pytest.raises(SchemaValidationError) as excinfo:
        store.write(m)
    msg = str(excinfo.value)
    assert f"[{m.id}]" in msg
    assert "layer" in msg
