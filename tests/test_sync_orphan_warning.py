"""`skmemory sync` must be LOUD when it rescues SQLite-only orphans.

Card: sqlite-only orphans (2026-10-16). export_orphans_to_flat() is the
safety net that materializes index-only rows into flat files (lossy: it only
has the ~150-char content_preview, not full content). Before this change,
a sync that quietly rescued thousands of orphans printed an ordinary
click.echo info line (suppressed entirely under --quiet unless *other* stats
changed) -- exactly how 23,525 rows went unnoticed on a laptop for months.
A nonzero rescue must now log a WARNING with the counts, every time,
regardless of --quiet.
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


def _fake_store(exported: int, errors: int = 0):
    store = MagicMock()
    store.export_orphans_to_flat.return_value = {
        "exported": exported,
        "errors": errors,
        "skipped": 0,
        "orphan_ids": [f"id-{i}" for i in range(exported)],
    }
    store.reindex.return_value = exported
    return store


class TestSyncOrphanWarning:
    def test_nonzero_rescue_logs_warning_with_counts(self, runner, agent_paths, caplog):
        store = _fake_store(exported=23525, errors=3)
        import logging

        with caplog.at_level(logging.WARNING, logger="skmemory.cli"):
            result = runner.invoke(cli, ["sync"], obj={"store": store})

        assert result.exit_code == 0, result.output
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "expected a WARNING when sync materializes sqlite-only rows"
        joined = " ".join(r.getMessage() for r in warnings)
        assert "23525" in joined or "23,525" in joined

    def test_zero_rescue_logs_no_warning(self, runner, agent_paths, caplog):
        store = _fake_store(exported=0)
        import logging

        with caplog.at_level(logging.WARNING, logger="skmemory.cli"):
            result = runner.invoke(cli, ["sync", "--quiet"], obj={"store": store})

        assert result.exit_code == 0, result.output
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_quiet_still_warns_on_nonzero_rescue(self, runner, agent_paths, caplog):
        """--quiet silences the normal info echo but must NOT silence the
        warning -- that's the whole point of this being a safety net."""
        store = _fake_store(exported=42)
        import logging

        with caplog.at_level(logging.WARNING, logger="skmemory.cli"):
            result = runner.invoke(cli, ["sync", "--quiet"], obj={"store": store})

        assert result.exit_code == 0, result.output
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings
