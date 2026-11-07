"""Per-query HNSW tuning for the filtered kNN (SKMEM-P1-10).

pgvector's HNSW scan returns at most ``hnsw.ef_search`` (default 40) candidates
BEFORE the WHERE clause runs, so a filtered query can come back short or empty
and lose true neighbours. The pg vector arm sets ef_search and iterative_scan
with SET LOCAL inside the query's own transaction.
"""

from __future__ import annotations

import importlib.util
import os
import random

import pytest

from skmemory.backends import pgvector_backend as pgb

DSN = os.environ.get("SKMEMORY_PG_DSN", "")
needs_throwaway = pytest.mark.skipif(
    not DSN
    or importlib.util.find_spec("psycopg") is None
    or importlib.util.find_spec("pgvector") is None,
    reason="needs the throwaway skmem-pg: set SKMEMORY_PG_DSN (CI has no Postgres)",
)


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return [("row",)]


class _Tx:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append(("BEGIN", None))

    def __exit__(self, *a):
        self.log.append(("END", None))
        return False


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cur(self.log)

    def transaction(self):
        return _Tx(self.log)


def test_settings_never_below_limit_and_capped(monkeypatch):
    monkeypatch.delenv("SKMEMORY_HNSW_EF_SEARCH", raising=False)
    monkeypatch.delenv("SKMEMORY_HNSW_ITERATIVE_SCAN", raising=False)
    assert pgb.hnsw_query_settings(90) == (200, "relaxed_order")
    assert pgb.hnsw_query_settings(500)[0] == 500
    assert pgb.hnsw_query_settings(5000)[0] == 1000
    monkeypatch.setenv("SKMEMORY_HNSW_EF_SEARCH", "0")
    monkeypatch.setenv("SKMEMORY_HNSW_ITERATIVE_SCAN", "off")
    assert pgb.hnsw_query_settings(90) == (None, "off")
    monkeypatch.setenv("SKMEMORY_HNSW_ITERATIVE_SCAN", "bogus")
    assert pgb.hnsw_query_settings(90)[1] is None


def test_max_scan_tuples_default_override_and_failsafe(monkeypatch):
    monkeypatch.delenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", raising=False)
    assert pgb.hnsw_max_scan_tuples() == 10000
    monkeypatch.setenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", "5000")
    assert pgb.hnsw_max_scan_tuples() == 5000
    monkeypatch.setenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", "0")
    assert pgb.hnsw_max_scan_tuples() is None
    monkeypatch.setenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", "junk")
    assert pgb.hnsw_max_scan_tuples() == 10000


def test_knn_fetch_max_scan_tuples_local_and_skippable(monkeypatch):
    monkeypatch.delenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", raising=False)
    conn = _Conn()
    pgb.knn_fetch(conn, "SELECT 1", [], 90)
    cfg = [(s, p) for s, p in conn.log if "max_scan_tuples" in s]
    assert len(cfg) == 1 and cfg[0][0].rstrip().endswith("true)") and cfg[0][1] == ("10000",)
    monkeypatch.setenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", "0")
    conn = _Conn()
    pgb.knn_fetch(conn, "SELECT 1", [], 90)
    assert not [s for s, _ in conn.log if "max_scan_tuples" in s]
    monkeypatch.delenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES")
    monkeypatch.setenv("SKMEMORY_HNSW_ITERATIVE_SCAN", "off")
    conn = _Conn()
    pgb.knn_fetch(conn, "SELECT 1", [], 90)
    assert not [s for s, _ in conn.log if "max_scan_tuples" in s]


def test_knn_fetch_sets_local_inside_transaction(monkeypatch):
    monkeypatch.delenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", raising=False)
    monkeypatch.delenv("SKMEMORY_HNSW_ITERATIVE_SCAN", raising=False)
    monkeypatch.delenv("SKMEMORY_HNSW_EF_SEARCH", raising=False)
    conn = _Conn()
    rows = pgb.knn_fetch(conn, "SELECT 1", [], 90)
    assert rows == [("row",)]
    sqls = [s for s, _ in conn.log]
    assert sqls[0] == "BEGIN" and sqls[-1] == "END"
    cfg = [(s, p) for s, p in conn.log if "set_config" in s]
    assert all(s.rstrip().endswith("true)") for s, _ in cfg), "must be transaction-local"
    assert {p[0] for _, p in cfg} == {"200", "relaxed_order", "10000"}
    assert sqls.index("SELECT 1") > max(i for i, s in enumerate(sqls) if "set_config" in s)


def test_knn_fetch_without_transaction_just_queries():
    class Bare:
        def __init__(self):
            self.log = []

        def cursor(self):
            return _Cur(self.log)

    conn = Bare()
    assert pgb.knn_fetch(conn, "SELECT 1", [], 10) == [("row",)]
    assert [s for s, _ in conn.log] == ["SELECT 1"]


# ------------------------------------------------------------ throwaway proof

TABLE = "zz_hnsw_fixture"
DIM = 8


def _unit(rng):
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def _near(rng, center, spread):
    v = [c + spread * g for c, g in zip(center, _unit(rng), strict=True)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


@pytest.fixture
def fixture_conn():
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(DSN, autocommit=True)
    register_vector(conn)
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(
        f"CREATE TABLE {TABLE} (id text PRIMARY KEY, agent text, embedding vector({DIM}))"
    )
    rng = random.Random(7)
    center = _unit(rng)
    rows = []
    # Another agent's rows crowd the query's neighbourhood; ours are spread out.
    for i in range(6000):
        rows.append((f"b{i}", "other", _near(rng, center, 0.05)))
    for i in range(3000):
        rows.append((f"a{i}", "mine", _near(rng, center, 0.8)))
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO {TABLE} VALUES (%s, %s, %s)", rows)
    conn.execute(f"CREATE INDEX ON {TABLE} USING hnsw (embedding vector_cosine_ops)")
    conn.execute(f"ANALYZE {TABLE}")
    try:
        yield conn, center
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.close()


def _filtered(conn, q, *, tuned, limit=90):
    sql = pgb.knn_sql("id", "agent=%s", table=TABLE)
    params = [q, "mine", q, limit]
    if tuned:
        return [r[0] for r in pgb.knn_fetch(conn, sql, params, limit)]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


@needs_throwaway
def test_untuned_filtered_hnsw_loses_true_neighbours_and_fix_restores_them(fixture_conn):
    conn, center = fixture_conn
    rng = random.Random(11)
    qs = [_near(rng, center, 0.02) for _ in range(20)]
    exact_sql = (
        f"SELECT id FROM {TABLE} WHERE agent='mine' ORDER BY embedding <=> %s::vector LIMIT 5"
    )
    lost_untuned = lost_tuned = 0
    for q in qs:
        with conn.transaction():
            conn.execute("SET LOCAL enable_indexscan = off")
            truth = [r[0] for r in conn.execute(exact_sql, [q]).fetchall()]
        lost_untuned += len(set(truth) - set(_filtered(conn, q, tuned=False)[:5]))
        lost_tuned += len(set(truth) - set(_filtered(conn, q, tuned=True)[:5]))
    assert lost_untuned > 0, "fixture must reproduce the loss (default ef_search=40)"
    assert lost_tuned == 0
    # SET LOCAL leaves nothing behind on the connection.
    assert conn.execute("SHOW hnsw.ef_search").fetchone()[0] == "40"


def test_tune_search_session_sets_max_scan_tuples(monkeypatch):
    monkeypatch.delenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", raising=False)
    monkeypatch.delenv("SKMEMORY_HNSW_ITERATIVE_SCAN", raising=False)
    be = pgb.PGVectorBackend.__new__(pgb.PGVectorBackend)
    conn = _Conn()
    be._tune_search_session(conn)
    assert ("SELECT set_config(%s, %s, false)", ("hnsw.max_scan_tuples", "10000")) in conn.log
    monkeypatch.setenv("SKMEMORY_HNSW_MAX_SCAN_TUPLES", "0")
    conn = _Conn()
    be._tune_search_session(conn)
    assert not [1 for _, p in conn.log if p and p[0] == "hnsw.max_scan_tuples"]
