"""SKMEM-P1-07 SQL executed on a THROWAWAY skmem-pg (skipped everywhere else).

Runs only when ``SKMEMORY_PG_DSN`` names a LOCAL database that is either not a
production name or the overhaul throwaway on port 55432 (which is named
``skmemory`` but is not production), with pgvector. Every row it writes is prefixed and deleted afterwards.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import uuid

import pytest

from skmemory.pytest_guard import PRODUCTION_DB_NAMES, _db_name

DSN = os.environ.get("SKMEMORY_PG_DSN", "")
_local = any(h in DSN for h in ("@127.0.0.1", "@localhost", "//127.0.0.1", "//localhost"))
pytestmark = pytest.mark.skipif(
    not DSN
    or not _local
    or (_db_name(DSN) in PRODUCTION_DB_NAMES and ":55432/" not in DSN)
    or importlib.util.find_spec("psycopg") is None
    or importlib.util.find_spec("numpy") is None,
    reason="needs a throwaway local skmem-pg (SKMEMORY_PG_DSN) with psycopg and numpy",
)

AGENT = os.environ.get("SKMEMORY_TEST_RECONCILE_AGENT", "reconcile-test")


@pytest.fixture
def pg():
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(DSN, autocommit=True)
    register_vector(conn)
    prefix = f"p107t-{uuid.uuid4().hex[:8]}-"
    yield conn, prefix
    conn.execute("DELETE FROM memories WHERE id LIKE %s", (prefix + "%",))
    conn.close()


def _mj(mem_id: str) -> str:
    from skmemory.models import Memory

    return Memory(id=mem_id, title=f"t {mem_id}", content="c").model_dump_json()


def _backend():
    from skmemory.backends.pgvector_backend import PGVectorBackend

    return PGVectorBackend(dsn=DSN, agent=AGENT, embed_fn=lambda t: [0.0] * 1024)


def test_search_session_is_tuned_on_every_connection():
    be = _backend()
    seen = {}

    def read(conn):
        return (
            conn.execute("SHOW hnsw.ef_search").fetchone()[0],
            conn.execute("SHOW hnsw.iterative_scan").fetchone()[0],
        )

    seen["owner"] = read(be._connection())
    holder = {}

    def worker():
        holder["conn"] = be._connection()
        seen["worker"] = read(holder["conn"])

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["owner"] == ("200", "relaxed_order")
    assert seen["worker"] == ("200", "relaxed_order")
    assert holder["conn"] is not be._connection(), "a worker thread must have its own connection"


def test_agent_post_filter_returns_the_requested_count(pg):
    """The measured starvation: a global HNSW with the agent filter applied after."""
    import numpy as np

    conn, prefix = pg
    rng = np.random.default_rng(3)
    centers = rng.normal(size=(20, 1024)).astype("float32")

    def vec(i):
        v = centers[i % 20] + 0.6 * rng.normal(size=1024).astype("float32")
        return v / np.linalg.norm(v)

    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY memories (id, agent, layer, title, content, memory_json, embedding) FROM STDIN"
        ) as cp,
    ):
        for i in range(2600):
            agent = AGENT if i % 9 == 0 else f"{AGENT}-noise"
            mid = f"{prefix}{i}"
            cp.write_row((mid, agent, "long-term", f"t{i}", f"c{i}", _mj(mid), vec(i)))
    conn.execute("ANALYZE memories")
    q = vec(5)
    be = _backend()
    be._embed = lambda text: [float(x) for x in q]
    want = 75
    sc = be._connection()
    sc.execute("SET enable_sort = off")  # force the HNSW + post-filter plan

    sc.execute("SET hnsw.ef_search = 40")
    sc.execute("SET hnsw.iterative_scan = off")
    before = len(be.search_semantic("q", limit=want))

    be._tune_search_session(sc)  # the shipped settings
    after = be.search_semantic("q", limit=want)
    assert before < want, "precondition: the old settings starve the filtered query"
    assert len(after) == want
    assert len({m.id for m in after}) == want


def test_results_are_ordered_by_distance_under_relaxed_order(pg):
    import numpy as np

    conn, prefix = pg
    rng = np.random.default_rng(9)
    base = rng.normal(size=1024).astype("float32")
    base /= np.linalg.norm(base)
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY memories (id, agent, layer, title, content, memory_json, embedding) FROM STDIN"
        ) as cp,
    ):
        for i in range(200):
            v = base + (0.05 + i * 0.01) * rng.normal(size=1024).astype("float32")
            mid = f"{prefix}{i:03d}"
            cp.write_row((mid, AGENT, "long-term", f"t{i}", "c", _mj(mid), v / np.linalg.norm(v)))
    be = _backend()
    be._embed = lambda text: [float(x) for x in base]
    rows = [r for r in be.search_semantic("q", limit=50) if r.id.startswith(prefix)]
    ids = [r.id for r in rows]
    assert len(ids) == 50, "the query must actually return parseable rows"
    dist = {
        r[0]: float(1.0 - np.dot(base, np.array(r[1], dtype="float32")))
        for r in conn.execute(
            "SELECT id, embedding FROM memories WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    }
    got = [dist[i] for i in ids]
    assert got == sorted(got), "relaxed_order output must be re-sorted by true distance"


def test_reembed_on_the_throwaway_db_writes_only_the_embedding(pg, tmp_path):
    from skmemory import reembed as rb

    conn, prefix = pg
    other = f"{AGENT}-other"
    with conn.cursor() as cur:
        for i in range(7):
            for agent in (AGENT, other):
                cur.execute(
                    "INSERT INTO memories (id, agent, layer, title, content, summary, memory_json, embedding) "
                    "VALUES (%s,%s,'long-term',%s,%s,%s,'{}', %s::vector)",
                    (
                        f"{prefix}{agent[-5:]}-{i}",
                        agent,
                        f"title {i}",
                        f"body {i}",
                        "sum",
                        [0.5] * 1024,
                    ),
                )

    def snapshot():
        return conn.execute(
            "SELECT id, agent, md5(row(title, content, summary, layer, tags, memory_json::text)::text), "
            "embedding::text FROM memories WHERE id LIKE %s ORDER BY id",
            (prefix + "%",),
        ).fetchall()

    before = snapshot()
    # dry run: nothing changes, no embedder needed
    dry = rb.reembed(conn, AGENT, None, apply=False, batch_size=3)
    assert dry["updated"] == 0 and snapshot() == before

    calls = []

    def embed(text):
        calls.append(text)
        return [0.25] * 1024

    rep = rb.reembed(
        conn,
        AGENT,
        embed,
        apply=True,
        batch_size=3,
        rows_per_sec=0,
        state_path=tmp_path / "state.json",
    )
    after = snapshot()
    mine = [r for r in after if r[1] == AGENT]
    theirs_before = [r for r in before if r[1] == other]
    assert rep["updated"] >= 7
    assert all(r[3] != [0.5] and "0.25" in r[3] for r in mine)
    assert [r for r in after if r[1] == other] == theirs_before, "another agent must not change"
    # every non-embedding column is byte-identical
    assert [(r[0], r[2]) for r in after] == [(r[0], r[2]) for r in before]
    assert any(c.startswith("title ") and "\nbody " in c and c.endswith("sum") for c in calls)
