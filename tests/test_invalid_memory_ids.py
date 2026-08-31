"""Empty memory IDs fail closed across storage and recovery boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.backends.chroma_backend import SKChromaBackend
from skmemory.backends.file_backend import FileBackend
from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.backends.skgraph_backend import SKGraphBackend
from skmemory.backends.skvector_backend import SKVectorBackend
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.backends.vaulted_backend import VaultedSQLiteBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


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
    backend.probe_connection = lambda: None

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


def test_chroma_backend_rejects_empty_id_before_dedup_check():
    """ChromaBackend must validate ID before using it in dedup tracker."""
    backend = object.__new__(SKChromaBackend)
    with pytest.raises(ValueError, match="cannot be empty"):
        backend.save(_malformed())


def test_skvector_backend_rejects_empty_id_before_dedup_check():
    """SKVectorBackend must validate ID before using it in dedup check."""
    backend = object.__new__(SKVectorBackend)
    with pytest.raises(ValueError, match="cannot be empty"):
        backend.save(_malformed())


def test_vaulted_backend_rejects_empty_id_before_encryption():
    """VaultedSQLiteBackend must validate ID before encrypting and writing."""
    backend = object.__new__(VaultedSQLiteBackend)
    with pytest.raises(ValueError, match="cannot be empty"):
        backend.save(_malformed())


def test_promotion_rejects_empty_source_id(tmp_path: Path) -> None:
    """MemoryStore.promote() must fail closed when source memory ID is empty."""
    backend = FileBackend(base_path=str(tmp_path / "memory"))
    store = MemoryStore(primary=backend)
    with pytest.raises(ValueError, match="cannot be empty"):
        store.promote("", MemoryLayer.MID)


def test_promotion_with_null_promoted_id_is_blocked(tmp_path: Path) -> None:
    """Promotion must verify the promoted copy has a non-empty ID before persisting."""
    backend = FileBackend(base_path=str(tmp_path / "memory"))
    store = MemoryStore(primary=backend)

    # Create a valid memory first
    original = store.snapshot(
        title="Test memory",
        content="Test content",
    )

    # Mock the promote() method to return a memory with empty ID
    # This simulates a bug where promote() could create a bad copy
    import unittest.mock as mock

    with mock.patch.object(
        store.primary,
        "load",
        return_value=original,
    ):
        # Create a malformed promoted copy
        bad_promoted = Memory.model_construct(
            id="",
            title="Promoted",
            content="Promoted content",
            layer=MemoryLayer.MID,
            parent_id=original.id,
        )

        with (
            mock.patch.object(Memory, "promote", return_value=bad_promoted),  # type: ignore[attr-defined]
            pytest.raises(ValueError, match="cannot be empty"),
        ):
            store.promote(original.id, MemoryLayer.MID)


def test_import_backup_quarantines_empty_id_entries(tmp_path: Path) -> None:
    """import_backup must quarantine records with empty/null IDs before writing."""
    backend = SQLiteBackend(base_path=str(tmp_path / "memory"))

    # Create a backup with one valid and one invalid entry
    backup_data = {
        "version": "0.11.0",
        "exported_at": "2026-08-30T00:00:00Z",
        "memories": [
            {
                "id": "valid-memory-id",
                "title": "Valid memory",
                "content": "This is valid content",
                "layer": "short-term",
                "created_at": "2026-08-30T00:00:00Z",
                "updated_at": "2026-08-30T00:00:00Z",
            },
            {
                "id": "",
                "title": "Invalid memory",
                "content": "This has an empty ID",
                "layer": "short-term",
                "created_at": "2026-08-30T00:00:00Z",
                "updated_at": "2026-08-30T00:00:00Z",
            },
            {
                "id": None,  # type: ignore[typeddict-item]
                "title": "Also invalid",
                "content": "This has a null ID",
                "layer": "short-term",
                "created_at": "2026-08-30T00:00:00Z",
                "updated_at": "2026-08-30T00:00:00Z",
            },
        ],
    }

    backup_path = tmp_path / "backup.json"
    backup_path.write_text(json.dumps(backup_data), encoding="utf-8")

    # Import should only restore the valid memory
    restored_count = backend.import_backup(str(backup_path))
    assert restored_count == 1

    # Valid memory should be in the store
    valid_mem = backend.load("valid-memory-id")
    assert valid_mem is not None
    assert valid_mem.title == "Valid memory"

    # Invalid entries should be quarantined
    quarantine = tmp_path / "memory" / "quarantine" / "invalid-memory-id"
    assert quarantine.exists()

    report_path = quarantine / "report.json"
    assert report_path.exists()

    report = json.loads(report_path.read_text())
    assert report["schema"] == "skmemory.invalid-records/v1"

    # Should have 2 quarantined entries (empty ID and null ID)
    assert len(report["entries"]) == 2

    for entry in report["entries"]:
        assert "empty/null ID in backup import" in entry["reason"]
        assert f"backup={backup_path.name}" in entry["reason"]
        # Content should not be in the report
        assert "This is valid content" not in json.dumps(entry)
        assert "This has an empty ID" not in json.dumps(entry)
        assert "This has a null ID" not in json.dumps(entry)


def test_reconcile_quarantines_dot_json_filename(tmp_path: Path) -> None:
    """reconcile must quarantine .json files (empty stem) before backfilling."""
    from skmemory.reconcile import reconcile

    short = tmp_path / "short-term"
    mid = tmp_path / "mid-term"
    short.mkdir()
    mid.mkdir()

    # Create a .json file (empty stem)
    dot_json = short / ".json"
    dot_json.write_text(
        json.dumps(
            {
                "id": "",
                "title": "Empty filename memory",
                "content": "This should be quarantined",
                "layer": "short-term",
            }
        )
    )

    # Run reconcile (should quarantine the .json file)
    reconcile(agent="test-agent", mem_dir=str(tmp_path))

    # The .json file should be removed
    assert not dot_json.exists()

    # It should be in quarantine with the right reason
    quarantine = tmp_path / "quarantine" / "invalid-memory-id"
    assert quarantine.exists()

    report_path = quarantine / "report.json"
    assert report_path.exists()

    report = json.loads(report_path.read_text())
    assert report["schema"] == "skmemory.invalid-records/v1"

    # Should have one entry for the .json file
    assert len(report["entries"]) == 1
    entry = report["entries"][0]
    assert ".json" in entry["source"]
    assert "empty filename" in entry["reason"].lower()

    # Content should not be in the report
    assert "This should be quarantined" not in json.dumps(entry)
