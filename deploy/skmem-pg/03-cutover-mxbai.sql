-- CUTOVER: make mxbai the primary `embedding` column (code/queries read `embedding`);
-- keep bge vectors as emb_bge_legal backup. Run AFTER memories emb_mxbai is populated.
\set ON_ERROR_STOP on
BEGIN;
-- DOCS
ALTER TABLE docs    RENAME COLUMN embedding   TO emb_bge_legal;
ALTER TABLE docs    RENAME COLUMN emb_mxbai   TO embedding;
-- MEMORIES
ALTER TABLE memories RENAME COLUMN embedding  TO emb_bge_legal;
ALTER TABLE memories RENAME COLUMN emb_mxbai  TO embedding;
COMMIT;

-- repoint hybrid_search_docs to `embedding` (now = mxbai) + keep vector-weighted RRF
DROP FUNCTION IF EXISTS hybrid_search_docs(text,vector,integer,text,integer,double precision);
CREATE FUNCTION hybrid_search_docs(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60, vec_w float DEFAULT 2.0)
RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT d.id, row_number() OVER (ORDER BY d.embedding <=> q_vec) AS rnk
  FROM docs d WHERE q_vec IS NOT NULL AND d.embedding IS NOT NULL AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY d.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT d.id, row_number() OVER (ORDER BY paradedb.score(d.id) DESC) AS rnk
  FROM docs d WHERE d.content @@@ q_text AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY paradedb.score(d.id) DESC LIMIT 100)
SELECT d.id, d.corpus, d.source, left(d.content,160), vec.rnk::int, bm.rnk::int,
       (vec_w*COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM docs d LEFT JOIN vec ON vec.id=d.id LEFT JOIN bm ON bm.id=d.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- FAIL-CLOSED public reader (card 7d6e91e7). hybrid_search_docs above ranks over
-- the BASE `docs` table, which still contains @chef-only / private rows, so it is
-- a PRIVILEGED (Chef-authorized) reader. Per skingest SECURITY.md consumer
-- guidance, every NON-Chef reader of the shared docs table (skmemory pgvector,
-- dashboards, ad-hoc SQL, any future consumer) MUST read the `docs_public` VIEW,
-- never base `docs`. hybrid_search_docs_public is the fail-closed sibling: same
-- signature + columns, but every leg (vector, BM25, final projection) ranks over
-- `docs_public`, which excludes private / @chef-only rows server-side. A private
-- row can never enter its result set. Use THIS unless you are on an explicitly
-- Chef-authorized path (then call hybrid_search_docs, which sees base docs).
DROP FUNCTION IF EXISTS hybrid_search_docs_public(text,vector,integer,text,integer,double precision);
CREATE FUNCTION hybrid_search_docs_public(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60, vec_w float DEFAULT 2.0)
RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (
  SELECT d.id, row_number() OVER (ORDER BY d.embedding <=> q_vec) AS rnk
  FROM docs_public d WHERE q_vec IS NOT NULL AND d.embedding IS NOT NULL AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY d.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT d.id, row_number() OVER (ORDER BY paradedb.score(d.id) DESC) AS rnk
  FROM docs_public d WHERE d.content @@@ q_text AND (agent_filter IS NULL OR d.agent = agent_filter)
  ORDER BY paradedb.score(d.id) DESC LIMIT 100)
SELECT d.id, d.corpus, d.source, left(d.content,160), vec.rnk::int, bm.rnk::int,
       (vec_w*COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM docs_public d LEFT JOIN vec ON vec.id=d.id LEFT JOIN bm ON bm.id=d.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- hybrid_search_memories already references m.embedding -> now mxbai (no change needed)
SELECT 'cutover complete: embedding=mxbai (docs+memories), bge kept as emb_bge_legal' AS status;
