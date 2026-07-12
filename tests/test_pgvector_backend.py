"""Guards for the skmem-pg write-path DSN invariant (prb-6f069c5e).

skmem-pg is a LOCAL, per-node writable Postgres. The default DSN must resolve to
a local writable port, never the retired :5433 standby and never a hardcoded
remote host. A node that constructs the backend without an explicit override must
NOT end up writing to a read-only replica (where every save()/delete() raises).
"""

import importlib

import pytest


def _reload_backend(monkeypatch, env_value):
    """Reimport the backend module with SKMEMORY_PG_DSN set/unset so the
    import-time DEFAULT_DSN is recomputed."""
    if env_value is None:
        monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    else:
        monkeypatch.setenv("SKMEMORY_PG_DSN", env_value)
    import skmemory.backends.pgvector_backend as mod

    return importlib.reload(mod)


def test_default_dsn_is_local_writable_port(monkeypatch):
    """With no env override the default targets the local writable port :5432."""
    mod = _reload_backend(monkeypatch, None)
    assert "localhost:5432" in mod.DEFAULT_DSN


def test_default_dsn_never_standby_or_remote(monkeypatch):
    """The write-path default must never be the retired standby port or a
    hardcoded remote host (that reintroduces the SPOF / read-only-replica bug)."""
    mod = _reload_backend(monkeypatch, None)
    assert ":5433" not in mod.DEFAULT_DSN, "retired standby port leaked into default"
    assert "192.168." not in mod.DEFAULT_DSN, "hardcoded remote host in write-path default"


def test_env_overrides_default_dsn(monkeypatch):
    """SKMEMORY_PG_DSN (the node-local lever) drives the default when set."""
    mod = _reload_backend(monkeypatch, "postgresql://postgres:x@localhost:5433/skmemory")
    assert mod.DEFAULT_DSN.endswith("localhost:5433/skmemory")


def test_backend_uses_explicit_dsn_over_default(monkeypatch):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(dsn="postgresql://postgres:x@localhost:5433/skmemory", agent="jarvis")
    assert be.dsn.endswith("localhost:5433/skmemory")
    assert be.agent == "jarvis"


def test_backend_default_dsn_is_local(monkeypatch):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina")
    assert "localhost:5432" in be.dsn


# Restore a clean module state for any later test in the session.
@pytest.fixture(autouse=True, scope="module")
def _restore_module():
    yield
    import skmemory.backends.pgvector_backend as mod

    importlib.reload(mod)
