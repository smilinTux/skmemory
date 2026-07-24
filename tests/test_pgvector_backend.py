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


# --- Gap A: forget() must delete the pgvector row immediately (card 23a722ca) ---
#
# MemoryStore.forget() calls self.vector.remove(memory_id). PGVectorBackend used
# to expose only delete() (no remove()), so on the default (pgvector) deployment
# forget() raised AttributeError, which store.py swallowed — the pg row survived
# until the daily reconcile prune. remove() now cascades the delete like the
# other vector backends. These run without a live DB by faking the connection.


class _FakeCursor:
    """Records executed SQL and returns a scripted rowcount per execute()."""

    def __init__(self, rowcounts):
        self._rowcounts = list(rowcounts)
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor


def _backend_with_cursor(monkeypatch, rowcounts):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina")
    cur = _FakeCursor(rowcounts)
    be._conn = _FakeConn(cur)  # _connection() returns this (not closed) as-is
    return be, cur


def test_pgvector_backend_exposes_remove(monkeypatch):
    """forget() calls vector.remove(); the pgvector backend must have it.

    Fail-before: PGVectorBackend had no remove() (only delete()), so this
    attribute was missing and forget() raised AttributeError.
    """
    mod = _reload_backend(monkeypatch, None)
    assert callable(getattr(mod.PGVectorBackend, "remove", None))


def test_remove_deletes_row_and_cascades_chunks(monkeypatch):
    """remove() deletes the memory's own row (scoped id+agent) AND cascades to
    child/chunk rows via memory_json.parent_id — matching the other backends."""
    be, cur = _backend_with_cursor(monkeypatch, rowcounts=[1, 2])
    result = be.remove("mem-123")

    assert result is True
    # main-row delete: scoped to id + agent
    assert any(
        "DELETE FROM memories" in sql and "id=%s" in sql and params == ("mem-123", "lumina")
        for sql, params in cur.executed
    ), f"no scoped main-row delete in {cur.executed}"
    # chunk/child cascade: delete rows whose memory_json.parent_id == id
    assert any(
        "parent_id" in sql and params == ("mem-123", "lumina") for sql, params in cur.executed
    ), f"no parent_id cascade delete in {cur.executed}"


def test_remove_returns_false_when_nothing_deleted(monkeypatch):
    """No matching row and no children -> nothing removed -> False."""
    be, _cur = _backend_with_cursor(monkeypatch, rowcounts=[0, 0])
    assert be.remove("ghost") is False


def test_remove_true_when_only_children_deleted(monkeypatch):
    """A child/chunk cascade alone still counts as a removal."""
    be, _cur = _backend_with_cursor(monkeypatch, rowcounts=[0, 3])
    assert be.remove("parent-only") is True


# Restore a clean module state for any later test in the session.
@pytest.fixture(autouse=True, scope="module")
def _restore_module():
    yield
    import skmemory.backends.pgvector_backend as mod

    importlib.reload(mod)
