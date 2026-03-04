"""Tests for backup rotation (list, prune, auto-rotate on export)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.cli import cli
from skmemory.models import EmotionalSnapshot, MemoryLayer
from skmemory.store import MemoryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path):
    """SQLiteBackend with temporary storage."""
    return SQLiteBackend(base_path=str(tmp_path / "memories"))


@pytest.fixture
def store(backend):
    """MemoryStore wrapping the temp backend."""
    return MemoryStore(primary=backend)


@pytest.fixture
def populated_store(store):
    """Store pre-loaded with 3 memories."""
    for i in range(3):
        store.snapshot(
            title=f"Memory {i}",
            content=f"Content {i}",
            tags=["rotation-test"],
            emotional=EmotionalSnapshot(intensity=float(i)),
        )
    return store


def _make_backup_files(backup_dir: Path, dates: list[str]) -> None:
    """Create dummy skmemory backup files for the given dates."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    for d in dates:
        f = backup_dir / f"skmemory-backup-{d}.json"
        f.write_text(
            json.dumps({"skmemory_version": "0.5.0", "memories": []}),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


class TestListBackups:
    def test_empty_dir_returns_empty_list(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        assert backend.list_backups(str(backup_dir)) == []

    def test_nonexistent_dir_returns_empty_list(self, backend, tmp_path):
        assert backend.list_backups(str(tmp_path / "no_such_dir")) == []

    def test_lists_all_backup_files(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(
            backup_dir, ["2026-01-01", "2026-01-02", "2026-01-03"]
        )
        results = backend.list_backups(str(backup_dir))
        assert len(results) == 3

    def test_sorted_newest_first(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(
            backup_dir, ["2026-01-01", "2026-01-03", "2026-01-02"]
        )
        results = backend.list_backups(str(backup_dir))
        dates = [r["date"] for r in results]
        assert dates == ["2026-01-03", "2026-01-02", "2026-01-01"]

    def test_entry_fields(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        f = backup_dir / "skmemory-backup-2026-03-01.json"
        f.write_text('{"test": true}', encoding="utf-8")

        results = backend.list_backups(str(backup_dir))
        assert len(results) == 1
        entry = results[0]
        assert entry["date"] == "2026-03-01"
        assert entry["name"] == "skmemory-backup-2026-03-01.json"
        assert entry["path"] == str(f)
        assert entry["size_bytes"] > 0

    def test_ignores_non_backup_files(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "notes.txt").write_text("not a backup")
        (backup_dir / "skmemory-backup-2026-01-01.json").write_text("{}")

        results = backend.list_backups(str(backup_dir))
        assert len(results) == 1

    def test_store_delegates_to_backend(self, store, tmp_path):
        backup_dir = store.primary.base_path.parent / "backups"
        _make_backup_files(backup_dir, ["2026-01-01", "2026-01-02"])
        results = store.list_backups()
        assert len(results) == 2


# ---------------------------------------------------------------------------
# prune_backups
# ---------------------------------------------------------------------------


class TestPruneBackups:
    def test_prune_keeps_n_most_recent(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(
            backup_dir,
            ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        )
        deleted = backend.prune_backups(keep=3, backup_dir=str(backup_dir))

        assert len(deleted) == 2
        remaining = backend.list_backups(str(backup_dir))
        assert len(remaining) == 3
        assert remaining[0]["date"] == "2026-01-05"
        assert remaining[-1]["date"] == "2026-01-03"

    def test_prune_nothing_when_under_limit(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(backup_dir, ["2026-01-01", "2026-01-02"])
        deleted = backend.prune_backups(keep=7, backup_dir=str(backup_dir))
        assert deleted == []
        assert len(backend.list_backups(str(backup_dir))) == 2

    def test_prune_all_with_keep_zero(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(backup_dir, ["2026-01-01", "2026-01-02"])
        deleted = backend.prune_backups(keep=0, backup_dir=str(backup_dir))
        assert len(deleted) == 2
        assert backend.list_backups(str(backup_dir)) == []

    def test_prune_empty_dir(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        deleted = backend.prune_backups(keep=7, backup_dir=str(backup_dir))
        assert deleted == []

    def test_deleted_files_are_gone(self, backend, tmp_path):
        backup_dir = tmp_path / "backups"
        _make_backup_files(
            backup_dir, ["2026-01-01", "2026-01-02", "2026-01-03"]
        )
        deleted = backend.prune_backups(keep=1, backup_dir=str(backup_dir))
        for path in deleted:
            assert not Path(path).exists()

    def test_store_delegates_to_backend(self, store, tmp_path):
        backup_dir = store.primary.base_path.parent / "backups"
        _make_backup_files(
            backup_dir, ["2026-01-01", "2026-01-02", "2026-01-03"]
        )
        deleted = store.prune_backups(keep=1)
        assert len(deleted) == 2


# ---------------------------------------------------------------------------
# Auto-rotation on export
# ---------------------------------------------------------------------------


class TestAutoRotationOnExport:
    def test_export_auto_prunes_to_7(self, populated_store):
        """Default-path export prunes backup dir to 7 entries."""
        backend = populated_store.primary
        backup_dir = backend.base_path.parent / "backups"

        # Pre-populate with 8 old backups
        old_dates = [f"2025-12-{str(i).zfill(2)}" for i in range(1, 9)]
        _make_backup_files(backup_dir, old_dates)
        assert len(backend.list_backups()) == 8

        # Export using default path → creates today's backup (9th) then prunes to 7
        populated_store.export_backup()

        remaining = backend.list_backups()
        assert len(remaining) <= 7

    def test_export_custom_path_no_auto_prune(self, populated_store, tmp_path):
        """Custom output path must NOT trigger auto-rotation."""
        backend = populated_store.primary
        backup_dir = backend.base_path.parent / "backups"

        old_dates = [f"2025-12-{str(i).zfill(2)}" for i in range(1, 11)]
        _make_backup_files(backup_dir, old_dates)

        custom = tmp_path / "manual_backup.json"
        populated_store.export_backup(str(custom))

        # Backup dir untouched
        remaining = backend.list_backups()
        assert len(remaining) == 10

    def test_export_rotation_leaves_newest(self, populated_store):
        """After auto-rotation the 7 most-recent backups survive."""
        backend = populated_store.primary
        backup_dir = backend.base_path.parent / "backups"

        old_dates = [f"2025-12-{str(i).zfill(2)}" for i in range(1, 9)]
        _make_backup_files(backup_dir, old_dates)

        populated_store.export_backup()

        remaining = backend.list_backups()
        dates = [r["date"] for r in remaining]
        # The oldest of the pre-created set should be gone
        assert "2025-12-01" not in dates


# ---------------------------------------------------------------------------
# CLI: skmemory backup
# ---------------------------------------------------------------------------


class TestBackupCLI:
    """CLI tests for `skmemory backup`."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def cli_store(self, tmp_path):
        """Backend whose paths are injected into the CLI via ctx.obj."""
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        return MemoryStore(primary=backend)

    def _invoke(self, runner, store, args):
        return runner.invoke(
            cli,
            args,
            obj={"store": store},
            catch_exceptions=False,
        )

    def test_list_empty(self, runner, cli_store):
        result = self._invoke(runner, cli_store, ["backup", "--list"])
        assert result.exit_code == 0
        assert "No backups found" in result.output

    def test_list_shows_backups(self, runner, cli_store, tmp_path):
        backup_dir = cli_store.primary.base_path.parent / "backups"
        _make_backup_files(backup_dir, ["2026-01-01", "2026-01-02"])

        result = self._invoke(runner, cli_store, ["backup", "--list"])
        assert result.exit_code == 0
        assert "2026-01-02" in result.output
        assert "2026-01-01" in result.output

    def test_prune_removes_old(self, runner, cli_store):
        backup_dir = cli_store.primary.base_path.parent / "backups"
        _make_backup_files(
            backup_dir,
            ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        )

        result = self._invoke(runner, cli_store, ["backup", "--prune", "3"])
        assert result.exit_code == 0
        assert "Pruned 2 backup(s)" in result.output
        assert len(cli_store.list_backups()) == 3

    def test_prune_nothing_to_prune(self, runner, cli_store):
        backup_dir = cli_store.primary.base_path.parent / "backups"
        _make_backup_files(backup_dir, ["2026-01-01"])

        result = self._invoke(runner, cli_store, ["backup", "--prune", "7"])
        assert result.exit_code == 0
        assert "Nothing to prune" in result.output

    def test_prune_negative_exits_error(self, runner, cli_store):
        result = runner.invoke(
            cli,
            ["backup", "--prune", "-1"],
            obj={"store": cli_store},
        )
        assert result.exit_code != 0

    def test_restore_alias(self, runner, cli_store, tmp_path):
        # Seed store, export to a known path, then restore from it
        for i in range(2):
            cli_store.snapshot(title=f"M{i}", content=f"C{i}")

        backup_path = tmp_path / "manual.json"
        cli_store.export_backup(str(backup_path))

        # Fresh store for restore
        fresh_backend = SQLiteBackend(base_path=str(tmp_path / "fresh"))
        fresh_store = MemoryStore(primary=fresh_backend)

        result = self._invoke(
            runner,
            fresh_store,
            ["backup", "--restore", str(backup_path)],
        )
        assert result.exit_code == 0
        assert "Restored 2 memories" in result.output

    def test_restore_missing_file(self, runner, cli_store):
        result = runner.invoke(
            cli,
            ["backup", "--restore", "/nonexistent/backup.json"],
            obj={"store": cli_store},
        )
        assert result.exit_code != 0

    def test_no_option_shows_help(self, runner, cli_store):
        result = self._invoke(runner, cli_store, ["backup"])
        assert result.exit_code == 0
        assert "--list" in result.output
