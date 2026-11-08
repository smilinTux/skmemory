-- Run AFTER swapping skmem-pg to the pg17-bm25-age image (shared_preload_libraries=pg_search,age)
-- Safe/additive: creates extensions, BM25 indexes, real-BM25 hybrid functions.

CREATE EXTENSION IF NOT EXISTS pg_search;   -- ParadeDB BM25 (@@@ operator, paradedb.score)
CREATE EXTENSION IF NOT EXISTS age;         -- Apache AGE graph
CREATE EXTENSION IF NOT EXISTS vector;      -- (already present)

-- ---- BM25 indexes (real Okapi BM25) ----
-- docs: integer id -> straightforward key_field
CREATE INDEX IF NOT EXISTS docs_bm25 ON docs
  USING bm25 (id, content, corpus, source)
  WITH (key_field='id');

-- memories: text id (pg_search supports text key fields in 0.24)
CREATE INDEX IF NOT EXISTS memories_bm25 ON memories
  USING bm25 (id, title, content, summary)
  WITH (key_field='id');

-- ---- Real-BM25 hybrid (RRF of pgvector + pg_search) for DOCS ----
CREATE OR REPLACE FUNCTION hybrid_search_docs(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60)
RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT d.id, row_number() OVER (ORDER BY d.embedding <=> q_vec) AS rnk
  FROM docs d WHERE q_vec IS NOT NULL AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY d.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT d.id, row_number() OVER (ORDER BY paradedb.score(d.id) DESC) AS rnk
  FROM docs d WHERE d.content @@@ q_text AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY paradedb.score(d.id) DESC LIMIT 100)
SELECT d.id, d.corpus, d.source, left(d.content,160),
       vec.rnk::int, bm.rnk::int,
       (COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM docs d LEFT JOIN vec ON vec.id=d.id LEFT JOIN bm ON bm.id=d.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL
ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- ---- FAIL-CLOSED public reader over docs (card 7d6e91e7) ----
-- hybrid_search_docs above ranks over the BASE `docs` table, which still holds
-- @chef-only / private rows -> it is a PRIVILEGED (Chef) reader. Per skingest
-- SECURITY.md, every NON-Chef reader of the shared docs table (skmemory pgvector,
-- dashboards, ad-hoc SQL) MUST read `docs_public`, never base `docs`. This sibling
-- ranks every leg over the `docs_public` VIEW, which excludes private / @chef-only
-- rows server-side, so a private row can never enter its result set. Fail-closed:
-- use this unless you are on an explicitly Chef-authorized path.
CREATE OR REPLACE FUNCTION hybrid_search_docs_public(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60)
RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT d.id, row_number() OVER (ORDER BY d.embedding <=> q_vec) AS rnk
  FROM docs_public d WHERE q_vec IS NOT NULL AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY d.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT d.id, row_number() OVER (ORDER BY paradedb.score(d.id) DESC) AS rnk
  FROM docs_public d WHERE d.content @@@ q_text AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY paradedb.score(d.id) DESC LIMIT 100)
SELECT d.id, d.corpus, d.source, left(d.content,160),
       vec.rnk::int, bm.rnk::int,
       (COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM docs_public d LEFT JOIN vec ON vec.id=d.id LEFT JOIN bm ON bm.id=d.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL
ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- ---- Real-BM25 hybrid for MEMORIES ----
CREATE OR REPLACE FUNCTION hybrid_search_memories(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60)
RETURNS TABLE(id text, layer text, title text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT m.id, row_number() OVER (ORDER BY m.embedding <=> q_vec) AS rnk
  FROM memories m WHERE q_vec IS NOT NULL AND (agent_filter IS NULL OR m.agent = agent_filter)
  ORDER BY m.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT m.id, row_number() OVER (ORDER BY paradedb.score(m.id) DESC) AS rnk
  FROM memories m WHERE m.content @@@ q_text AND (agent_filter IS NULL OR m.agent = agent_filter)
  ORDER BY paradedb.score(m.id) DESC LIMIT 100)
SELECT m.id, m.layer, m.title, left(m.content,160),
       vec.rnk::int, bm.rnk::int,
       (COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM memories m LEFT JOIN vec ON vec.id=m.id LEFT JOIN bm ON bm.id=m.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL
ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;
