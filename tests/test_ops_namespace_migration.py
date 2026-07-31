"""Structural tests for the skbrain ops-namespace migration (card SB0.3, epic
b29656b3).

`deploy/skmem-pg/03-ops-namespace.sql` adds a dedicated `ops` Postgres schema +
`ops_brain` AGE graph to the existing skmem-pg instance for the skbrain operations
corpus, cloning the proven public `docs` shapes with a REVOKE-from-PUBLIC privacy
wall (spec 2026-07-31-skbrain-ops-wiki-itil-cmdb-architecture.md sections 4.3, 4.4,
8, 9).

These are DB-INDEPENDENT (always run in CI). They assert the migration defines the
right objects, that `hybrid_search_ops` is scoped to the ops schema (never public
docs), that the embedding dim matches the live 1024-dim mxbai vectors, that the
statements are idempotent, and that the privacy wall REVOKEs the ops schema(s) from
PUBLIC. Behavioural (live-DB) validation is the operator apply/verify step in
deploy/skmem-pg/README.md, deliberately not automated: the migration is never
applied from CI.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "skmem-pg"
    / "03-ops-namespace.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), f"migration file missing: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Objects created                                                             #
# --------------------------------------------------------------------------- #


def test_creates_ops_schema_idempotently():
    assert re.search(r"CREATE SCHEMA IF NOT EXISTS ops\b", _sql(), re.IGNORECASE)


def test_creates_three_core_tables():
    sql = _sql()
    for tbl in ("ops.wiki_nodes", "ops.wiki_chunks", "ops.links"):
        assert re.search(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(tbl)}\b", sql, re.IGNORECASE
        ), f"{tbl} not created (idempotently)"


def test_wiki_nodes_has_provenance_and_frontmatter_and_timestamps():
    sql = _sql()
    # origin (git|repo|itil|cmdb provenance), frontmatter jsonb, timestamps.
    assert re.search(r"origin\s+text", sql, re.IGNORECASE)
    assert re.search(r"frontmatter\s+jsonb", sql, re.IGNORECASE)
    assert re.search(r"content_hash\s+text", sql, re.IGNORECASE)
    assert re.search(r"created_at\s+timestamptz", sql, re.IGNORECASE)
    assert re.search(r"updated_at\s+timestamptz", sql, re.IGNORECASE)


def test_links_has_definition_observed_provenance():
    sql = _sql()
    assert re.search(r"provenance\s+text", sql, re.IGNORECASE)
    assert re.search(
        r"provenance\s+IN\s*\(\s*'definition'\s*,\s*'observed'\s*\)", sql, re.IGNORECASE
    ), "ops.links.provenance must be constrained to definition|observed"


def test_embedding_dim_matches_live_mxbai_1024():
    """ops.wiki_chunks.embedding must be vector(1024) -- the live mxbai dim."""
    sql = _sql()
    assert re.search(r"embedding\s+public\.vector\(1024\)", sql, re.IGNORECASE), (
        "ops chunk embedding must be public.vector(1024), matching public.docs"
    )


# --------------------------------------------------------------------------- #
# Per-table indexes (the reason for schema separation, spec 4.3(1))           #
# --------------------------------------------------------------------------- #


def test_own_hnsw_index_on_ops_chunks():
    sql = _sql()
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS ops_chunks_hnsw\s+ON ops\.wiki_chunks\s+USING hnsw",
        sql,
        re.IGNORECASE,
    ), "ops.wiki_chunks needs its own pgvector HNSW index"


def test_own_bm25_index_on_ops_chunks():
    sql = _sql()
    m = re.search(
        r"CREATE INDEX IF NOT EXISTS ops_chunks_bm25\s+ON ops\.wiki_chunks\s+USING bm25.*?key_field='id'",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, "ops.wiki_chunks needs its own pg_search BM25 index (key_field='id')"


# --------------------------------------------------------------------------- #
# hybrid_search_ops: RRF sibling scoped to the ops schema, never public docs   #
# --------------------------------------------------------------------------- #


def _hybrid_ddl() -> str:
    m = re.search(
        r"CREATE OR REPLACE FUNCTION ops\.hybrid_search_ops.*?\$\$\s*LANGUAGE sql STABLE;",
        _sql(),
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "ops.hybrid_search_ops not defined"
    return m.group(0)


def test_hybrid_search_ops_defined():
    _hybrid_ddl()  # asserts presence


def test_hybrid_search_ops_reads_only_ops_chunks_never_public_docs():
    """Every FROM/JOIN in the function body targets ops.wiki_chunks/ops.wiki_nodes;
    it must never rank over public.docs (that would defeat the isolation)."""
    ddl = _hybrid_ddl()
    # No reference to the public docs corpus.
    assert not re.search(r"\b(?:FROM|JOIN)\s+docs\b", ddl, re.IGNORECASE), (
        "hybrid_search_ops must not read the public docs table"
    )
    assert not re.search(r"\bdocs_public\b", ddl, re.IGNORECASE)
    assert re.search(r"\bFROM ops\.wiki_chunks\b", ddl, re.IGNORECASE)
    # RRF shape carried over from hybrid_search_docs (rrf_k smoothing + vec weight).
    assert "rrf_k" in ddl and "vec_w" in ddl
    assert "paradedb.score" in ddl and "@@@" in ddl


def test_hybrid_search_ops_supports_kind_filter():
    ddl = _hybrid_ddl()
    assert re.search(r"kind_filter\s+text", ddl, re.IGNORECASE), (
        "hybrid_search_ops must accept a kind_filter (e.g. runbook)"
    )


# --------------------------------------------------------------------------- #
# AGE graph                                                                   #
# --------------------------------------------------------------------------- #


def test_creates_ops_brain_graph_idempotently():
    sql = _sql()
    assert re.search(r"create_graph\('ops_brain'\)", sql, re.IGNORECASE)
    # Guarded against re-run via the AGE catalog.
    assert re.search(r"ag_catalog\.ag_graph WHERE name = 'ops_brain'", sql), (
        "create_graph('ops_brain') must be guarded by an ag_graph catalog check"
    )


# --------------------------------------------------------------------------- #
# Privacy wall (spec 4.3(2), section 9.1)                                     #
# --------------------------------------------------------------------------- #


def test_revokes_ops_schema_from_public():
    sql = _sql()
    assert re.search(r"REVOKE ALL ON SCHEMA ops FROM PUBLIC", sql, re.IGNORECASE), (
        "the ops schema must be REVOKEd from PUBLIC so public readers cannot see it"
    )
    assert re.search(
        r"REVOKE ALL ON SCHEMA ops_brain FROM PUBLIC", sql, re.IGNORECASE
    ), "the ops_brain graph schema must be REVOKEd from PUBLIC too"


def test_grants_only_to_ops_roles():
    sql = _sql()
    # Grant USAGE only to the dedicated ops roles, not PUBLIC.
    assert re.search(
        r"GRANT USAGE ON SCHEMA ops TO skbrain_ops_ro, skbrain_ops_rw", sql, re.IGNORECASE
    )
    # Writer role gets write; reader role read-only.
    assert re.search(
        r"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops TO skbrain_ops_rw",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"GRANT SELECT ON ALL TABLES IN SCHEMA ops TO skbrain_ops_ro", sql, re.IGNORECASE
    )
    assert not re.search(
        r"GRANT[^;]*\bTO PUBLIC\b", sql, re.IGNORECASE
    ), "nothing in ops may be granted to PUBLIC"


def test_roles_created_idempotently():
    sql = _sql()
    for role in ("skbrain_ops_rw", "skbrain_ops_ro"):
        assert re.search(
            rf"pg_roles WHERE rolname = '{role}'", sql
        ), f"role {role} must be created idempotently (pg_roles guard)"


# --------------------------------------------------------------------------- #
# Idempotence + transaction safety                                           #
# --------------------------------------------------------------------------- #


def test_migration_is_additive_and_transactional():
    sql = _sql()
    assert "\\set ON_ERROR_STOP on" in sql, "must abort on first error"
    assert re.search(r"^BEGIN;", sql, re.MULTILINE) and re.search(
        r"^COMMIT;", sql, re.MULTILINE
    ), "migration must be wrapped in a single transaction"
    # No destructive ops against the public corpus.
    assert not re.search(r"DROP TABLE\s+public\.", sql, re.IGNORECASE)
    assert not re.search(r"ALTER TABLE\s+public\.", sql, re.IGNORECASE)


def test_dollar_quotes_balanced():
    """Cheap structural sanity: $$ blocks and BEGIN/COMMIT are balanced."""
    sql = _sql()
    assert sql.count("$$") % 2 == 0, "unbalanced $$ dollar-quote blocks"
    assert sql.count("BEGIN;") == sql.count("COMMIT;")
