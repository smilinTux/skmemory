"""Regression: non-Chef readers of the shared skmem-pg `docs` table must not
leak @chef-only / private rows (card 7d6e91e7, SECURITY/privacy).

skingest tags sacred / --private ingests in `docs.meta`
(`meta->>'private'='true'`, `meta->>'context_tag'='@chef-only'`) and exposes a
`docs_public` VIEW that drops those rows server-side. The shipped
`hybrid_search_docs` SQL function ranks over the BASE `docs` table, so it returns
private content to ANY direct caller -> it is a PRIVILEGED (Chef-authorized)
reader. Per skingest SECURITY.md, every NON-Chef reader (skmemory pgvector,
dashboards, ad-hoc SQL) must instead read `docs_public`.

This card adds the fail-closed sibling `hybrid_search_docs_public`, which ranks
every leg over `docs_public`. These tests prove:

  1. STRUCTURAL (DB-independent, always runs): the deploy SQL defines
     `hybrid_search_docs_public` and it reads `docs_public`, never base `docs`.
     On origin/main (no such function) this FAILS -> fail-before / pass-after.

  2. BEHAVIOURAL (needs a live skmem-pg; skipped otherwise): with a real
     @chef-only row planted so it would rank #1 by vector similarity,
     `hybrid_search_docs_public` DOES NOT return it (leak closed) while the
     legitimate public row IS returned; the privileged `hybrid_search_docs`
     STILL returns the private row (Chef's full-access path is preserved).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

DEPLOY_SQL = (
    Path(__file__).resolve().parent.parent / "deploy" / "skmem-pg" / "03-cutover-mxbai.sql"
)

# Isolated scratch scope so the test never touches real corpus rows.
PROBE_CORPUS = "skmemory-privacy-probe"
PROBE_AGENT = "privacyprobe"
PROBE_TERM = "zzqprivacyprobe"  # unique BM25 term shared by both planted rows

DSN = os.environ.get("SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory")


def _extract_public_fn_ddl() -> str | None:
    """Return the `hybrid_search_docs_public` CREATE block from the deploy SQL,
    or None if it is not defined (the origin/main / pre-fix state)."""
    if not DEPLOY_SQL.exists():
        return None
    text = DEPLOY_SQL.read_text(encoding="utf-8")
    # Grab the (optional DROP +) CREATE FUNCTION ... $$ ... $$ ... ; block.
    m = re.search(
        r"(?:DROP FUNCTION IF EXISTS hybrid_search_docs_public[^\n]*\n)?"
        r"CREATE FUNCTION hybrid_search_docs_public.*?\$\$\s*LANGUAGE sql STABLE;",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# 1) STRUCTURAL: runs everywhere, fails on origin/main (fail-before)           #
# --------------------------------------------------------------------------- #


def test_public_reader_function_is_defined():
    """The fail-closed public reader must exist in the deploy SQL."""
    ddl = _extract_public_fn_ddl()
    assert ddl is not None, (
        "hybrid_search_docs_public is not defined in deploy/skmem-pg/"
        "03-cutover-mxbai.sql -- non-Chef readers have no fail-closed docs "
        "reader and the @chef-only leak is OPEN (card 7d6e91e7)."
    )


def test_public_reader_targets_docs_public_not_base_docs():
    """Every FROM in the public reader must be `docs_public`, never base `docs`.

    This is the crux of the fix: reading base `docs` re-exposes @chef-only /
    private rows. The privileged `hybrid_search_docs` may read base docs, but the
    _public sibling must not.
    """
    ddl = _extract_public_fn_ddl()
    assert ddl is not None, "hybrid_search_docs_public missing (see previous test)"
    # No bare `FROM docs` / `JOIN docs` (base table). `docs_public` is allowed.
    base_refs = re.findall(r"\b(?:FROM|JOIN)\s+docs\b(?!_public)", ddl, re.IGNORECASE)
    assert not base_refs, (
        f"hybrid_search_docs_public reads the BASE docs table {base_refs} -- it "
        "must read docs_public so @chef-only / private rows are excluded."
    )
    assert re.search(r"\bFROM\s+docs_public\b", ddl, re.IGNORECASE), (
        "hybrid_search_docs_public must rank over the docs_public view."
    )


# --------------------------------------------------------------------------- #
# 2) BEHAVIOURAL: needs a live skmem-pg                                        #
# --------------------------------------------------------------------------- #


def _connect():
    try:
        import psycopg
    except Exception:  # pragma: no cover - dep missing
        return None
    try:
        return psycopg.connect(DSN, connect_timeout=3, autocommit=True)
    except Exception:
        return None


def _vec_literal(hot_index: int, dim: int = 1024) -> str:
    v = ["0"] * dim
    v[hot_index] = "1"
    return "[" + ",".join(v) + "]"


@pytest.fixture()
def live_conn():
    conn = _connect()
    if conn is None:
        pytest.skip("live skmem-pg not reachable (SKMEMORY_PG_DSN) -- skip behavioural leak test")
    # Ensure the fix is applied to this DB (idempotent), sourced from the deploy SQL.
    ddl = _extract_public_fn_ddl()
    if ddl is None:
        pytest.fail("hybrid_search_docs_public not defined in deploy SQL -- leak open")
    # docs_public view must exist (owned by skingest); if absent the whole gate is moot.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.views WHERE table_name='docs_public'")
        if cur.fetchone() is None:
            conn.close()
            pytest.skip(
                "docs_public view absent on this skmem-pg -- skingest privacy gate not installed"
            )
        cur.execute(ddl)
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM docs WHERE corpus=%s", (PROBE_CORPUS,))
    conn.close()


def _plant_rows(conn) -> tuple[int, int]:
    """Insert a public row and an @chef-only private row; return (public_id, private_id)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM docs WHERE corpus=%s", (PROBE_CORPUS,))
        # public row -> vector hot at index 1
        cur.execute(
            "INSERT INTO docs (corpus, source, chunk_idx, content, meta, agent, embedding) "
            "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::vector) RETURNING id",
            (
                PROBE_CORPUS,
                "public.md",
                0,
                f"public probe note {PROBE_TERM} harmless",
                "{}",
                PROBE_AGENT,
                _vec_literal(1),
            ),
        )
        public_id = cur.fetchone()[0]
        # private @chef-only row -> vector hot at index 0 (== the query vector below)
        cur.execute(
            "INSERT INTO docs (corpus, source, chunk_idx, content, meta, agent, embedding) "
            "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::vector) RETURNING id",
            (
                PROBE_CORPUS,
                "sacred.md",
                0,
                f"private probe secret {PROBE_TERM} chef-only",
                '{"private":"true","context_tag":"@chef-only"}',
                PROBE_AGENT,
                _vec_literal(0),
            ),
        )
        private_id = cur.fetchone()[0]
    return public_id, private_id


def _call(conn, fn: str) -> list[int]:
    # Query vector == the PRIVATE row's embedding, so the private row is the
    # single nearest neighbour: the strongest possible leak for a docs reader.
    qvec = _vec_literal(0)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id FROM {fn}(%s, %s::vector, %s, %s, %s, %s)",
            (PROBE_TERM, qvec, 10, PROBE_AGENT, 60, 2.0),
        )
        return [r[0] for r in cur.fetchall()]


def test_public_reader_does_not_leak_chef_only_row(live_conn):
    """hybrid_search_docs_public must drop the @chef-only row and keep the public one."""
    public_id, private_id = _plant_rows(live_conn)

    ids = _call(live_conn, "hybrid_search_docs_public")

    assert private_id not in ids, (
        "LEAK: hybrid_search_docs_public returned the @chef-only private row "
        f"(id={private_id}). Non-Chef readers must never see private docs."
    )
    assert public_id in ids, (
        "hybrid_search_docs_public dropped the legitimate PUBLIC row "
        f"(id={public_id}); the fail-closed reader must still return public docs."
    )


def test_privileged_reader_still_sees_chef_only_row(live_conn):
    """The privileged hybrid_search_docs must STILL return the private row.

    Fail-closed must not mean fail-broken: Chef's full-access path is preserved.
    This also demonstrates the fail-BEFORE behaviour -- base `docs` (what the only
    reader used to be) does surface the @chef-only row, which is exactly the leak
    the _public reader closes.
    """
    _public_id, private_id = _plant_rows(live_conn)

    ids = _call(live_conn, "hybrid_search_docs")

    assert private_id in ids, (
        "privileged hybrid_search_docs did NOT return the @chef-only row "
        f"(id={private_id}); Chef's authorized full-access path is broken."
    )
