"""DSN transport selection and fail-closed reconcile behavior."""

from __future__ import annotations

import sys

import pytest

from skmemory import reconcile as reconcile_mod
from skmemory.dsn_psql import _sql_from_argv


def test_configured_dsn_selects_protected_python_transport(monkeypatch) -> None:
    monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://user:secret@localhost/skmemory")

    command = reconcile_mod.default_psql_cmd()

    assert command == [sys.executable, "-m", "skmemory.dsn_psql"]
    assert "secret" not in " ".join(command)


def test_missing_dsn_retains_legacy_local_container_transport(monkeypatch) -> None:
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)

    assert reconcile_mod.default_psql_cmd() == reconcile_mod.DEFAULT_PSQL


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-tAF\\t", "-c", "select 1"], ("select 1", True)),
        (["-c", "select 1"], ("select 1", False)),
    ],
)
def test_dsn_transport_parses_supported_command_forms(argv, expected) -> None:
    assert _sql_from_argv(argv) == expected


def test_reconcile_stops_on_sql_transport_failure(monkeypatch, tmp_path) -> None:
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(reconcile_mod.subprocess, "run", lambda *args, **kwargs: Failed())
    memory = tmp_path / "memory"
    (memory / "short-term").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="connection refused"):
        reconcile_mod.reconcile("jarvis", mem_dir=str(memory), psql_cmd=["psql"], verbose=False)
