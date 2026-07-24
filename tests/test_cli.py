"""Tests for the SKMemory CLI."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skmemory import __version__
from skmemory.cli import _get_store, cli
from skmemory.config import SKMemoryConfig


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
        for group in ["soul", "journal", "anchor", "lovenote", "steelman", "graph", "corpora"]:
            result = runner.invoke(cli, [group, "--help"])
            assert result.exit_code == 0, f"{group} --help failed"


class TestCLIGlobalOptions:
    """Global option tests."""

    def test_skvector_url_option_exists(self, runner):
        """--skvector-url option is accepted."""
        result = runner.invoke(cli, ["--help"])
        assert "--skvector-url" in result.output

    def test_skvector_key_option_exists(self, runner):
        """--skvector-key option is accepted."""
        result = runner.invoke(cli, ["--help"])
        assert "--skvector-key" in result.output

    def test_ingest_file_command_exists(self, runner):
        """The document ingest command is exposed."""
        result = runner.invoke(cli, ["--help"])
        assert "ingest-file" in result.output

    def test_graph_subcommands_exist(self, runner):
        """The decomposition-aware graph query commands are exposed."""
        result = runner.invoke(cli, ["graph", "--help"])
        assert "entity" in result.output
        assert "citation" in result.output
        assert "claim" in result.output
        assert "section" in result.output
        assert "around" in result.output
        assert "related-claims" in result.output
        result = runner.invoke(cli, ["task-pack", "--help"])
        assert "create" in result.output
        assert "show" in result.output
        result = runner.invoke(cli, ["--help"])
        assert "novelty" in result.output
        assert "session-brief" in result.output


class TestCLIGraphCommands:
    """Graph command behavior."""

    def test_graph_entity_emits_collapsed_results(self, runner):
        graph = MagicMock()
        graph.search_by_entity.return_value = [
            {
                "id": "mem-001",
                "title": "IRS Notice",
                "layer": "mid-term",
                "intensity": 7.0,
                "matched_values": ["Internal Revenue Service"],
                "source_memory_ids": ["chunk-001", "chunk-002"],
                "match_count": 2,
                "chunk_match_count": 2,
            }
        ]
        store = SimpleNamespace(graph=graph)

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["graph", "entity", "Revenue"])

        assert result.exit_code == 0
        assert '"match_count": 2' in result.output
        graph.search_by_entity.assert_called_once_with("Revenue", limit=10)

    def test_graph_around_calls_get_related(self, runner):
        graph = MagicMock()
        graph.get_related.return_value = [{"id": "mem-002", "distance": 1}]
        store = SimpleNamespace(graph=graph)

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["graph", "around", "mem-001", "--depth", "3"])

        assert result.exit_code == 0
        graph.get_related.assert_called_once_with("mem-001", depth=3)

    def test_graph_related_claims_requires_single_pivot(self, runner):
        graph = MagicMock()
        store = SimpleNamespace(graph=graph)

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(
                cli,
                ["graph", "related-claims", "--entity", "IRS", "--citation", "26 U.S.C. § 6903"],
            )

        assert result.exit_code != 0
        assert "Provide exactly one" in result.output

    def test_graph_related_claims_by_entity(self, runner):
        graph = MagicMock()
        graph.related_claims_by_entity.return_value = [{"claim": "Respond", "support_count": 2}]
        store = SimpleNamespace(graph=graph)

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["graph", "related-claims", "--entity", "IRS"])

        assert result.exit_code == 0
        graph.related_claims_by_entity.assert_called_once_with("IRS", limit=10)

    def test_graph_related_claims_by_citation(self, runner):
        graph = MagicMock()
        graph.related_claims_by_citation.return_value = [
            {"claim": "Holder rights", "support_count": 1}
        ]
        store = SimpleNamespace(graph=graph)

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["graph", "related-claims", "--citation", "UCC § 3-301"])

        assert result.exit_code == 0
        graph.related_claims_by_citation.assert_called_once_with("UCC § 3-301", limit=10)


class TestCLICorpora:
    """Shared corpus registry CLI behavior."""

    def test_corpora_status_emits_registry_report(self, runner):
        report = {
            "agent": "jarvis",
            "local": {"primary_vector_collection": "jarvis-memory"},
            "shared_corpora": [
                {
                    "name": "hammertime",
                    "vector_collection": "hammertime-v3",
                    "graph_name": "hammertime-v4",
                }
            ],
        }
        with patch(
            "skmemory.corpus_registry.build_corpus_registry_report", return_value=report
        ) as mock_report:
            result = runner.invoke(
                cli,
                ["corpora", "status", "--name", "hammertime"],
                env={"SKMEMORY_AGENT": "jarvis"},
            )

        assert result.exit_code == 0
        mock_report.assert_called_once_with(agent="jarvis", names=["hammertime"])
        assert "hammertime-v4" in result.output


class TestBackendConfigRouting:
    """Backend configuration should propagate collection and graph names."""

    def test_get_store_passes_graph_name_from_config(self):
        """Configured SKGraph graph_name should be forwarded to the backend."""
        cfg = SKMemoryConfig(
            skgraph_url="redis://graph.example:6379",
            skgraph_graph_name="aster-memory",
        )

        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch(
                "skmemory.config.merge_env_and_config", return_value=(None, None, cfg.skgraph_url)
            ),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.skgraph_backend.SKGraphBackend") as mock_graph_backend,
        ):
            _get_store()

        mock_graph_backend.assert_called_once_with(
            url="redis://graph.example:6379",
            graph_name="aster-memory",
        )

    def test_get_store_wires_age_graph_when_pgvector_and_no_skgraph(self, monkeypatch):
        """Gap B (card dc8280a7): on the default skmem-pg deployment (pgvector
        enabled, no FalkorDB SKGraph URL), _get_store() must wire the AGE graph
        into the graph role so forget() cascades a DETACH DELETE to the AGE
        <agent>_knowledge graph.

        Fail-before: the graph role stayed None (AGE was never wired in
        _get_store), so forget() orphaned the AGE Memory node.
        """
        monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://u:p@node/skmemory")
        cfg = SKMemoryConfig(
            backends_enabled=["pgvector"],
            pgvector_dsn="postgresql://ignored/db",
        )
        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch("skmemory.config.merge_env_and_config", return_value=(None, None, None)),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.pgvector_backend.PGVectorBackend"),
        ):
            store = _get_store()

        from skmemory.backends.age_backend import AGEGraphBackend

        assert isinstance(store.graph, AGEGraphBackend)
        # DSN precedence: node-local SKMEMORY_PG_DSN env wins over cfg.pgvector_dsn.
        assert store.graph.dsn == "postgresql://u:p@node/skmemory"

    def test_get_store_age_dsn_falls_back_to_cfg_pgvector_dsn(self, monkeypatch):
        """When SKMEMORY_PG_DSN is unset, the AGE graph uses cfg.pgvector_dsn."""
        monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
        cfg = SKMemoryConfig(
            backends_enabled=["pgvector"],
            pgvector_dsn="postgresql://cfg-host/skmemory",
        )
        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch("skmemory.config.merge_env_and_config", return_value=(None, None, None)),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.pgvector_backend.PGVectorBackend"),
        ):
            store = _get_store()

        assert store.graph.dsn == "postgresql://cfg-host/skmemory"

    def test_get_store_falkordb_skgraph_not_clobbered_by_age(self, monkeypatch):
        """A configured FalkorDB SKGraph keeps the graph role; AGE does not
        displace it (AGE only fills an otherwise-empty graph role)."""
        monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://u:p@node/skmemory")
        cfg = SKMemoryConfig(
            backends_enabled=["pgvector"],
            skgraph_url="redis://graph.example:6379",
            skgraph_graph_name="aster-memory",
        )
        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch(
                "skmemory.config.merge_env_and_config",
                return_value=(None, None, cfg.skgraph_url),
            ),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.pgvector_backend.PGVectorBackend"),
            patch("skmemory.backends.skgraph_backend.SKGraphBackend") as mock_skgraph,
        ):
            store = _get_store()

        # graph role holds the FalkorDB SKGraph mock, not an AGEGraphBackend.
        assert store.graph is mock_skgraph.return_value

    def test_get_store_age_wiring_degrades_when_construction_fails(self, monkeypatch):
        """If AGE construction raises, the graph role stays None and _get_store
        still returns a usable store (forget() must not hard-fail)."""
        monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://u:p@node/skmemory")
        cfg = SKMemoryConfig(backends_enabled=["pgvector"])
        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch("skmemory.config.merge_env_and_config", return_value=(None, None, None)),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.pgvector_backend.PGVectorBackend"),
            patch(
                "skmemory.backends.age_backend.AGEGraphBackend",
                side_effect=RuntimeError("skmem-pg unreachable"),
            ),
        ):
            store = _get_store()

        assert store.graph is None

    def test_get_store_passes_vector_collection_from_config(self):
        """Configured SKVector collection should be forwarded to the backend."""
        cfg = SKMemoryConfig(
            skvector_url="https://vector.example",
            skvector_collection="aster-memory",
        )

        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch(
                "skmemory.config.merge_env_and_config", return_value=(cfg.skvector_url, None, None)
            ),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.skvector_backend.SKVectorBackend") as mock_vector_backend,
        ):
            _get_store()

        mock_vector_backend.assert_called_once()
        _, kwargs = mock_vector_backend.call_args
        assert kwargs["url"] == "https://vector.example"
        assert kwargs["collection"] == "aster-memory"

    def test_get_store_passes_embedding_model_from_config(self):
        """Configured SKVector embedding settings should be forwarded."""
        cfg = SKMemoryConfig(
            skvector_url="https://vector.example",
            skvector_collection="aster-memory",
            skvector_embedding_model="mxbai-embed-large",
            skvector_vector_dim=1024,
        )

        with (
            patch("skmemory.config.load_config", return_value=cfg),
            patch(
                "skmemory.config.merge_env_and_config",
                return_value=(
                    cfg.skvector_url,
                    None,
                    None,
                    cfg.skvector_embedding_model,
                    cfg.skvector_vector_dim,
                ),
            ),
            patch(
                "skmemory.config.build_endpoint_list",
                side_effect=lambda single_url, endpoints, default_role="primary": [],
            ),
            patch("skmemory.backends.skvector_backend.SKVectorBackend") as mock_vector_backend,
        ):
            _get_store()

        _, kwargs = mock_vector_backend.call_args
        assert kwargs["embedding_model"] == "mxbai-embed-large"
        assert kwargs["vector_dim"] == 1024


class TestCLINoveltyAndBriefing:
    """Novelty, task-pack, and session-brief behavior."""

    def test_novelty_command_uses_store(self, runner):
        store = SimpleNamespace()
        store.novelty_search = MagicMock(
            return_value=[{"title": "Rare claim", "novelty_score": 2.5}]
        )

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["novelty", "rare claim"])

        assert result.exit_code == 0
        store.novelty_search.assert_called_once_with("rare claim", limit=8)
        assert "Rare claim" in result.output

    def test_task_pack_create_uses_store(self, runner):
        store = SimpleNamespace()
        store.create_task_pack = MagicMock(
            return_value=SimpleNamespace(
                id="pack-1", title="Task Pack: Levy", metadata={"task_pack": {"task": "Levy"}}
            )
        )

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(
                cli, ["task-pack", "create", "Levy", "--query", "writ of execution"]
            )

        assert result.exit_code == 0
        store.create_task_pack.assert_called_once()
        assert '"id": "pack-1"' in result.output

    def test_session_brief_uses_store(self, runner):
        store = SimpleNamespace()
        store.build_session_brief = MagicMock(
            return_value={
                "task": "Judgment defense",
                "facts": [],
                "missing_facts": [],
                "authority_summary": {"statute": 1},
            }
        )

        with patch("skmemory.cli._get_store", return_value=store):
            result = runner.invoke(cli, ["session-brief", "Judgment defense"])

        assert result.exit_code == 0
        store.build_session_brief.assert_called_once_with("Judgment defense", limit=6)
        assert '"authority_summary"' in result.output
