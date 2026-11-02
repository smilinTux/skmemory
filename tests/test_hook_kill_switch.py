"""Tests for the SKMEMORY_HOOKS hook kill switch."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from skmemory import hook_cli
from skmemory.hooks import claude_code_hooks


@pytest.mark.parametrize("event", hook_cli.EVENTS)
@pytest.mark.parametrize("harness", hook_cli.HARNESSES)
def test_disabled_unified_hook_is_silent_before_all_work(monkeypatch, capsys, event, harness):
    """Every supported event and harness exits before input or side effects."""

    class ForbiddenInput:
        def isatty(self):
            raise AssertionError("stdin inspected")

        def read(self):
            raise AssertionError("stdin read")

    monkeypatch.setenv("SKMEMORY_HOOKS", "off")
    monkeypatch.setenv("SKMEMORY_HOOK_DEBUG", "1")
    out = io.StringIO()
    with (
        patch.object(hook_cli, "_context_event") as context_event,
        patch.object(hook_cli, "_save_event") as save_event,
        patch.object(hook_cli, "_stop_event") as stop_event,
    ):
        assert hook_cli.run_hook(event, harness, stdin=ForbiddenInput(), out=out) == 0
    context_event.assert_not_called()
    save_event.assert_not_called()
    stop_event.assert_not_called()
    assert out.getvalue() == ""
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_disabled_codex_stop_is_silent_even_in_debug_mode(monkeypatch, capsys):
    monkeypatch.setenv("SKMEMORY_HOOKS", "off")
    monkeypatch.setenv("SKMEMORY_HOOK_DEBUG", "1")
    assert hook_cli.run_hook("stop", "codex", stdin=None) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_disabled_internal_persist_does_not_read_or_delete_payload(monkeypatch):
    monkeypatch.setenv("SKMEMORY_HOOKS", "off")
    with (
        patch.object(Path, "read_text", side_effect=AssertionError("payload read")),
        patch.object(os, "unlink", side_effect=AssertionError("payload deleted")),
    ):
        assert hook_cli.persist("must-remain.json") == 0


@pytest.mark.parametrize("value", [None, "on"])
def test_unset_and_on_keep_existing_hook_behavior(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SKMEMORY_HOOKS", raising=False)
    else:
        monkeypatch.setenv("SKMEMORY_HOOKS", value)
    out = io.StringIO()
    with patch.object(hook_cli, "_context_event", return_value="context") as build:
        assert hook_cli.run_hook("session-start", "generic", stdin=io.StringIO(""), out=out) == 0
    build.assert_called_once()
    assert out.getvalue() == "context\n"


@pytest.mark.parametrize(
    "handler", [claude_code_hooks.handle_stop, claude_code_hooks.handle_precompact]
)
def test_disabled_legacy_python_hooks_do_not_read_stdin(monkeypatch, handler):
    monkeypatch.setenv("SKMEMORY_HOOKS", "off")
    with patch.object(
        claude_code_hooks,
        "_read_stdin_if_available",
        side_effect=AssertionError("legacy stdin read"),
    ):
        assert handler() is None


def test_legacy_shell_hooks_exit_before_cli_and_print_nothing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "called"
    fake = fake_bin / "skmemory"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nprintf 'unexpected output\\n'\n")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "SKMEMORY_HOOKS": "off",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }

    wrappers = sorted((Path(__file__).parents[1] / "skmemory" / "hooks").glob("*.sh"))
    assert len(wrappers) == 8
    for wrapper in wrappers:
        result = subprocess.run(
            [str(wrapper)],
            input="do not read",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, wrapper.name
        assert result.stdout == result.stderr == "", wrapper.name
    assert not marker.exists()
