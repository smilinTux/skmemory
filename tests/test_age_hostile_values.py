"""Values can never alter an AGE query: they travel only in the agtype param map (SKMEM-P0-24)."""

from __future__ import annotations

import os

import pytest

from skmemory.backends.age_backend import AGEGraphBackend, GraphTransportError

DSN = os.environ.get("SKMEMORY_PG_DSN", "")

HOSTILE = [
    "a$$b",
    "$$ ) RETURN 1 $$",
    "x$tag$y",
    'it\'s "q" \\ back',
    "100% %s %(x)s {id: $c}",
    "line\nbreak\ttab",
    "x'}) DETACH DELETE (m) //",
    "é☃\U0001f600",
]


def _available() -> bool:
    if not DSN:
        return False
    try:
        import psycopg

        with psycopg.connect(DSN, autocommit=True, connect_timeout=3) as c:
            c.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="needs throwaway skmem-pg with AGE")


@pytest.fixture()
def backend():
    graph = f"p024_hostile_{os.getpid()}"
    b = AGEGraphBackend(dsn=DSN, agent="lumina", graph=graph)
    b._conn().execute(f"SELECT create_graph('{graph}')")
    yield b
    b._conn().execute(f"SELECT drop_graph('{graph}', true)")
    b.reset_connection()


def test_hostile_ids_convert_and_never_inject(backend):
    sentinel = "sentinel-node"
    backend._cypher_strict("CREATE (:Memory {id: $i}) RETURN 1", {"i": sentinel}, cols="r agtype")
    for n, bad in enumerate(HOSTILE):
        child, parent = f"c{n}-{bad}", f"p{n}-{bad}"
        backend._cypher_strict(
            "CREATE (c:Memory {id: $c}) CREATE (p:Memory {id: $p}) "
            "CREATE (c)-[:SUPERSEDES {recorded_at: $t, valid_from: $t, valid_to: $t}]->(p) "
            "RETURN 1",
            {"c": child, "p": parent, "t": bad},
            cols="r agtype",
        )
        assert backend.convert_supersedes_to_lineage(child, parent, "PROMOTED_FROM")
    assert backend.supersedes_edges() == []
    rows = backend._cypher_strict(
        "MATCH ()-[e:PROMOTED_FROM]->() RETURN count(e)", cols="c agtype"
    )
    assert int(backend._agtype(rows[0][0])) == len(HOSTILE)
    # the DETACH DELETE payload did not run: every node is still there
    rows = backend._cypher_strict("MATCH (m:Memory) RETURN count(m)", cols="c agtype")
    assert int(backend._agtype(rows[0][0])) == 2 * len(HOSTILE) + 1


def test_dollar_quote_in_body_is_refused(backend):
    with pytest.raises(GraphTransportError):
        backend._cypher_strict("RETURN 1 $$ ; SELECT 1 ; $$", cols="r agtype")
