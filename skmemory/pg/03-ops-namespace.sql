-- =====================================================================
-- 03-ops-namespace.sql  --  skbrain OPERATIONS namespace (card SB0.3)
-- =====================================================================
-- Epic: skbrain ops/wiki/ITIL/CMDB (b29656b3), spec
--   ~/clawd/docs/superpowers/specs/2026-07-31-skbrain-ops-wiki-itil-cmdb-architecture.md
--   section 4.3 (schema-per-namespace decision) + 4.4 (DDL contract) + 8/9 (privacy wall).
--
-- WHAT THIS DOES: creates a dedicated `ops` Postgres SCHEMA inside the EXISTING
-- skmem-pg instance (localhost:5432, image skmem-pg:pg17-bm25-age) for the skbrain
-- operations corpus, cloning the PROVEN shapes from the public `docs` corpus:
--   * ops.wiki_nodes / ops.wiki_chunks / ops.links  (mirror docs/chunks/links)
--   * own pgvector HNSW index + own pg_search BM25 index on ops.wiki_chunks
--     (per-table, so ops ranking statistics NEVER mix with public docs -- the
--      spec's load-bearing reason for schema separation, 4.3(1))
--   * a dedicated AGE graph `ops_brain` (its own schema, exactly like
--     lumina_knowledge/opus_knowledge today)
--   * hybrid_search_ops(...) -- the RRF sibling of public.hybrid_search_docs,
--     both legs scoped to ops.wiki_chunks
--   * the PRIVACY WALL: REVOKE the ops + ops_brain schemas from PUBLIC and grant
--     only to the ops read/write roles, so public aggregate readers
--     (docs_public, hybrid_search_docs_public, dashboards) structurally cannot
--     read ops rows (4.3(2), section 9.1).
--
-- ADDITIVE + IDEMPOTENT: nothing in `public` changes; every statement is guarded
-- (IF NOT EXISTS / CREATE OR REPLACE / catalog checks) so a re-run is a no-op.
--
-- Embedding dimension: vector(1024) -- mxbai-embed-large, matching the LIVE
-- public.docs.embedding / public.memories.embedding columns (schema.sql).
--
-- APPLY (card OPS1.3): auto-applied on a FRESH compose boot via migrations.txt
-- + initdb/00-run-init.sh (no live data on first initdb). On a LIVE node it is
-- operator-initiated, .158-first, AFTER a pg_dump, via `skmemory pg migrate`
-- (pre-dump + apply + verify in one guarded command). See README.md
-- ("The ops namespace migration"). Do NOT auto-apply from CI.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- Operators/types we reference (vector in public, pg_search in paradedb, AGE in
-- ag_catalog). Put them on the path so the function body + BM25 index resolve
-- `@@@`, paradedb.score, and the vector ops the same way the shipped migrations do.
SET LOCAL search_path = ops, public, paradedb, ag_catalog;

-- ---------------------------------------------------------------------
-- 0. Roles (idempotent). The public aggregate readers run as PUBLIC / the
--    default login role; ops gets its OWN least-privilege roles so the wall
--    below is enforceable. NOLOGIN group roles, granted to real logins by the
--    operator out-of-band (GRANT skbrain_ops_rw TO <projector_login>; etc).
--      skbrain_ops_rw  = the projector (skbrain sync): read+write ops.
--      skbrain_ops_ro  = operator / Atlas retrieval: read-only ops.
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skbrain_ops_rw') THEN
    CREATE ROLE skbrain_ops_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skbrain_ops_ro') THEN
    CREATE ROLE skbrain_ops_ro NOLOGIN;
  END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 1. Schema
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------------
-- 2. Tables (mirror public.docs / chunks / links shapes, ops-typed)
-- ---------------------------------------------------------------------

-- One row per canon page OR projected state record (spec 4.4).
CREATE TABLE IF NOT EXISTS ops.wiki_nodes (
    id           text PRIMARY KEY,                 -- stable slug / record id (runbook-*, ke-*, ci id, inc-*)
    kind         text NOT NULL,                    -- runbook|ci|service|node|known-error|postmortem|synthesis|incident|problem|change
    origin       text NOT NULL DEFAULT 'git',      -- git|repo|itil|cmdb  (canon vs projected state)
    title        text NOT NULL,
    lifecycle    text NOT NULL DEFAULT 'canon',     -- draft|reviewed|canon
    frontmatter  jsonb NOT NULL DEFAULT '{}'::jsonb,
    body_md      text NOT NULL DEFAULT '',
    content_hash text NOT NULL DEFAULT '',          -- idempotence key for the projector
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ops_wiki_nodes_origin_chk
        CHECK (origin IN ('git', 'repo', 'itil', 'cmdb')),
    CONSTRAINT ops_wiki_nodes_lifecycle_chk
        CHECK (lifecycle IN ('draft', 'reviewed', 'canon'))
);

-- Retrieval unit: <=512 mxbai tokens per chunk (chunk.py discipline). Embedding
-- column sized to the LIVE embedding dim (vector(1024), == public.docs).
CREATE TABLE IF NOT EXISTS ops.wiki_chunks (
    id         bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    node_id    text NOT NULL REFERENCES ops.wiki_nodes(id) ON DELETE CASCADE,
    ord        int  NOT NULL,
    content    text NOT NULL,
    embedding  public.vector(1024),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ops_wiki_chunks_node_ord_uq UNIQUE (node_id, ord)
);

-- Resolved wikilinks + typed edges: the cheap relational mirror of the graph
-- (hot backlinks/drift queries). `provenance` = definition|observed so
-- definition-vs-observed drift is a plain WHERE clause (spec 4.4 note, 5.3).
CREATE TABLE IF NOT EXISTS ops.links (
    src        text NOT NULL,
    dst        text NOT NULL,
    edge_type  text NOT NULL DEFAULT 'links_to',
    provenance text NOT NULL DEFAULT 'definition',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ops_links_provenance_chk
        CHECK (provenance IN ('definition', 'observed')),
    PRIMARY KEY (src, dst, edge_type, provenance)
);

-- ---------------------------------------------------------------------
-- 3. Indexes (per-table -> ops ranking stats never mix with public docs)
-- ---------------------------------------------------------------------

-- pgvector HNSW on ops chunk embeddings (mirrors public.docs_emb_mxbai_hnsw).
CREATE INDEX IF NOT EXISTS ops_chunks_hnsw
    ON ops.wiki_chunks USING hnsw (embedding public.vector_cosine_ops);

-- pg_search BM25 over ops content ONLY -> ops-only corpus statistics / IDF
-- (mirrors public.docs_bm25; this per-table isolation is the whole point of 4.3(1)).
CREATE INDEX IF NOT EXISTS ops_chunks_bm25
    ON ops.wiki_chunks USING bm25 (id, content)
    WITH (key_field='id', text_fields='{
       "content": {"tokenizer": {"type": "default", "stemmer": "English", "stopwords_language": "English"}}}');

-- Helper btrees (backlinks + kind filter + FK).
CREATE INDEX IF NOT EXISTS ops_chunks_node ON ops.wiki_chunks USING btree (node_id);
CREATE INDEX IF NOT EXISTS ops_nodes_kind  ON ops.wiki_nodes  USING btree (kind);
CREATE INDEX IF NOT EXISTS ops_links_dst   ON ops.links       USING btree (dst);
CREATE INDEX IF NOT EXISTS ops_links_type  ON ops.links       USING btree (edge_type, provenance);

-- ---------------------------------------------------------------------
-- 4. hybrid_search_ops -- RRF sibling of public.hybrid_search_docs, scoped to
--    ops.wiki_chunks joined to ops.wiki_nodes for kind filtering. Same RRF shape
--    (vec_w-weighted pgvector leg + BM25 leg, rrf_k smoothing) as the proven
--    docs function; INVOKER security so the schema-level role wall (section 5)
--    applies to callers of this function too.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.hybrid_search_ops(
        q_text      text,
        q_vec       public.vector,
        k           int   DEFAULT 10,
        kind_filter text  DEFAULT NULL,
        rrf_k       int   DEFAULT 60,
        vec_w       float DEFAULT 2.0)
RETURNS TABLE(id bigint, node_id text, kind text, title text, content text,
              vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> q_vec) AS rnk
  FROM ops.wiki_chunks c JOIN ops.wiki_nodes n ON n.id = c.node_id
  WHERE q_vec IS NOT NULL AND c.embedding IS NOT NULL
    AND (kind_filter IS NULL OR n.kind = kind_filter)
  ORDER BY c.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT c.id, row_number() OVER (ORDER BY paradedb.score(c.id) DESC) AS rnk
  FROM ops.wiki_chunks c JOIN ops.wiki_nodes n ON n.id = c.node_id
  WHERE c.content @@@ q_text
    AND (kind_filter IS NULL OR n.kind = kind_filter)
  ORDER BY paradedb.score(c.id) DESC LIMIT 100)
SELECT c.id, c.node_id, n.kind, n.title, left(c.content, 160),
       vec.rnk::int, bm.rnk::int,
       (vec_w * COALESCE(1.0/(rrf_k+vec.rnk), 0) + COALESCE(1.0/(rrf_k+bm.rnk), 0))::float
FROM ops.wiki_chunks c JOIN ops.wiki_nodes n ON n.id = c.node_id
     LEFT JOIN vec ON vec.id = c.id
     LEFT JOIN bm  ON bm.id  = c.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL
ORDER BY 8 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- ---------------------------------------------------------------------
-- 5. AGE graph `ops_brain` (its own schema, like lumina_knowledge). Idempotent
--    via the AGE catalog check; vertices/edges are created lazily by the
--    projector (section 3.3 vocabulary).
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'ops_brain') THEN
    PERFORM ag_catalog.create_graph('ops_brain');
  END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 6. PRIVACY WALL (spec 4.3(2), section 9.1)
--    ops content is structurally invisible to the public aggregate readers:
--    it is not in the tables they query (docs_public / hybrid_search_docs_public
--    read public.docs only), AND role grants make an ad-hoc query fail too.
-- ---------------------------------------------------------------------

-- Relational namespace: strip PUBLIC, grant only the ops roles.
REVOKE ALL ON SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM PUBLIC;

GRANT USAGE ON SCHEMA ops TO skbrain_ops_ro, skbrain_ops_rw;

GRANT SELECT ON ALL TABLES IN SCHEMA ops TO skbrain_ops_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops TO skbrain_ops_rw;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ops TO skbrain_ops_rw;
GRANT EXECUTE ON FUNCTION
      ops.hybrid_search_ops(text, public.vector, int, text, int, float)
      TO skbrain_ops_ro, skbrain_ops_rw;

-- Future objects created by the projector inherit the same wall.
ALTER DEFAULT PRIVILEGES FOR ROLE skbrain_ops_rw IN SCHEMA ops
      REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE skbrain_ops_rw IN SCHEMA ops
      GRANT SELECT ON TABLES TO skbrain_ops_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE skbrain_ops_rw IN SCHEMA ops
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO skbrain_ops_rw;

-- Graph namespace (ops_brain schema, its label tables + future vlabel/elabel).
REVOKE ALL ON SCHEMA ops_brain FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA ops_brain FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ops_brain FROM PUBLIC;
GRANT USAGE ON SCHEMA ops_brain TO skbrain_ops_ro, skbrain_ops_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops_brain TO skbrain_ops_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE skbrain_ops_rw IN SCHEMA ops_brain
      REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE skbrain_ops_rw IN SCHEMA ops_brain
      GRANT SELECT ON TABLES TO skbrain_ops_ro;

COMMENT ON SCHEMA ops IS
  'skbrain OPERATIONS namespace (card SB0.3): runbook/CI/service/node/KEDB wiki '
  'projection + ITIL/CMDB state mirror. Private: REVOKED from PUBLIC, granted only '
  'to skbrain_ops_ro/rw. Public aggregate readers cannot see these rows.';

COMMIT;

-- Post-apply verification lives in README.md ("verify" step). Nothing here runs
-- outside the operator apply path.
