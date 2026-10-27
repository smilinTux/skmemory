"""Empty memory IDs fail closed across storage and recovery boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.backends.file_backend import FileBackend
from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.backends.skgraph_backend import SKGraphBackend
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
    backend.index_memory = lambda memory, sync_hash=None: True
    backend.probe_connection = lambda: None

    result = backend.sync_all(tmp_path, "jarvis")
    assert (result["indexed"], result["errors"]) == (0, 0)
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


def test_import_backup_reports_empty_id_entries_as_skipped(tmp_path: Path) -> None:
    """An empty-id record must not be restored, and must not be restored SILENTLY.

    The original version of this test expected a `quarantine/invalid-memory-id`
    directory. main does not quarantine on this path; it refuses the record and
    REPORTS it, and the record is still present in the backup file the caller
    supplied, so nothing is lost by not copying it aside. What mattered was that
    a partial restore stopped looking complete, which it now does.
    """
    from skmemory.backends.sqlite_backend import SQLiteBackend

    backend = SQLiteBackend(base_path=str(tmp_path / "memory"))
    backup = tmp_path / "backup.json"
    backup.write_text(
        json.dumps(
            {
                "memories": [
                    {
                        "id": "",
                        "title": "Empty id",
                        "content": "must not store",
                        "layer": "short-term",
                    },
                    {
                        "id": "good1",
                        "title": "Good",
                        "content": "real content",
                        "layer": "short-term",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    restored = backend.import_backup(str(backup))

    assert restored == 1, "the empty-id record must not be restored"
    report = backend.last_import_report
    assert report["attempted"] == 2
    assert report["skipped"] == 1
    assert "empty or null" in report["skipped_detail"][0].lower()


def test_reconcile_quarantines_dot_json_filename(tmp_path: Path, monkeypatch) -> None:
    """reconcile must quarantine .json files (empty stem) before backfilling."""
    import unittest.mock as mock

    from skmemory import reconcile as reconcile_mod
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

    # Hermetic (no docker/psql transport, no registered profile on CI or any
    # host): stub the transport probe, the psql runner, and the memory profile
    # resolution so the test exercises the quarantine boundary only.
    monkeypatch.setattr(reconcile_mod, "probe_transport", lambda cmd: None)
    monkeypatch.setattr(
        reconcile_mod.subprocess,
        "run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    import skmemory.agents as agents_mod

    monkeypatch.setattr(
        agents_mod,
        "require_memory_profile",
        lambda name=None: type("P", (), {"profile_id": "test-agent"})(),
    )
    monkeypatch.setattr(reconcile_mod, "requests", mock.MagicMock())

    # Run reconcile (should quarantine the .json file)
    stats = reconcile(agent="test-agent", mem_dir=str(tmp_path), verbose=False)
    assert stats["flat"] == 0

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
    # Assert the guard FIRED, not its exact prose. The original pinned
    # "empty filename"; the reason now reads "Memory ID cannot be empty or
    # null", so this failed while the protection worked perfectly: the
    # quarantine report, its schema, and the offending source path were all
    # correct. A test pinned to wording breaks on a reworded message and
    # says nothing about whether the guard still holds.
    assert "empty" in entry["reason"].lower()

    # Content should not be in the report
    assert "This should be quarantined" not in json.dumps(entry)
