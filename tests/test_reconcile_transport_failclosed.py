"""Fail-closed psql transport for the pgvector reconcile (card 9157c2c5).

Incident chg-a76c0aee: ``skmemory sync --vector`` ran with a dead Docker
socket (permission denied), the failure was misread as an empty pg store
(pg=0), and an unnecessary full backfill began. These tests pin the
regression contract against fake transports (no docker daemon, no Postgres,
no network):

  * a transport probe (``select 1;``) runs before any counting query or
    mutation, and its failure raises ``ReconcileTransportError`` naming the
    transport and the permission problem;
  * a mid-run query failure aborts before any mutating SQL is issued;
  * error output is scrubbed so a DSN password never reaches a message;
  * a healthy fake transport still reconciles end to end;
  * the CLI exits nonzero on reconcile failure.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skmemory import dsn_psql
from skmemory import reconcile as reconcile_mod
from skmemory.cli import cli
from skmemory.reconcile import ReconcileTransportError, probe_transport

DOCKER_SOCKET_DENIED = (
    "permission denied while trying to connect to the Docker daemon socket at "
    'unix:///var/run/docker.sock: Get "http://%2Fvar%2Frun%2Fdocker.sock/v1.24/'
    'containers/skmem-pg/json": dial unix /var/run/docker.sock: connect: '
    "permission denied"
)

MUTATING_TOKENS = ("insert into", "delete from", "drop table", "create table", "copy ")


class _CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _DeadTransport:
    """Every call fails the way a docker-socket permission denial does."""

    def __init__(self, stderr=DOCKER_SOCKET_DENIED):
        self.stderr = stderr
        self.statements = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            return _CP()
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        return _CP(stderr=self.stderr, returncode=1)


def _write_flat(mem_dir: Path, layer: str, mem_id: str) -> None:
    layer_dir = mem_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": f"fixture {mem_id[:8]}", "layer": layer}),
        encoding="utf-8",
    )


def _mutations(statements):
    return [s for s in statements if any(token in s.lower() for token in MUTATING_TOKENS)]


# ----------------------------------------------------------- probe contract ---


def test_probe_names_transport_and_permission_problem():
    fake = _DeadTransport()
    with (
        patch.object(reconcile_mod.subprocess, "run", fake.run),
        pytest.raises(ReconcileTransportError) as caught,
    ):
        probe_transport(["docker", "exec", "-i", "skmem-pg", "psql"])
    message = str(caught.value)
    assert "transport unavailable" in message
    assert "docker exec skmem-pg" in message
    assert "permission denied" in message
    # The probe is the only thing the transport was asked to do.
    assert fake.statements == ["select 1;"]


def test_probe_missing_binary_is_a_transport_error():
    with (
        patch.object(reconcile_mod.subprocess, "run", side_effect=FileNotFoundError("docker")),
        pytest.raises(ReconcileTransportError) as caught,
    ):
        probe_transport(["docker", "exec", "-i", "skmem-pg", "psql"])
    assert "transport unavailable" in str(caught.value)


def test_probe_dsn_transport_is_named_without_the_dsn():
    fake = _DeadTransport(stderr="connection to server failed: connection refused")
    dsn_cmd = [sys.executable, "-m", "skmemory.dsn_psql"]
    with (
        patch.object(reconcile_mod.subprocess, "run", fake.run),
        pytest.raises(ReconcileTransportError) as caught,
    ):
        probe_transport(dsn_cmd)
    message = str(caught.value)
    assert "dsn" in message
    assert "SKMEMORY_PG_DSN" in message


# ------------------------------------------------------- reconcile failclosed ---


def test_permission_denied_aborts_before_any_mutation(monkeypatch, tmp_path):
    """THE incident regression: dead docker socket, no mutation, no phantom pg=0."""
    fake = _DeadTransport()
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    for _ in range(3):
        _write_flat(mem, "short-term", str(uuid.uuid4()))

    with pytest.raises(ReconcileTransportError) as caught:
        reconcile_mod.reconcile("lumina", mem_dir=str(mem), psql_cmd=["psql"], verbose=False)

    message = str(caught.value)
    assert "permission denied" in message
    # Only the probe reached the transport: no counting query, no mutation.
    assert fake.statements == ["select 1;"]
    assert _mutations(fake.statements) == []


def test_mid_run_counting_query_failure_aborts_before_mutation(monkeypatch, tmp_path):
    """The probe succeeds but the pg counting query dies: still no mutation."""

    class _FlakyTransport:
        def __init__(self):
            self.statements = []

        def run(self, args, capture_output=False, text=False, input=None, timeout=None):
            if args and args[0] == "sk-alert":
                return _CP()
            sql = args[args.index("-c") + 1] if "-c" in args else (input or "")
            self.statements.append(sql)
            if sql.startswith("select id from memories"):
                return _CP(stderr="server closed the connection unexpectedly", returncode=1)
            return _CP(stdout="1\n")

    fake = _FlakyTransport()
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "mid-term", str(uuid.uuid4()))

    with pytest.raises(RuntimeError):
        reconcile_mod.reconcile("lumina", mem_dir=str(mem), psql_cmd=["psql"], verbose=False)

    assert _mutations(fake.statements) == []


def test_query_error_during_backfill_aborts_before_insert(monkeypatch, tmp_path):
    """A rejected DDL mid-backfill aborts before the INSERT/COPY lands."""

    class _BackfillBreaker:
        def __init__(self):
            self.statements = []

        def run(self, args, capture_output=False, text=False, input=None, timeout=None):
            if args and args[0] == "sk-alert":
                return _CP()
            sql = args[args.index("-c") + 1] if "-c" in args else (input or "")
            self.statements.append(sql)
            low = sql.lower()
            if "memories_bf" in low:
                return _CP(stderr='relation "memories_bf" is not permitted here', returncode=1)
            if low.startswith("select id from memories where agent"):
                return _CP(stdout="")  # pg empty: every flat memory is missing
            return _CP(stdout="1\n")

    fake = _BackfillBreaker()
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", str(uuid.uuid4()))

    with pytest.raises(RuntimeError) as caught:
        reconcile_mod.reconcile("lumina", mem_dir=str(mem), psql_cmd=["psql"], verbose=False)

    assert not isinstance(caught.value, ReconcileTransportError)
    assert "query failed" in str(caught.value)
    issued = [s.lower() for s in fake.statements]
    assert not any("insert into memories" in s for s in issued)
    assert not any("copy memories_bf" in s for s in issued)


def test_happy_path_reconcile_still_works(monkeypatch, tmp_path):
    """A healthy transport reconciles: probe, count, no-op prune, stats intact."""

    class _HealthyTransport:
        def __init__(self, pg_ids):
            self.pg_ids = list(pg_ids)
            self.statements = []

        def run(self, args, capture_output=False, text=False, input=None, timeout=None):
            if args and args[0] == "sk-alert":
                return _CP()
            sql = args[args.index("-c") + 1] if "-c" in args else (input or "")
            self.statements.append(sql)
            low = sql.lower()
            if "delete from memories" in low:
                return _CP(stdout="0\n")  # flat and pg agree: nothing pruned
            if low.startswith("select id from memories where agent"):
                return _CP(stdout="\n".join(self.pg_ids) + "\n")
            if "embedding is null" in low:
                return _CP(stdout="")
            if "count(*) filter" in low:
                n = len(self.pg_ids)
                return _CP(stdout=f"{n}/{n}\n")
            return _CP(stdout="1\n")

    mem_id = str(uuid.uuid4())
    fake = _HealthyTransport([mem_id])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "long-term", mem_id)

    stats = reconcile_mod.reconcile("lumina", mem_dir=str(mem), psql_cmd=["psql"], verbose=False)

    assert fake.statements[0] == "select 1;", "the transport probe runs first"
    assert stats["flat"] == 1
    assert stats["pg"] == 1
    assert stats["missing"] == 0
    assert stats["backfilled"] == 0
    assert stats["pruned"] == 0
    assert stats["prune_skipped"] is False


# ------------------------------------------------------------- scrubbing ---


def test_scrub_stderr_redacts_password_forms():
    dirty = (
        "connection failed: postgresql://agent:s3cretpw@10.0.0.9:5432/skmemory "
        "password=s3cretpw timeout"
    )
    clean = reconcile_mod._scrub_stderr(dirty)
    assert "s3cretpw" not in clean
    assert "password=***" in clean
    assert "://***:***@" in clean


def test_dsn_psql_error_output_is_scrubbed(monkeypatch, capsys):
    """A psycopg failure that echoes the DSN must not leak its password."""
    fake_psycopg = MagicMock()
    fake_psycopg.connect.side_effect = RuntimeError(
        "connection failed for dsn postgresql://agent:s3cretpw@10.0.0.9:5432/skmemory"
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://agent:s3cretpw@10.0.0.9:5432/skmemory")
    rc = dsn_psql.main(["-c", "select 1;"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "transport failed" in err
    assert "s3cretpw" not in err


# ----------------------------------------------------------------- CLI seam ---


def test_cli_vector_exits_nonzero_on_transport_failure(monkeypatch, tmp_path):
    """`skmemory sync --vector` fails closed when the pg transport is dead."""
    base = tmp_path / "lumina"
    (base / "memory").mkdir(parents=True)
    paths = {"base": base, "config": base / "config", "memory": base / "memory"}
    # Keep the flat scan on the throwaway tree, never the host's agent home.
    monkeypatch.setenv("SKMEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)

    store = MagicMock()
    store.export_orphans_to_flat.return_value = {"exported": 0, "errors": 0}
    store.reindex.return_value = 0

    fake = _DeadTransport()
    with (
        patch("skmemory.agents.get_agent_paths", return_value=paths),
        patch("skmemory.config.load_config", return_value=None),
        patch.object(reconcile_mod.subprocess, "run", fake.run),
    ):
        result = CliRunner().invoke(cli, ["sync", "--vector"], obj={"store": store})

    assert result.exit_code == 1
    assert "pgvector reconcile failed" in result.output
    assert "transport unavailable" in result.output
    assert "permission denied" in result.output
    assert _mutations(fake.statements) == []
