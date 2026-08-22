"""Empty memory IDs fail closed across storage and recovery boundaries."""

from __future__ import annotations

import json

import pytest

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.backends.file_backend import FileBackend
from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.backends.skgraph_backend import SKGraphBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer


def _malformed() -> Memory:
    return Memory.model_construct(
        id="",
        title="Legacy invalid memory",
        content="payload",
        layer=MemoryLayer.SHORT,
    )


@pytest.mark.parametrize("backend_type", [FileBackend, SQLiteBackend])
def test_primary_backends_reject_empty_id_before_flat_or_sqlite_write(tmp_path, backend_type):
    backend = backend_type(base_path=str(tmp_path / "memory"))
    with pytest.raises(ValueError, match="cannot be empty"):
        backend.save(_malformed())
    assert not list((tmp_path / "memory").rglob(".json"))


def test_pgvector_rejects_empty_id_before_embedding_or_connection():
    backend = object.__new__(PGVectorBackend)
    with pytest.raises(ValueError, match="cannot be empty"):
        backend.save(_malformed())


def test_graph_backends_reject_empty_id_before_connection():
    age = object.__new__(AGEGraphBackend)
    age.graph = "test_graph"
    assert age.index_memory(_malformed()) is False

    falkor = object.__new__(SKGraphBackend)
    assert falkor.index_memory(_malformed()) is False


def test_age_sync_quarantines_dot_json_with_deterministic_report(tmp_path):
    short = tmp_path / "short-term"
    short.mkdir()
    source = short / ".json"
    source.write_text(json.dumps({"id": "", "title": "invalid", "content": "payload"}))

    backend = object.__new__(AGEGraphBackend)
    backend.graph = "test_graph"
    backend.index_memory = lambda memory: True

    assert backend.sync_all(tmp_path, "jarvis") == {"indexed": 0, "errors": 0}
    assert not source.exists()
    quarantine = tmp_path / "quarantine" / "invalid-memory-id"
    report = json.loads((quarantine / "report.json").read_text())
    assert report["schema"] == "skmemory.invalid-records/v1"
    assert report["entries"] == sorted(
        report["entries"], key=lambda item: (item["sha256"], item["source"])
    )
    assert report["entries"][0]["source"] == "short-term/.json"
    assert "payload" not in json.dumps(report)
    assert (tmp_path / report["entries"][0]["quarantine"]).is_file()
