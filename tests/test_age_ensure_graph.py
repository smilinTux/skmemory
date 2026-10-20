"""Nothing in skmemory ever provisioned a per-agent AGE graph.

The graph role was wired, queried and health-checked, but whoever added an
agent had to know to run `SELECT ag_catalog.create_graph('<agent>_knowledge')`
by hand. Three agents on one node had no graph at all as a result, and until
the health fix landed they reported a healthy EMPTY graph rather than a missing
one, so nobody found out.

Two things make this easy to get wrong by hand, both of them hit for real on
2026-10-20:

  * `create_graph` needs `ag_catalog` on the search_path or it fails with
    `operator class "graphid_ops" does not exist for access method "btree"`,
    an error that says nothing whatsoever about search_path.
  * It is not idempotent, so a second call errors rather than no-opping.

Verified live against three real agents after the fix: each returned False
(already present) with a healthy graph, so the guard does not re-create.
"""

from __future__ import annotations

import pytest

from skmemory.backends.age_backend import AGEGraphBackend, GraphTransportError


class _Cur:
    def __init__(self, owner, exists):
        self.owner = owner
        self._exists = exists

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.owner.statements.append((sql, params))
        if self.owner.fail_on and self.owner.fail_on in sql:
            raise RuntimeError("boom")

    def fetchone(self):
        return (1,) if self._exists else None


class _Conn:
    def __init__(self, exists=False, fail_on=None):
        self.exists = exists
        self.fail_on = fail_on
        self.statements: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _Cur(self, self.exists)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _backend(monkeypatch, conn):
    b = AGEGraphBackend.__new__(AGEGraphBackend)
    b.graph = "test_knowledge"
    b.dsn = "postgresql://u:p@127.0.0.1:1/none"
    b.agent = "test"
    monkeypatch.setattr(b, "_conn", lambda: conn)
    return b


class TestCreation:
    def test_an_absent_graph_is_created(self, monkeypatch):
        conn = _Conn(exists=False)
        created = _backend(monkeypatch, conn).ensure_graph()

        assert created is True
        assert conn.commits == 1
        sqls = " | ".join(s for s, _ in conn.statements)
        assert "create_graph" in sqls

    def test_it_sets_the_search_path_before_creating(self, monkeypatch):
        """Without ag_catalog on the path, create_graph fails with an error
        about graphid_ops that gives no hint what is actually wrong."""
        conn = _Conn(exists=False)
        _backend(monkeypatch, conn).ensure_graph()

        order = [s for s, _ in conn.statements]
        path_at = next(i for i, s in enumerate(order) if "search_path" in s)
        create_at = next(i for i, s in enumerate(order) if "create_graph" in s)
        assert path_at < create_at, "search_path must be set BEFORE create_graph"
        assert "ag_catalog" in order[path_at]

    def test_the_graph_name_is_passed_as_a_parameter(self, monkeypatch):
        """Not interpolated. The name comes from a profile but parameterising
        it costs nothing and removes the question entirely."""
        conn = _Conn(exists=False)
        _backend(monkeypatch, conn).ensure_graph()

        create = next((s, p) for s, p in conn.statements if "create_graph" in s)
        assert create[1] == ("test_knowledge",)


class TestIdempotency:
    def test_an_existing_graph_is_not_recreated(self, monkeypatch):
        """create_graph is not idempotent; a second call errors."""
        conn = _Conn(exists=True)
        created = _backend(monkeypatch, conn).ensure_graph()

        assert created is False
        sqls = " | ".join(s for s, _ in conn.statements)
        assert "create_graph" not in sqls
        assert conn.commits == 0, "nothing to commit"

    def test_it_checks_the_catalog_first(self, monkeypatch):
        conn = _Conn(exists=True)
        _backend(monkeypatch, conn).ensure_graph()
        assert "ag_graph" in conn.statements[0][0]


class TestFailures:
    def test_a_disabled_graph_raises(self, monkeypatch):
        b = AGEGraphBackend.__new__(AGEGraphBackend)
        b.graph = None
        b.dsn = "postgresql://u:p@127.0.0.1:1/none"
        b.agent = "test"
        with pytest.raises(GraphTransportError, match="invalid graph name"):
            b.ensure_graph()

    def test_no_connection_raises(self, monkeypatch):
        b = _backend(monkeypatch, None)
        monkeypatch.setattr(b, "_conn", lambda: None)
        with pytest.raises(GraphTransportError, match="connection unavailable"):
            b.ensure_graph()

    def test_a_failed_create_rolls_back_and_raises(self, monkeypatch):
        conn = _Conn(exists=False, fail_on="create_graph")
        with pytest.raises(GraphTransportError, match="could not create AGE graph"):
            _backend(monkeypatch, conn).ensure_graph()
        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_a_failed_catalog_read_raises_rather_than_assuming_absent(self, monkeypatch):
        """Assuming absent would mean trying to create an existing graph."""
        conn = _Conn(exists=False, fail_on="ag_graph")
        with pytest.raises(GraphTransportError):
            _backend(monkeypatch, conn).ensure_graph()
        assert conn.commits == 0
