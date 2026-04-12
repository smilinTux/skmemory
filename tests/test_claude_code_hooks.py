"""Tests for skmemory.hooks.claude_code_hooks"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from skmemory.hooks.claude_code_hooks import (
    handle_stop,
    handle_precompact,
    install_hooks,
    _get_session_id,
    _get_agent,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

SHORT_CONVERSATION = "hi"

LONG_CONVERSATION = """
Chef: We decided to use ChromaDB for vector search going forward.
Lumina: Great call. I prefer flat files for primary storage though.
Chef: Agreed. Finally shipped v0.9.4 with the WAL integration.
Lumina: All tests pass on the new branch.
Chef: Found a bug in the tail() method — root cause was off-by-one on the slice.
Lumina: This is a real breakthrough for the sovereign memory system.
""" * 5  # repeat to be > 100 chars


# ---------------------------------------------------------------------------
# Test _get_session_id and _get_agent
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_get_session_id_from_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-abc")
        assert _get_session_id() == "test-session-abc"

    def test_get_session_id_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert _get_session_id() == "unknown"

    def test_get_agent_skagent(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_AGENT", "opus")
        agent = _get_agent()
        assert agent in ("opus", "lumina")  # depends on env priority


# ---------------------------------------------------------------------------
# Test handle_stop
# ---------------------------------------------------------------------------

class TestHandleStop:
    def test_empty_conversation_does_nothing(self):
        # Should not raise, should return without saving
        with patch("skmemory.hooks.claude_code_hooks._get_store") as mock_store:
            handle_stop()
            mock_store.assert_not_called()

    def test_short_conversation_does_nothing(self):
        with patch("skmemory.hooks.claude_code_hooks._get_store") as mock_store:
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available", return_value="hi"):
                handle_stop()
            mock_store.assert_not_called()

    def test_long_conversation_extracts_memories(self):
        mock_store_instance = MagicMock()
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=LONG_CONVERSATION):
                handle_stop()
        # Should have called snapshot at least once
        assert mock_store_instance.snapshot.call_count >= 1

    def test_stop_uses_correct_tags(self):
        mock_store_instance = MagicMock()
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=LONG_CONVERSATION):
                with patch("skmemory.hooks.claude_code_hooks._get_session_id",
                           return_value="test-session-xyz"):
                    handle_stop()

        # All calls should include "auto-extract" tag
        for call in mock_store_instance.snapshot.call_args_list:
            tags = call.kwargs.get("tags", [])
            assert "auto-extract" in tags
            assert "session:test-session-xyz" in tags

    def test_stop_source_is_claude_code_hook(self):
        mock_store_instance = MagicMock()
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=LONG_CONVERSATION):
                handle_stop()

        for call in mock_store_instance.snapshot.call_args_list:
            assert call.kwargs.get("source") == "claude-code-hook"

    def test_stop_snapshot_failure_does_not_crash(self):
        mock_store_instance = MagicMock()
        mock_store_instance.snapshot.side_effect = RuntimeError("disk full")
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=LONG_CONVERSATION):
                # Should not raise even if snapshot fails
                handle_stop()


# ---------------------------------------------------------------------------
# Test handle_precompact
# ---------------------------------------------------------------------------

class TestHandlePrecompact:
    def test_empty_conversation_does_nothing(self):
        with patch("skmemory.hooks.claude_code_hooks._get_store") as mock_store:
            handle_precompact()
            mock_store.assert_not_called()

    def test_short_conversation_does_nothing(self):
        with patch("skmemory.hooks.claude_code_hooks._get_store") as mock_store:
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value="x" * 100):
                handle_precompact()
            mock_store.assert_not_called()

    def test_long_conversation_saves_snapshot(self):
        mock_store_instance = MagicMock()
        long_text = "context " * 200  # > 200 chars
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=long_text):
                handle_precompact()
        mock_store_instance.snapshot.assert_called_once()

    def test_precompact_uses_last_4000_chars(self):
        mock_store_instance = MagicMock()
        long_text = "A" * 3000 + "B" * 3000  # 6000 chars total
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=long_text):
                handle_precompact()

        call_kwargs = mock_store_instance.snapshot.call_args.kwargs
        content = call_kwargs["content"]
        # Should be the last 4000 chars (all B's)
        assert len(content) <= 4000
        assert "B" in content

    def test_precompact_tags(self):
        mock_store_instance = MagicMock()
        long_text = "context " * 200
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=long_text):
                with patch("skmemory.hooks.claude_code_hooks._get_session_id",
                           return_value="sess-123"):
                    handle_precompact()

        call_kwargs = mock_store_instance.snapshot.call_args.kwargs
        assert "pre-compact" in call_kwargs["tags"]
        assert "auto-save" in call_kwargs["tags"]
        assert "session:sess-123" in call_kwargs["tags"]

    def test_precompact_failure_does_not_crash(self):
        mock_store_instance = MagicMock()
        mock_store_instance.snapshot.side_effect = RuntimeError("oops")
        long_text = "context " * 200
        with patch("skmemory.hooks.claude_code_hooks._get_store", return_value=mock_store_instance):
            with patch("skmemory.hooks.claude_code_hooks._read_stdin_if_available",
                       return_value=long_text):
                # Should not raise
                handle_precompact()


# ---------------------------------------------------------------------------
# Test install_hooks
# ---------------------------------------------------------------------------

class TestInstallHooks:
    def test_creates_settings_file(self, tmp_path):
        settings_path = tmp_path / ".claude" / "settings.json"
        install_hooks(settings_path)
        assert settings_path.exists()

    def test_installs_stop_hook(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        install_hooks(settings_path)
        settings = json.loads(settings_path.read_text())
        stop_cmds = [h["command"] for h in settings["hooks"]["Stop"]]
        assert any("claude_code_hooks stop" in cmd for cmd in stop_cmds)

    def test_installs_precompact_hook(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        install_hooks(settings_path)
        settings = json.loads(settings_path.read_text())
        precompact_cmds = [h["command"] for h in settings["hooks"]["PreCompact"]]
        assert any("claude_code_hooks precompact" in cmd for cmd in precompact_cmds)

    def test_idempotent_install(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        install_hooks(settings_path)
        install_hooks(settings_path)  # Second install should not duplicate
        settings = json.loads(settings_path.read_text())
        stop_hooks = settings["hooks"]["Stop"]
        precompact_hooks = settings["hooks"]["PreCompact"]
        stop_cmds = [h["command"] for h in stop_hooks]
        precompact_cmds = [h["command"] for h in precompact_hooks]
        # Each hook command should appear exactly once
        assert stop_cmds.count("python3 -m skmemory.hooks.claude_code_hooks stop") == 1
        assert precompact_cmds.count("python3 -m skmemory.hooks.claude_code_hooks precompact") == 1

    def test_preserves_existing_settings(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        existing = {"theme": "dark", "fontSize": 14}
        settings_path.write_text(json.dumps(existing))
        install_hooks(settings_path)
        settings = json.loads(settings_path.read_text())
        assert settings["theme"] == "dark"
        assert settings["fontSize"] == 14
        assert "hooks" in settings

    def test_handles_corrupt_settings(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{ not valid json }")
        # Should not raise
        install_hooks(settings_path)
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
