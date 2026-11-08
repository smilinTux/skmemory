"""`skmemory sync` must report refused resurrections in its summary line.

export_orphans_to_flat() counts, as ``tombstoned``, every orphan it refused to
write back because a tombstone says the memory was deliberately forgotten
(PR #107). The sync summary used to print ``exported=`` and ``orphan_errors=``
but never that count, so a peer index trying to resurrect a deleted memory was
invisible in the log even though the stats dict carried it. The timer runs
``sync --quiet``, so a nonzero count must also make --quiet speak: putting it
only in a line that --quiet suppresses would hide it just as well.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skmemory.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def agent_paths(tmp_path):
    base = tmp_path / "lumina"
    (base / "memory").mkdir(parents=True)
    paths = {"base": base, "config": base / "config", "memory": base / "memory"}
    with patch("skmemory.agents.get_agent_paths", return_value=paths):
        yield paths


def _fake_store(tombstoned=None):
    store = MagicMock()
    stats = {"exported": 0, "errors": 0, "skipped": 0, "orphan_ids": []}
    if tombstoned is not None:
        stats["tombstoned"] = tombstoned
    store.export_orphans_to_flat.return_value = stats
    store.reindex.return_value = 10
    return store


def _report_line(output: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.startswith("sync[")]
    assert len(lines) == 1, output
    return lines[0]


class TestSyncReportTombstoned:
    def test_report_line_carries_tombstoned_beside_orphan_errors(self, runner, agent_paths):
        result = runner.invoke(cli, ["sync"], obj={"store": _fake_store(tombstoned=7)})

        assert result.exit_code == 0, result.output
        line = _report_line(result.output)
        assert "orphan_errors=0 tombstoned=7" in line
        assert "exported=0" in line

    def test_quiet_speaks_when_a_resurrection_was_refused(self, runner, agent_paths):
        result = runner.invoke(cli, ["sync", "--quiet"], obj={"store": _fake_store(tombstoned=3)})

        assert result.exit_code == 0, result.output
        assert "tombstoned=3" in _report_line(result.output)

    def test_quiet_stays_silent_when_nothing_was_refused(self, runner, agent_paths):
        result = runner.invoke(cli, ["sync", "--quiet"], obj={"store": _fake_store(tombstoned=0)})

        assert result.exit_code == 0, result.output
        assert "sync[" not in result.output

    def test_store_without_the_key_reports_zero(self, runner, agent_paths):
        """An older store (pre #107) returns no ``tombstoned`` key; that is
        zero refusals, not a crash."""
        result = runner.invoke(cli, ["sync"], obj={"store": _fake_store()})

        assert result.exit_code == 0, result.output
        assert "tombstoned=0" in _report_line(result.output)
