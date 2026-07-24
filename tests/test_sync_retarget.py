"""Retarget of `skmemory sync` onto the live stack (card 162a19eb).

`sync --vector` must reconcile flat files into skmem-pg (pgvector) via the
vendored `skmemory.reconcile` engine, and `sync --graph` must backfill the
Apache AGE knowledge graph — NOT the retired ChromaDB / FalkorDB backends.

These are unit-level integration tests: the Postgres-touching engines
(`skmemory.reconcile.reconcile` and `AGEGraphBackend.sync_all`) are mocked so
the test asserts the CLI wires the *new* targets and no longer imports the dead
ones, without needing a live container.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skmemory.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def agent_paths(tmp_path):
    """Patch get_agent_paths to a throwaway 'lumina' home with a memory dir."""
    base = tmp_path / "lumina"
    (base / "memory").mkdir(parents=True)
    paths = {"base": base, "config": base / "config", "memory": base / "memory"}
    with patch("skmemory.agents.get_agent_paths", return_value=paths):
        yield paths


@pytest.fixture
def fake_store():
    store = MagicMock()
    store.export_orphans_to_flat.return_value = {"exported": 0, "errors": 0}
    store.reindex.return_value = 5
    return store


def _pg_stats(**over):
    base = {
        "agent": "lumina",
        "flat": 10,
        "pg": 10,
        "missing": 2,
        "backfilled": 2,
        "pruned": 0,
        "prune_skipped": False,
        "prune_reason": "ok",
        "null_embedded": 0,
        "embedded": 10,
        "total": 10,
    }
    base.update(over)
    return base


class TestSyncRetarget:
    def test_vector_calls_reconcile_engine_not_chroma(self, runner, agent_paths, fake_store):
        """--vector delegates to skmemory.reconcile.reconcile (pgvector), not Chroma."""
        with (
            patch("skmemory.reconcile.reconcile", return_value=_pg_stats()) as recon,
            patch("skmemory.config.load_config", return_value=None),
        ):
            result = runner.invoke(cli, ["sync", "--vector"], obj={"store": fake_store})
        assert result.exit_code == 0, result.output
        recon.assert_called_once()
        # agent positional is the resolved home name
        assert recon.call_args.args[0] == "lumina"
        assert "pg_backfilled=2" in result.output
        assert "chroma" not in result.output.lower()

    def test_graph_calls_age_backend_not_falkordb(self, runner, agent_paths, fake_store):
        """--graph backfills the AGE knowledge graph, not FalkorDB/SKGraph."""
        age_inst = MagicMock()
        age_inst.sync_all.return_value = {"indexed": 3, "errors": 0}
        with (
            patch("skmemory.backends.age_backend.AGEGraphBackend", return_value=age_inst) as age_cls,
            patch("skmemory.config.load_config", return_value=None),
        ):
            result = runner.invoke(cli, ["sync", "--graph"], obj={"store": fake_store})
        assert result.exit_code == 0, result.output
        age_cls.assert_called_once()
        # constructed with the resolved agent name
        assert age_cls.call_args.kwargs.get("agent") == "lumina"
        age_inst.sync_all.assert_called_once()
        assert "graph_indexed=3" in result.output

    def test_vector_passes_unified_config_embed_settings(self, runner, agent_paths, fake_store):
        """embed_url/embed_model from unified config are forwarded to reconcile."""
        cfg = SimpleNamespace(
            embed_url="http://192.168.0.100:11434/api/embed",
            embed_model="mxbai-embed-large",
            pgvector_dsn=None,
        )
        with (
            patch("skmemory.reconcile.reconcile", return_value=_pg_stats()) as recon,
            patch("skmemory.config.load_config", return_value=cfg),
        ):
            result = runner.invoke(cli, ["sync", "--vector"], obj={"store": fake_store})
        assert result.exit_code == 0, result.output
        kwargs = recon.call_args.kwargs
        assert kwargs["embed_url"] == "http://192.168.0.100:11434/api/embed"
        assert kwargs["embed_model"] == "mxbai-embed-large"

    def test_graph_honors_pg_dsn_env_over_yaml(
        self, runner, agent_paths, fake_store, monkeypatch
    ):
        """SKMEMORY_PG_DSN env wins over the (Syncthing-shared) yaml DSN."""
        monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://postgres:x@10.0.0.9:5432/skmemory")
        cfg = SimpleNamespace(
            embed_url=None,
            embed_model=None,
            pgvector_dsn="postgresql://postgres:x@localhost:5432/skmemory",
        )
        age_inst = MagicMock()
        age_inst.sync_all.return_value = {"indexed": 0, "errors": 0}
        with (
            patch("skmemory.backends.age_backend.AGEGraphBackend", return_value=age_inst) as age_cls,
            patch("skmemory.config.load_config", return_value=cfg),
        ):
            result = runner.invoke(cli, ["sync", "--graph"], obj={"store": fake_store})
        assert result.exit_code == 0, result.output
        assert age_cls.call_args.kwargs.get("dsn") == "postgresql://postgres:x@10.0.0.9:5432/skmemory"

    def test_quiet_no_output_when_nothing_changed(self, runner, agent_paths, fake_store):
        """--quiet stays silent on a clean idempotent run (cron-friendly)."""
        with (
            patch("skmemory.reconcile.reconcile", return_value=_pg_stats(backfilled=0)),
            patch("skmemory.config.load_config", return_value=None),
        ):
            result = runner.invoke(
                cli, ["sync", "--quiet", "--vector"], obj={"store": fake_store}
            )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == ""

    def test_reconcile_failure_is_non_fatal(self, runner, agent_paths, fake_store):
        """A pgvector reconcile error must not crash the timer run."""
        with (
            patch("skmemory.reconcile.reconcile", side_effect=RuntimeError("embed failed")),
            patch("skmemory.config.load_config", return_value=None),
        ):
            result = runner.invoke(cli, ["sync", "--vector"], obj={"store": fake_store})
        assert result.exit_code == 0, result.output
        assert "pgvector reconcile failed" in result.output


class TestSyncHelpText:
    def test_help_mentions_pgvector_and_age(self, runner):
        result = runner.invoke(cli, ["sync", "--help"])
        assert result.exit_code == 0
        low = result.output.lower()
        assert "pgvector" in low or "skmem-pg" in low
        assert "age" in low

    def test_help_drops_retired_backends_as_targets(self, runner):
        """The old option help ('re-sync ... into ChromaDB/FalkorDB') is gone.

        The docstring still *names* the retired backends in the removal note,
        so we assert the specific old target phrasing is absent rather than the
        bare product names.
        """
        result = runner.invoke(cli, ["sync", "--help"])
        assert result.exit_code == 0
        low = result.output.lower()
        assert "into chromadb" not in low
        assert "into falkordb" not in low
        assert "removed" in low  # deprecation note present
