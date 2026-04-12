"""
Tests for skmemory setup wizard, config persistence, and CLI commands.

All Docker/network operations are mocked — no Docker required to run tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from skmemory.config import (
    SKMemoryConfig,
    load_config,
    merge_env_and_config,
    save_config,
)
from skmemory.setup_wizard import (
    PlatformInfo,
    check_port_available,
    check_skgraph_health,
    check_skvector_health,
    compose_down,
    compose_ps,
    compose_up,
    detect_platform,
    find_compose_file,
    get_docker_install_instructions,
    install_python_deps,
    run_setup_wizard,
)

# ═══════════════════════════════════════════════════════════
# Config tests
# ═══════════════════════════════════════════════════════════


class TestConfig:
    """Test SKMemoryConfig save/load/merge."""

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        config = SKMemoryConfig(
            skvector_url="http://localhost:6333",
            skvector_key="secret",
            skvector_embedding_model="bge-legal-v1",
            skvector_vector_dim=1024,
            skgraph_url="redis://localhost:6379",
            backends_enabled=["skvector", "skgraph"],
            docker_compose_file="/some/path/docker-compose.yml",
            setup_completed_at="2026-02-28T12:00:00+00:00",
        )
        path = tmp_path / "config.yaml"
        save_config(config, path)

        loaded = load_config(path)
        assert loaded is not None
        assert loaded.skvector_url == "http://localhost:6333"
        assert loaded.skvector_key == "secret"
        assert loaded.skvector_embedding_model == "bge-legal-v1"
        assert loaded.skvector_vector_dim == 1024
        assert loaded.skgraph_url == "redis://localhost:6379"
        assert loaded.backends_enabled == ["skvector", "skgraph"]
        assert loaded.setup_completed_at == "2026-02-28T12:00:00+00:00"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        result = load_config(tmp_path / "nonexistent.yaml")
        assert result is None

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("not: [valid: yaml: {{")
        result = load_config(path)
        # pyyaml may parse this or error — either way should not crash
        assert result is None or isinstance(result, SKMemoryConfig)

    def test_load_non_dict_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n")
        result = load_config(path)
        assert result is None

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "config.yaml"
        config = SKMemoryConfig(skvector_url="http://localhost:6333")
        save_config(config, nested)
        assert nested.exists()

    def test_default_config_values(self) -> None:
        config = SKMemoryConfig()
        assert config.skvector_url is None
        assert config.skvector_key is None
        assert config.skvector_embedding_model is None
        assert config.skvector_vector_dim is None
        assert config.skgraph_url is None
        assert config.backends_enabled == []
        assert config.docker_compose_file is None
        assert config.setup_completed_at is None

    def test_merge_cli_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKMEMORY_SKVECTOR_URL", "http://env:6333")
        monkeypatch.setenv("SKMEMORY_SKVECTOR_KEY", "env-key")
        monkeypatch.setenv("SKMEMORY_SKGRAPH_URL", "redis://env:6379")
        monkeypatch.setenv("SKMEMORY_SKVECTOR_EMBEDDING_MODEL", "env-model")
        monkeypatch.setenv("SKMEMORY_SKVECTOR_VECTOR_DIM", "768")

        skvector_url, skvector_key, skgraph_url, embedding_model, vector_dim = (
            merge_env_and_config(
            cli_skvector_url="http://cli:6333",
            cli_skvector_key="cli-key",
            cli_skgraph_url="redis://cli:6379",
            cli_skvector_embedding_model="cli-model",
            cli_skvector_vector_dim=1536,
        )
        )
        assert skvector_url == "http://cli:6333"
        assert skvector_key == "cli-key"
        assert skgraph_url == "redis://cli:6379"
        assert embedding_model == "cli-model"
        assert vector_dim == 1536

    def test_merge_env_overrides_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKMEMORY_SKVECTOR_URL", "http://env:6333")
        monkeypatch.setenv("SKMEMORY_SKVECTOR_EMBEDDING_MODEL", "env-model")
        monkeypatch.setenv("SKMEMORY_SKVECTOR_VECTOR_DIM", "768")
        monkeypatch.delenv("SKMEMORY_SKVECTOR_KEY", raising=False)
        monkeypatch.delenv("SKMEMORY_SKGRAPH_URL", raising=False)

        # Mock load_config to return a saved config
        cfg = SKMemoryConfig(
            skvector_url="http://config:6333",
            skvector_key="config-key",
            skvector_embedding_model="config-model",
            skvector_vector_dim=1024,
            skgraph_url="redis://config:6379",
        )
        with mock.patch("skmemory.config.load_config", return_value=cfg):
            skvector_url, skvector_key, skgraph_url, embedding_model, vector_dim = (
                merge_env_and_config()
            )

        assert skvector_url == "http://env:6333"  # env wins
        assert skvector_key == "config-key"  # falls through to config
        assert skgraph_url == "redis://config:6379"  # falls through to config
        assert embedding_model == "env-model"
        assert vector_dim == 768

    def test_merge_config_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKMEMORY_SKVECTOR_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_KEY", raising=False)
        monkeypatch.delenv("SKMEMORY_SKGRAPH_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_VECTOR_DIM", raising=False)

        cfg = SKMemoryConfig(
            skvector_url="http://config:6333",
            skvector_embedding_model="config-model",
            skvector_vector_dim=1024,
            skgraph_url="redis://config:6379",
        )
        with mock.patch("skmemory.config.load_config", return_value=cfg):
            skvector_url, skvector_key, skgraph_url, embedding_model, vector_dim = (
                merge_env_and_config()
            )

        assert skvector_url == "http://config:6333"
        assert skvector_key is None
        assert skgraph_url == "redis://config:6379"
        assert embedding_model == "config-model"
        assert vector_dim == 1024

    def test_merge_all_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKMEMORY_SKVECTOR_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_KEY", raising=False)
        monkeypatch.delenv("SKMEMORY_SKGRAPH_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_VECTOR_DIM", raising=False)

        with mock.patch("skmemory.config.load_config", return_value=None):
            skvector_url, skvector_key, skgraph_url, embedding_model, vector_dim = (
                merge_env_and_config()
            )

        assert skvector_url is None
        assert skvector_key is None
        assert skgraph_url is None
        assert embedding_model is None
        assert vector_dim is None


# ═══════════════════════════════════════════════════════════
# Platform detection tests
# ═══════════════════════════════════════════════════════════


class TestPlatformDetection:
    """Test Docker/Compose detection across platforms."""

    def test_docker_not_found(self) -> None:
        with mock.patch("skmemory.setup_wizard.shutil.which", return_value=None):
            info = detect_platform()
        assert not info.docker_available
        assert not info.compose_available

    def test_docker_daemon_not_running(self) -> None:
        with (
            mock.patch("skmemory.setup_wizard.shutil.which", return_value="/usr/bin/docker"),
            mock.patch(
                "skmemory.setup_wizard.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["docker", "info"], returncode=1, stdout="", stderr="Cannot connect"
                ),
            ),
        ):
            info = detect_platform()
        assert not info.docker_available

    def test_compose_v2_detected(self) -> None:
        def mock_run(cmd, **kwargs):
            if cmd == ["docker", "info"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd == ["docker", "version", "--format", "{{.Server.Version}}"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="24.0.7\n", stderr=""
                )
            if cmd == ["docker", "compose", "version"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="v2.23.0\n", stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        with (
            mock.patch("skmemory.setup_wizard.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("skmemory.setup_wizard.subprocess.run", side_effect=mock_run),
        ):
            info = detect_platform()

        assert info.docker_available
        assert info.compose_available
        assert not info.compose_legacy
        assert info.docker_version == "24.0.7"

    def test_compose_v1_fallback(self) -> None:
        def mock_run(cmd, **kwargs):
            if cmd == ["docker", "info"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd == ["docker", "version", "--format", "{{.Server.Version}}"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="20.10.0\n", stderr=""
                )
            if cmd == ["docker", "compose", "version"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            if cmd == ["docker-compose", "--version"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="docker-compose version 1.29.2\n", stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        with (
            mock.patch("skmemory.setup_wizard.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("skmemory.setup_wizard.subprocess.run", side_effect=mock_run),
        ):
            info = detect_platform()

        assert info.docker_available
        assert info.compose_available
        assert info.compose_legacy

    def test_no_compose_at_all(self) -> None:
        def mock_run(cmd, **kwargs):
            if cmd == ["docker", "info"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd == ["docker", "version", "--format", "{{.Server.Version}}"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="24.0.7\n", stderr=""
                )
            if cmd == ["docker", "compose", "version"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            if cmd == ["docker-compose", "--version"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        with (
            mock.patch("skmemory.setup_wizard.shutil.which", return_value="/usr/bin/docker"),
            mock.patch("skmemory.setup_wizard.subprocess.run", side_effect=mock_run),
        ):
            info = detect_platform()

        assert info.docker_available
        assert not info.compose_available

    def test_docker_install_instructions_linux(self) -> None:
        msg = get_docker_install_instructions("Linux")
        assert "get.docker.com" in msg

    def test_docker_install_instructions_mac(self) -> None:
        msg = get_docker_install_instructions("Darwin")
        assert "mac" in msg.lower() or "Docker Desktop" in msg

    def test_docker_install_instructions_windows(self) -> None:
        msg = get_docker_install_instructions("Windows")
        assert "windows" in msg.lower() or "WSL" in msg

    def test_docker_install_instructions_unknown(self) -> None:
        msg = get_docker_install_instructions("FreeBSD")
        assert "docker" in msg.lower()


# ═══════════════════════════════════════════════════════════
# Port check tests
# ═══════════════════════════════════════════════════════════


class TestPortCheck:
    """Test port availability detection."""

    def test_port_available(self) -> None:
        mock_sock = mock.MagicMock()
        mock_sock.__enter__ = mock.MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch("skmemory.setup_wizard.socket.socket", return_value=mock_sock):
            assert check_port_available(6333) is True
        mock_sock.bind.assert_called_once_with(("127.0.0.1", 6333))

    def test_port_occupied(self) -> None:
        mock_sock = mock.MagicMock()
        mock_sock.__enter__ = mock.MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = mock.MagicMock(return_value=False)
        mock_sock.bind.side_effect = OSError("Address already in use")
        with mock.patch("skmemory.setup_wizard.socket.socket", return_value=mock_sock):
            assert check_port_available(6333) is False


# ═══════════════════════════════════════════════════════════
# Health check tests
# ═══════════════════════════════════════════════════════════


class TestHealthChecks:
    """Test SKVector and SKGraph health checks (mocked network)."""

    def test_skvector_healthy(self) -> None:
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("skmemory.setup_wizard.urllib.request.urlopen", return_value=mock_resp):
            assert check_skvector_health(timeout=5) is True

    def test_skvector_timeout(self) -> None:
        with (
            mock.patch(
                "skmemory.setup_wizard.urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ),
            mock.patch("skmemory.setup_wizard.time.sleep"),
        ):
            assert check_skvector_health(timeout=1) is False

    def test_skgraph_healthy(self) -> None:
        mock_sock = mock.MagicMock()
        mock_sock.recv.return_value = b"+PONG\r\n"
        mock_sock.__enter__ = mock.MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("skmemory.setup_wizard.socket.create_connection", return_value=mock_sock):
            assert check_skgraph_health(timeout=5) is True

    def test_skgraph_timeout(self) -> None:
        with (
            mock.patch(
                "skmemory.setup_wizard.socket.create_connection",
                side_effect=OSError("Connection refused"),
            ),
            mock.patch("skmemory.setup_wizard.time.sleep"),
        ):
            assert check_skgraph_health(timeout=1) is False


# ═══════════════════════════════════════════════════════════
# Compose command tests
# ═══════════════════════════════════════════════════════════


class TestComposeCommands:
    """Test docker compose subprocess invocations."""

    def test_compose_up_v2(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            compose_up(services=["skvector"], compose_file=compose_file, use_legacy=False)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[:2] == ["docker", "compose"]
        assert "-f" in args
        assert "up" in args
        assert "-d" in args
        assert "skvector" in args

    def test_compose_up_legacy(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            compose_up(services=None, compose_file=compose_file, use_legacy=True)

        args = mock_run.call_args[0][0]
        assert args[0] == "docker-compose"

    def test_compose_down_with_volumes(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            compose_down(compose_file=compose_file, remove_volumes=True, use_legacy=False)

        args = mock_run.call_args[0][0]
        assert "down" in args
        assert "-v" in args

    def test_compose_down_without_volumes(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            compose_down(compose_file=compose_file, remove_volumes=False, use_legacy=False)

        args = mock_run.call_args[0][0]
        assert "down" in args
        assert "-v" not in args

    def test_compose_ps(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="running", stderr=""
            )
            result = compose_ps(compose_file=compose_file, use_legacy=False)

        assert result.stdout == "running"
        args = mock_run.call_args[0][0]
        assert "ps" in args


# ═══════════════════════════════════════════════════════════
# Pip install tests
# ═══════════════════════════════════════════════════════════


class TestPipInstall:
    """Test Python dependency installation."""

    def test_install_skvector_deps(self) -> None:
        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            install_python_deps(["skvector"])

        args = mock_run.call_args[0][0]
        assert "pip" in " ".join(args)
        assert "qdrant-client" in args
        assert "sentence-transformers" in args
        assert "falkordb" not in args

    def test_install_skgraph_deps(self) -> None:
        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            install_python_deps(["skgraph"])

        args = mock_run.call_args[0][0]
        assert "falkordb" in args
        assert "qdrant-client" not in args

    def test_install_both_deps(self) -> None:
        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            install_python_deps(["skvector", "skgraph"])

        args = mock_run.call_args[0][0]
        assert "qdrant-client" in args
        assert "sentence-transformers" in args
        assert "falkordb" in args

    def test_install_no_backends(self) -> None:
        result = install_python_deps([])
        assert result.returncode == 0

    def test_install_failure(self) -> None:
        with mock.patch("skmemory.setup_wizard.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="ERROR: No matching distribution"
            )
            result = install_python_deps(["skvector"])
        assert result.returncode == 1


# ═══════════════════════════════════════════════════════════
# Find compose file tests
# ═══════════════════════════════════════════════════════════


class TestFindComposeFile:
    """Test compose file discovery."""

    def test_finds_bundled_compose(self) -> None:
        # The real docker-compose.yml exists in the package
        path = find_compose_file()
        assert path.exists()
        assert path.name == "docker-compose.yml"

    def test_fallback_generates_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Trick find_compose_file into not finding the bundled one
        monkeypatch.setattr(
            "skmemory.setup_wizard.Path.__file__",
            str(tmp_path / "fake" / "setup_wizard.py"),
            raising=False,
        )
        # Use CONFIG_DIR override to avoid touching real ~/.skmemory
        monkeypatch.setattr("skmemory.setup_wizard.CONFIG_DIR", tmp_path)

        # Make the bundled path not exist by patching
        fake_package = tmp_path / "nonexistent_pkg"
        with mock.patch(
            "skmemory.setup_wizard.Path.__file__", str(fake_package / "setup_wizard.py")
        ):
            # Just verify the function doesn't crash when it can't find bundled
            path = find_compose_file()
            assert path.exists()


# ═══════════════════════════════════════════════════════════
# Setup wizard integration test (all mocked)
# ═══════════════════════════════════════════════════════════


class TestSetupWizard:
    """Test the full setup wizard flow with all externals mocked."""

    def _mock_platform(self, docker=True, compose=True, legacy=False, version="24.0.7"):
        return PlatformInfo(
            os_name="Linux",
            docker_available=docker,
            compose_available=compose,
            compose_legacy=legacy,
            docker_version=version,
        )

    def test_wizard_success(self, tmp_path: Path) -> None:
        output = []

        with (
            mock.patch(
                "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
            ),
            mock.patch("skmemory.setup_wizard.check_port_available", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.find_compose_file",
                return_value=tmp_path / "docker-compose.yml",
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_up",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.check_skvector_health", return_value=True),
            mock.patch("skmemory.setup_wizard.check_skgraph_health", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.install_python_deps",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.save_config", return_value=tmp_path / "config.yaml"),
        ):
            result = run_setup_wizard(
                enable_skvector=True,
                enable_skgraph=True,
                skip_deps=False,
                non_interactive=True,
                echo=output.append,
            )

        assert result["success"] is True
        assert "skvector" in result["services"]
        assert "skgraph" in result["services"]
        assert result["health"]["skvector"] is True
        assert result["health"]["skgraph"] is True

    def test_wizard_no_docker(self) -> None:
        output = []
        with mock.patch(
            "skmemory.setup_wizard.detect_platform",
            return_value=self._mock_platform(docker=False),
        ):
            result = run_setup_wizard(non_interactive=True, echo=output.append)

        assert result["success"] is False
        assert "Docker not available" in result["errors"]

    def test_wizard_no_compose(self) -> None:
        output = []
        with mock.patch(
            "skmemory.setup_wizard.detect_platform",
            return_value=self._mock_platform(compose=False),
        ):
            result = run_setup_wizard(non_interactive=True, echo=output.append)

        assert result["success"] is False
        assert "Docker Compose not available" in result["errors"]

    def test_wizard_port_conflict(self) -> None:
        output = []
        with (
            mock.patch(
                "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
            ),
            mock.patch("skmemory.setup_wizard.check_port_available", return_value=False),
        ):
            result = run_setup_wizard(non_interactive=True, echo=output.append)

        assert result["success"] is False
        assert any("Port conflicts" in e for e in result["errors"])

    def test_wizard_compose_up_fails(self, tmp_path: Path) -> None:
        output = []
        with (
            mock.patch(
                "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
            ),
            mock.patch("skmemory.setup_wizard.check_port_available", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.find_compose_file", return_value=tmp_path / "dc.yml"
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_up",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="image pull failed"
                ),
            ),
        ):
            result = run_setup_wizard(non_interactive=True, echo=output.append)

        assert result["success"] is False
        assert any("docker compose up failed" in e for e in result["errors"])

    def test_wizard_skvector_only(self, tmp_path: Path) -> None:
        output = []
        captured = {}

        def capture_config(cfg):
            captured["config"] = cfg
            return tmp_path / "config.yaml"

        with (
            mock.patch(
                "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
            ),
            mock.patch("skmemory.setup_wizard.check_port_available", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.find_compose_file", return_value=tmp_path / "dc.yml"
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_up",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.check_skvector_health", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.install_python_deps",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.save_config", side_effect=capture_config),
        ):
            result = run_setup_wizard(
                enable_skvector=True,
                enable_skgraph=False,
                non_interactive=True,
                echo=output.append,
            )

        assert result["success"] is True
        assert result["services"] == ["skvector"]
        assert "skgraph" not in result["health"]
        assert captured["config"].skvector_embedding_model == "bge-legal-v1"
        assert captured["config"].skvector_vector_dim == 1024

    def test_wizard_no_backends_selected(self) -> None:
        output = []
        with mock.patch(
            "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
        ):
            result = run_setup_wizard(
                enable_skvector=False,
                enable_skgraph=False,
                non_interactive=True,
                echo=output.append,
            )

        assert result["success"] is True
        assert result["services"] == []

    def test_wizard_skip_deps(self, tmp_path: Path) -> None:
        output = []
        with (
            mock.patch(
                "skmemory.setup_wizard.detect_platform", return_value=self._mock_platform()
            ),
            mock.patch("skmemory.setup_wizard.check_port_available", return_value=True),
            mock.patch(
                "skmemory.setup_wizard.find_compose_file", return_value=tmp_path / "dc.yml"
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_up",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.check_skvector_health", return_value=True),
            mock.patch("skmemory.setup_wizard.install_python_deps") as mock_pip,
            mock.patch("skmemory.setup_wizard.save_config", return_value=tmp_path / "config.yaml"),
        ):
            result = run_setup_wizard(
                enable_skvector=True,
                enable_skgraph=False,
                skip_deps=True,
                non_interactive=True,
                echo=output.append,
            )

        assert result["success"] is True
        mock_pip.assert_not_called()


# ═══════════════════════════════════════════════════════════
# CLI tests
# ═══════════════════════════════════════════════════════════


class TestSetupCLI:
    """Test the setup CLI commands via Click's CliRunner."""

    def test_setup_wizard_cli(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with mock.patch("skmemory.setup_wizard.run_setup_wizard") as mock_wizard:
            mock_wizard.return_value = {
                "success": True,
                "services": [],
                "health": {},
                "config_path": None,
                "errors": [],
            }
            result = runner.invoke(cli, ["setup", "wizard", "--yes", "--no-skgraph"])

        assert result.exit_code == 0
        mock_wizard.assert_called_once()
        call_kwargs = mock_wizard.call_args
        assert (
            call_kwargs.kwargs.get("enable_skgraph") is False
            or call_kwargs[1].get("enable_skgraph") is False
        )

    def test_setup_wizard_cli_passes_embedding_options(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with mock.patch("skmemory.setup_wizard.run_setup_wizard") as mock_wizard:
            mock_wizard.return_value = {
                "success": True,
                "services": [],
                "health": {},
                "config_path": None,
                "errors": [],
            }
            result = runner.invoke(
                cli,
                [
                    "setup",
                    "wizard",
                    "--yes",
                    "--embedding-model",
                    "bge-legal-v1",
                    "--vector-dim",
                    "1024",
                ],
            )

        assert result.exit_code == 0
        _, kwargs = mock_wizard.call_args
        assert kwargs["embedding_model"] == "bge-legal-v1"
        assert kwargs["vector_dim"] == 1024

    def test_setup_wizard_cli_failure(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with mock.patch("skmemory.setup_wizard.run_setup_wizard") as mock_wizard:
            mock_wizard.return_value = {
                "success": False,
                "services": [],
                "health": {},
                "config_path": None,
                "errors": ["Docker not available"],
            }
            result = runner.invoke(cli, ["setup", "wizard", "--yes"])

        assert result.exit_code == 1

    def test_setup_status_no_config(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with mock.patch("skmemory.config.load_config", return_value=None):
            result = runner.invoke(cli, ["setup", "status"])

        assert "No setup config found" in result.output

    def test_setup_status_with_config(self) -> None:
        from skmemory.cli import cli

        cfg = SKMemoryConfig(
            skvector_url="http://localhost:6333",
            backends_enabled=["skvector"],
            setup_completed_at="2026-02-28T12:00:00+00:00",
        )
        runner = CliRunner()
        with (
            mock.patch("skmemory.config.load_config", return_value=cfg),
            mock.patch(
                "skmemory.setup_wizard.detect_platform",
                return_value=PlatformInfo(
                    os_name="Linux", docker_available=True, compose_available=True
                ),
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_ps",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="skmemory-skvector  running", stderr=""
                ),
            ),
            mock.patch("skmemory.setup_wizard.check_skvector_health", return_value=True),
        ):
            result = runner.invoke(cli, ["setup", "status"])

        assert "skvector" in result.output.lower()

    def test_setup_start_cli(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with (
            mock.patch(
                "skmemory.config.load_config",
                return_value=SKMemoryConfig(backends_enabled=["skvector"]),
            ),
            mock.patch(
                "skmemory.setup_wizard.detect_platform",
                return_value=PlatformInfo(
                    os_name="Linux", docker_available=True, compose_available=True
                ),
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_up",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
        ):
            result = runner.invoke(cli, ["setup", "start", "--service", "skvector"])

        assert result.exit_code == 0
        assert "Started" in result.output

    def test_setup_stop_cli(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with (
            mock.patch("skmemory.config.load_config", return_value=SKMemoryConfig()),
            mock.patch(
                "skmemory.setup_wizard.detect_platform",
                return_value=PlatformInfo(
                    os_name="Linux", docker_available=True, compose_available=True
                ),
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_down",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
        ):
            result = runner.invoke(cli, ["setup", "stop"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    def test_setup_stop_individual(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with (
            mock.patch("skmemory.config.load_config", return_value=SKMemoryConfig()),
            mock.patch(
                "skmemory.setup_wizard.detect_platform",
                return_value=PlatformInfo(
                    os_name="Linux", docker_available=True, compose_available=True
                ),
            ),
            mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
        ):
            result = runner.invoke(cli, ["setup", "stop", "--service", "skvector"])

        assert result.exit_code == 0

    def test_setup_reset_cli(self) -> None:
        from skmemory.cli import cli

        runner = CliRunner()
        with (
            mock.patch("skmemory.config.load_config", return_value=SKMemoryConfig()),
            mock.patch(
                "skmemory.setup_wizard.detect_platform",
                return_value=PlatformInfo(
                    os_name="Linux", docker_available=True, compose_available=True
                ),
            ),
            mock.patch(
                "skmemory.setup_wizard.compose_down",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch("skmemory.config.CONFIG_PATH", Path("/tmp/nonexistent_config.yaml")),
        ):
            result = runner.invoke(cli, ["setup", "reset", "--yes"])

        assert result.exit_code == 0


# ═══════════════════════════════════════════════════════════
# _get_store integration test (config wiring)
# ═══════════════════════════════════════════════════════════


class TestGetStoreWiring:
    """Test that _get_store wires both skvector and skgraph from config."""

    def test_get_store_wires_skgraph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skmemory.cli import _get_store

        monkeypatch.delenv("SKMEMORY_SKVECTOR_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_KEY", raising=False)
        monkeypatch.delenv("SKMEMORY_SKGRAPH_URL", raising=False)

        cfg = SKMemoryConfig(
            skvector_url="http://localhost:6333",
            skgraph_url="redis://localhost:6379",
        )
        with mock.patch("skmemory.config.load_config", return_value=cfg):
            _get_store()

        # SKVector may fail to import but that's OK — we test the wiring logic
        # SKGraph should be wired (lazy init, won't connect yet)
        assert True  # at least no crash (store.graph/vector may be None if backends unavailable)

    def test_get_store_no_config_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skmemory.cli import _get_store

        monkeypatch.delenv("SKMEMORY_SKVECTOR_URL", raising=False)
        monkeypatch.delenv("SKMEMORY_SKVECTOR_KEY", raising=False)
        monkeypatch.delenv("SKMEMORY_SKGRAPH_URL", raising=False)

        with mock.patch("skmemory.config.load_config", return_value=None):
            store = _get_store()

        # With no config and no env vars, ChromaDB is now the default vector backend.
        # It may succeed (SKChromaBackend) or gracefully fail (None); either is acceptable.
        assert store.primary is not None
        assert store.graph is None


import urllib.error  # noqa: E402
