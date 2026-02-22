"""Tests for the SKMemory CLI."""

import pytest
from click.testing import CliRunner

from skmemory import __version__
from skmemory.cli import cli


@pytest.fixture
def runner():
    """Create a CLI test runner."""
    return CliRunner()


class TestCLIVersion:
    """Version flag consistency tests."""

    def test_version_flag(self, runner):
        """--version prints version string with prog name."""
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "skmemory" in result.output
        assert __version__ in result.output

    def test_version_flag_format(self, runner):
        """Version output follows 'prog_name, version X.Y.Z' pattern."""
        result = runner.invoke(cli, ["--version"])
        assert f"skmemory, version {__version__}" in result.output


class TestCLIHelp:
    """Help text tests."""

    def test_help_flag(self, runner):
        """--help shows usage information."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "SKMemory" in result.output

    def test_subcommand_help(self, runner):
        """Subcommands have help text."""
        for cmd in ["snapshot", "recall", "search", "list", "health"]:
            result = runner.invoke(cli, [cmd, "--help"])
            assert result.exit_code == 0, f"{cmd} --help failed"

    def test_subgroup_help(self, runner):
        """Subgroups have help text."""
        for group in ["soul", "journal", "anchor", "lovenote", "steelman"]:
            result = runner.invoke(cli, [group, "--help"])
            assert result.exit_code == 0, f"{group} --help failed"


class TestCLIGlobalOptions:
    """Global option tests."""

    def test_qdrant_url_option_exists(self, runner):
        """--qdrant-url option is accepted."""
        result = runner.invoke(cli, ["--help"])
        assert "--qdrant-url" in result.output

    def test_qdrant_key_option_exists(self, runner):
        """--qdrant-key option is accepted."""
        result = runner.invoke(cli, ["--help"])
        assert "--qdrant-key" in result.output
