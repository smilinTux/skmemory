"""Fail-closed backend selection and honest health (two silent-degradation defects).

Measured on 192.168.0.41 on 2026-11-06 with no agent selector declared and
``SKMEMORY_VECTOR_BACKEND=pgvector`` in the environment::

    cli.py: No valid registered memory profile is available
    Warning: Could not initialize ChromaDB backend, trying Qdrant
    cli.py: No valid registered memory profile is available

and ``skmemory health`` then said ``status: degraded``,
``base_path: .../agents/lumina-template/memory``, ``total_memories: 367``
(the real home held 51,270).

Defect 1 (these tests): ``get_active_agent()`` correctly returned None, but
``config.py`` then pointed ``SKMEMORY_HOME`` at ``lumina-template`` and the
backends opened it as the live store. Separately, the CLI never read
``SKMEMORY_VECTOR_BACKEND``: it tried ChromaDB and then Qdrant. Chef's rule:
the retired vector store is gone for good, and an explicitly
requested backend that cannot initialise fails closed with the real reason.

Defect 2: ``memory_health`` returned ``embed_ok/vector_ok/graph_ok: null`` and
omitted the ``vector``/``graph`` blocks, while the real errors went only to
stderr. ``null`` reads as "not probed", never as "broken", so a separate nor
incident went unnoticed for days. ``null`` must mean "not configured".
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skmemory import cli as skcli
from skmemory.backend_select import BackendInitError, resolve_pg_dsn
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Defect 1a: a refused profile must not degrade into the template home
# ---------------------------------------------------------------------------


def _run_isolated(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Run *body* in a fresh interpreter whose agents dir has two real agents,
    a template, and NO selector (the 2026-11-06 .41 shape)."""
    home = tmp_path / "skhome"
    for name in ("lumina", "opus", "lumina-template"):
        (home / "agents" / name / "config").mkdir(parents=True)
        (home / "agents" / name / "config" / "skmemory.yaml").write_text("agent: x\n")
        (home / "agents" / name / "memory").mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "SKCAPSTONE_HOME": str(home),
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def test_unresolved_profile_does_not_become_the_template_home(tmp_path):
    out = _run_isolated(
        tmp_path,
        """
        from skmemory import config
        print(config.SKMEMORY_HOME.name)
        print(bool(getattr(config, 'PROFILE_UNRESOLVED', None)))
        """,
    )
    assert out.returncode == 0, out.stderr
    name, unresolved = out.stdout.split()
    assert not name.endswith("-template"), "unresolved profile fell back to a template"
    assert unresolved == "True"


def test_default_backend_refuses_when_profile_unresolved(tmp_path):
    out = _run_isolated(
        tmp_path,
        """
        from skmemory.backends.sqlite_backend import SQLiteBackend
        try:
            SQLiteBackend()
        except ValueError as e:
            print("REFUSED", e)
        else:
            print("OPENED")
        """,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("REFUSED"), out.stdout
    assert "No valid registered memory profile" in out.stdout
    # and nothing was created under the template or anywhere in the agents dir
    assert not (
        tmp_path / "skhome" / "agents" / "lumina-template" / "memory" / "index.db"
    ).exists()


def test_explicit_base_path_still_works_when_profile_unresolved(tmp_path):
    """Tests and tools that pass base_path are explicit; only the DEFAULT path
    is refused."""
    out = _run_isolated(
        tmp_path,
        f"""
        from skmemory.backends.sqlite_backend import SQLiteBackend
        SQLiteBackend(base_path={str(tmp_path / "explicit")!r})
        print("OK")
        """,
    )
    assert out.stdout.strip() == "OK", out.stderr


# ---------------------------------------------------------------------------
# Defect 1b: an explicitly requested backend is used or fails, never swapped
# ---------------------------------------------------------------------------


@pytest.fixture
def spy_backends(monkeypatch):
    """Patch every vector backend constructor and record which were built."""
    built: list[str] = []

    def make(name, fail=None):
        def ctor(*a, **kw):
            built.append(name)
            if fail:
                raise RuntimeError(fail)
            return MagicMock(name=name)

        return ctor

    return built, make


def _store_with(monkeypatch, spy, env, pg_fail=None):
    built, make = spy
    for key in ("SKMEMORY_VECTOR_BACKEND", "SKMEMORY_NO_VECTOR"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    with (
        patch("skmemory.backends.pgvector_backend.PGVectorBackend", make("pgvector", pg_fail)),
        patch("skmemory.backends.skvector_backend.SKVectorBackend", make("skvector")),
    ):
        return skcli._get_store()


def test_explicit_pgvector_failure_fails_closed_and_never_tries_another_store(
    monkeypatch, spy_backends
):
    with pytest.raises(BackendInitError, match="dsn guard says no"):
        _store_with(
            monkeypatch,
            spy_backends,
            {"SKMEMORY_VECTOR_BACKEND": "pgvector"},
            pg_fail="dsn guard says no",
        )
    assert spy_backends[0] == ["pgvector"], "must not fall back to another store"


def test_explicit_pgvector_is_used(monkeypatch, spy_backends):
    store = _store_with(monkeypatch, spy_backends, {"SKMEMORY_VECTOR_BACKEND": "pgvector"})
    assert spy_backends[0] == ["pgvector"]
    assert store.vector is not None


def test_no_declaration_builds_no_vector_backend(monkeypatch, spy_backends):
    """With nothing declared the old code defaulted to an embedded vector store."""
    store = _store_with(monkeypatch, spy_backends, {})
    assert spy_backends[0] == []
    assert store.vector is None


@pytest.mark.parametrize("name", ["chroma", "chromadb", "CHROMA"])
def test_retired_backend_env_fails_fast_with_message(monkeypatch, spy_backends, name):
    with pytest.raises(BackendInitError, match="ChromaDB has been retired") as exc:
        _store_with(monkeypatch, spy_backends, {"SKMEMORY_VECTOR_BACKEND": name})
    assert "pgvector" in str(exc.value) and "FTS5" in str(exc.value)
    assert spy_backends[0] == [], "nothing may be built for a retired backend"


def test_retired_backend_env_exits_nonzero_on_cli_and_health(tmp_path):
    out = _run_isolated(
        tmp_path,
        """
        import os, sys
        os.environ["SKCAPSTONE_AGENT"] = "lumina"
        os.environ["SKMEMORY_VECTOR_BACKEND"] = "chroma"
        from click.testing import CliRunner
        from skmemory.cli import cli
        for cmd in (["health"], ["list"]):
            r = CliRunner().invoke(cli, cmd)
            print(cmd[0], r.exit_code, "retired" in (r.output or ""))
        """,
    )
    assert out.returncode == 0, out.stderr
    lines = dict((ln.split()[0], ln.split()[1:]) for ln in out.stdout.strip().splitlines())
    for cmd in ("health", "list"):
        assert lines[cmd][0] != "0", out.stdout
    assert lines["health"][1] == "True", "health must carry the message on stdout"


def test_stale_yaml_keys_are_ignored_with_one_warning(tmp_path, caplog):
    import logging

    from skmemory import config as skconfig

    skconfig._retired_warned = False
    yml = tmp_path / "skmemory.yaml"
    yml.write_text(
        "chroma_persist_dir: /x\nchroma_collection: c\nbackends_enabled: [sqlite, chroma, pgvector]\n"
    )
    with caplog.at_level(logging.WARNING, logger="skmemory.config"):
        cfg = skconfig.load_config(yml)
        skconfig.load_config(yml)
    assert cfg is not None, "a stale yaml must not fail config loading"
    assert cfg.backends_enabled == ["sqlite", "pgvector"]
    assert not hasattr(cfg, "chroma_persist_dir")
    warnings = [r for r in caplog.records if "retired" in r.getMessage()]
    assert len(warnings) == 1, "warn once per process, not per load"


def test_explicit_none_builds_no_vector(monkeypatch, spy_backends):
    store = _store_with(monkeypatch, spy_backends, {"SKMEMORY_VECTOR_BACKEND": "none"})
    assert spy_backends[0] == []
    assert store.vector is None


def test_unknown_backend_name_is_an_error_not_a_guess(monkeypatch, spy_backends):
    with pytest.raises(BackendInitError, match="not a known vector backend"):
        _store_with(monkeypatch, spy_backends, {"SKMEMORY_VECTOR_BACKEND": "pgvecter"})
    assert spy_backends[0] == []


def test_failed_config_declared_pgvector_does_not_reach_another_store(monkeypatch, spy_backends):
    """backends_enabled=[pgvector] with a failing constructor: no Chroma."""
    from skmemory import config as skconfig

    cfg = skconfig.SKMemoryConfig(backends_enabled=["pgvector"])
    with patch("skmemory.config.load_config", return_value=cfg):
        store = _store_with(monkeypatch, spy_backends, {}, pg_fail="boom")
    assert spy_backends[0] == ["pgvector"]
    assert store.vector is None
    # and the failure is carried into health instead of vanishing
    h = store.health()
    assert h["vector"]["ok"] is False
    assert "boom" in h["vector"]["error"]


def test_mcp_explicit_pgvector_unhealthy_fails_closed(monkeypatch):
    import skmemory.mcp_server as mcp

    mcp._store = None
    monkeypatch.setenv("SKMEMORY_VECTOR_BACKEND", "pgvector")
    pg = MagicMock()
    pg.health_check.return_value = {"ok": False, "error": "connection refused"}
    with (
        patch("skmemory.backends.pgvector_backend.PGVectorBackend", return_value=pg),
        pytest.raises(BackendInitError, match="connection refused"),
    ):
        mcp._get_store()
    mcp._store = None


def test_mcp_retired_backend_env_fails_fast(monkeypatch):
    import skmemory.mcp_server as mcp

    mcp._store = None
    monkeypatch.setenv("SKMEMORY_VECTOR_BACKEND", "chroma")
    with pytest.raises(BackendInitError, match="ChromaDB was retired"):
        mcp._get_store()
    mcp._store = None


def test_mcp_default_pgvector_unhealthy_runs_on_sqlite(monkeypatch):
    import skmemory.mcp_server as mcp

    mcp._store = None
    monkeypatch.delenv("SKMEMORY_VECTOR_BACKEND", raising=False)
    pg = MagicMock()
    pg.health_check.return_value = {"ok": False, "error": "connection refused"}
    with patch("skmemory.backends.pgvector_backend.PGVectorBackend", return_value=pg):
        store = mcp._get_store()
    assert store.health()["vector"]["error"]
    mcp._store = None


def test_resolve_pg_dsn_env_beats_yaml(monkeypatch):
    cfg = MagicMock(pgvector_dsn="postgresql://yaml/db")
    monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://env/db")
    assert resolve_pg_dsn(cfg) == "postgresql://env/db"
    monkeypatch.delenv("SKMEMORY_PG_DSN")
    assert resolve_pg_dsn(cfg) == "postgresql://yaml/db"
    assert resolve_pg_dsn(None) is None


# ---------------------------------------------------------------------------
# Defect 2: health must say what is wrong
# ---------------------------------------------------------------------------


def _plain_store(tmp_path, **kw) -> MemoryStore:
    return MemoryStore(primary=SQLiteBackend(base_path=str(tmp_path / "memories")), **kw)


class _Raises:
    def health_check(self):
        raise RuntimeError("probe exploded: connection refused")


class _SilentFalse:
    def health_check(self):
        return {"ok": False}


def test_unconfigured_backends_are_null_with_explicit_blocks(tmp_path):
    h = _plain_store(tmp_path).health()
    for key in ("vector", "graph"):
        assert h[key] == {"ok": None, "configured": False, "error": None}
    assert h["vector_ok"] is None and h["graph_ok"] is None and h["embed_ok"] is None


def test_probe_that_raises_is_ok_false_with_the_error_text(tmp_path):
    h = _plain_store(tmp_path, vector=_Raises(), graph=_Raises()).health()
    for key in ("vector", "graph"):
        assert h[key]["ok"] is False
        assert h[key]["configured"] is True
        assert "connection refused" in h[key]["error"]
    assert h["vector_ok"] is False and h["graph_ok"] is False
    assert h["status"] != "ok"


def test_ok_false_without_detail_still_gets_an_error_string(tmp_path):
    h = _plain_store(tmp_path, vector=_SilentFalse()).health()
    assert h["vector"]["ok"] is False
    assert h["vector"]["error"]


def test_configured_but_failed_to_construct_is_broken_not_null(tmp_path):
    store = _plain_store(tmp_path)
    store.backend_errors["vector"] = "pgvector: dsn guard says no"
    store.backend_errors["graph"] = "SKGraph: no module falkordb"
    h = store.health()
    assert h["vector"]["ok"] is False
    assert h["vector"]["configured"] is True
    assert h["vector"]["error"] == "pgvector: dsn guard says no"
    assert h["vector_ok"] is False
    assert h["embed_ok"] is False, "a broken vector backend cannot report embeddings as unprobed"
    assert h["graph_ok"] is False
    assert "falkordb" in h["graph"]["error"]
    assert h["status"] == "degraded"


def test_template_home_is_visible_and_red(tmp_path):
    tpl = tmp_path / "agents" / "lumina-template" / "memory"
    store = MemoryStore(primary=SQLiteBackend(base_path=str(tpl)))
    h = store.health()
    assert h["profile"]["is_template"] is True
    assert h["status"] == "red"


def test_real_profile_is_not_flagged(tmp_path):
    h = _plain_store(tmp_path).health()
    assert h["profile"]["is_template"] is False
    assert h["profile"]["resolved"] is True


def test_cli_health_prints_structured_failure_when_store_cannot_open(monkeypatch):
    """The refusal must reach STDOUT JSON, not only stderr."""
    import inspect

    from click.testing import CliRunner

    # click >= 8.2 separates stdout and stderr unconditionally and REMOVED the
    # mix_stderr argument, so passing it is a TypeError there; click < 8.2 mixes
    # them by default and needs mix_stderr=False to keep stderr out of stdout.
    # Either way this test needs them separated, because its whole point is that
    # the JSON reaches stdout rather than only stderr.
    kwargs = {}
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        kwargs["mix_stderr"] = False

    monkeypatch.setenv("SKMEMORY_VECTOR_BACKEND", "pgvector")
    with patch(
        "skmemory.backends.pgvector_backend.PGVectorBackend",
        side_effect=RuntimeError("dsn guard says no"),
    ):
        res = CliRunner(**kwargs).invoke(skcli.cli, ["health"])
    assert res.exit_code != 0
    payload = json.loads(res.stdout)
    assert payload["status"] == "red"
    assert "dsn guard says no" in payload["error"]
    assert payload["vector_ok"] is False
