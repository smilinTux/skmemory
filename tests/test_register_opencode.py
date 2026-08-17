"""Tests for opencode MCP server registration.

Opencode reads MCP servers from the ``mcp`` key of its global config
(~/.config/opencode/opencode.json), NOT from the Claude Code style
"mcpServers" file at ~/.opencode/mcp.json. These tests pin the correct
target, shape, and merge behavior.
"""

import json
from pathlib import Path

import pytest

from skmemory.register import register_mcp


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Point Path.home() at a throwaway dir so nothing touches the real home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _read_config(home: Path) -> dict:
    path = home / ".config" / "opencode" / "opencode.json"
    assert path.exists(), "opencode.json was not written"
    return json.loads(path.read_text())


class TestOpenCodeMCPRegistration:
    def test_writes_to_global_opencode_config(self, fake_home: Path) -> None:
        """Entries land in ~/.config/opencode/opencode.json under the mcp key."""
        result = register_mcp(
            "skmemory",
            "skmemory-mcp",
            [],
            env={"SKAGENT": "lumina"},
            environments=["opencode"],
        )
        assert result["opencode"] == "created"

        config = _read_config(fake_home)
        assert "mcp" in config
        assert "mcpServers" not in config

        entry = config["mcp"]["skmemory"]
        assert entry["type"] == "local"
        assert entry["command"] == ["skmemory-mcp"]
        assert entry["enabled"] is True
        assert entry["env"] == {"SKAGENT": "lumina"}

    def test_command_joins_args_into_list(self, fake_home: Path) -> None:
        register_mcp(
            "skgit",
            "node",
            ["/path/to/forgejo-mcp/build/index.js"],
            env={"GITEA_HOST": "https://skgit.skstack01.douno.it"},
            environments=["opencode"],
        )
        config = _read_config(fake_home)
        entry = config["mcp"]["skgit"]
        assert entry["command"] == ["node", "/path/to/forgejo-mcp/build/index.js"]

    def test_preserves_existing_config_keys(self, fake_home: Path) -> None:
        """Merging must not clobber provider/autoupdate or other mcp entries."""
        config_path = fake_home / ".config" / "opencode" / "opencode.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "autoupdate": True,
                    "provider": {"nvidia": {"options": {"timeout": 600000}}},
                    "mcp": {"existing": {"type": "local", "command": ["keep-me"]}},
                }
            )
        )

        register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])

        config = _read_config(fake_home)
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert config["autoupdate"] is True
        assert config["provider"] == {"nvidia": {"options": {"timeout": 600000}}}
        assert config["mcp"]["existing"] == {"type": "local", "command": ["keep-me"]}
        assert "skmemory" in config["mcp"]

    def test_update_existing_entry(self, fake_home: Path) -> None:
        config_path = fake_home / ".config" / "opencode" / "opencode.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"mcp": {"skmemory": {"type": "local", "command": ["old-cmd"]}}})
        )

        result = register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])
        assert result["opencode"] == "updated"

        config = _read_config(fake_home)
        assert config["mcp"]["skmemory"]["command"] == ["skmemory-mcp"]

    def test_idempotent_when_unchanged(self, fake_home: Path) -> None:
        register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])
        result = register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])
        assert result["opencode"] == "exists"

    def test_never_writes_legacy_file(self, fake_home: Path) -> None:
        """The old ~/.opencode/mcp.json must not be (re)created."""
        register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])
        assert not (fake_home / ".opencode" / "mcp.json").exists()

    def test_multiple_servers_merge(self, fake_home: Path) -> None:
        register_mcp("skmemory", "skmemory-mcp", [], environments=["opencode"])
        register_mcp("skcapstone", "skcapstone-mcp", [], environments=["opencode"])
        config = _read_config(fake_home)
        assert set(config["mcp"].keys()) == {"skmemory", "skcapstone"}
