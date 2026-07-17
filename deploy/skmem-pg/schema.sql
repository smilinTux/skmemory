--
-- PostgreSQL database dump
--

\restrict fCV4A5dss8tPEjUHrbNJmFAgFBRxgRSI5LX9VhiAyWJ2TFfPbrc0gbPii3MADoc

-- Dumped from database version 17.10 (Debian 17.10-1.pgdg12+1)
-- Dumped by pg_dump version 17.10 (Debian 17.10-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ag_catalog; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: lumina_knowledge; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA lumina_knowledge;


--
-- Name: opus_knowledge; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA opus_knowledge;


--
-- Name: paradedb; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: pg_search; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_search WITH SCHEMA paradedb;


--
-- Name: EXTENSION pg_search; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_search IS 'pg_search: Full text search for PostgreSQL using BM25';


--
-- Name: personal_history; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA personal_history;


--
-- Name: age; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS age WITH SCHEMA ag_catalog;


--
-- Name: EXTENSION age; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION age IS 'AGE database extension';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: hybrid_search_docs(text, public.vector, integer, text, integer, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.hybrid_search_docs(q_text text, q_vec public.vector, k integer DEFAULT 10, agent_filter text DEFAULT NULL::text, rrf_k integer DEFAULT 60, vec_w double precision DEFAULT 2.0) RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank integer, bm25_rank integer, score double precision)
    LANGUAGE sql STABLE
    AS $$
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
$$;


--
-- Name: hybrid_search_memories(text, public.vector, integer, text, integer, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.hybrid_search_memories(q_text text, q_vec public.vector, k integer DEFAULT 10, agent_filter text DEFAULT NULL::text, rrf_k integer DEFAULT 60, vec_weight double precision DEFAULT 2.0) RETURNS TABLE(id text, layer text, title text, content text, vec_rank integer, bm25_rank integer, score double precision)
    LANGUAGE sql STABLE
    AS $$
WITH vec AS (
  SELECT m.id, row_number() OVER (ORDER BY m.embedding <=> q_vec) AS rnk
  FROM memories m
  WHERE q_vec IS NOT NULL AND (agent_filter IS NULL OR m.agent = agent_filter)
  ORDER BY m.embedding <=> q_vec LIMIT 100),
bm AS (
  SELECT s.id, row_number() OVER (ORDER BY s.sc DESC) AS rnk
  FROM (
    SELECT m.id, m.agent, paradedb.score(m.id) AS sc
    FROM memories m WHERE m.content @@@ q_text
    ORDER BY paradedb.score(m.id) DESC LIMIT 200
  ) s
  WHERE (agent_filter IS NULL OR s.agent = agent_filter)
  LIMIT 100)
SELECT m.id, m.layer, m.title, left(m.content,160), vec.rnk::int, bm.rnk::int,
       (vec_weight*COALESCE(1.0/(rrf_k+vec.rnk),0) + COALESCE(1.0/(rrf_k+bm.rnk),0))::float
FROM memories m LEFT JOIN vec ON vec.id=m.id LEFT JOIN bm ON bm.id=m.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL
ORDER BY 7 DESC LIMIT k;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: _ag_label_vertex; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge._ag_label_vertex (
    id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: Agent; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Agent" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Agent_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Agent_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Agent_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Agent_id_seq" OWNED BY lumina_knowledge."Agent".id;


--
-- Name: _ag_label_edge; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge._ag_label_edge (
    id ag_catalog.graphid NOT NULL,
    start_id ag_catalog.graphid NOT NULL,
    end_id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: CITES; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."CITES" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: CITES_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."CITES_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: CITES_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."CITES_id_seq" OWNED BY lumina_knowledge."CITES".id;


--
-- Name: CONTRADICTS; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."CONTRADICTS" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: CONTRADICTS_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."CONTRADICTS_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: CONTRADICTS_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."CONTRADICTS_id_seq" OWNED BY lumina_knowledge."CONTRADICTS".id;


--
-- Name: Concept; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Concept" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Concept_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Concept_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Concept_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Concept_id_seq" OWNED BY lumina_knowledge."Concept".id;


--
-- Name: DEFINES; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."DEFINES" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: DEFINES_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."DEFINES_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: DEFINES_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."DEFINES_id_seq" OWNED BY lumina_knowledge."DEFINES".id;


--
-- Name: Document; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Document" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Document_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Document_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Document_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Document_id_seq" OWNED BY lumina_knowledge."Document".id;


--
-- Name: ESTABLISHES; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."ESTABLISHES" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: ESTABLISHES_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."ESTABLISHES_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: ESTABLISHES_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."ESTABLISHES_id_seq" OWNED BY lumina_knowledge."ESTABLISHES".id;


--
-- Name: Host; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Host" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Host_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Host_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Host_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Host_id_seq" OWNED BY lumina_knowledge."Host".id;


--
-- Name: MENTIONS; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."MENTIONS" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: MENTIONS_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."MENTIONS_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: MENTIONS_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."MENTIONS_id_seq" OWNED BY lumina_knowledge."MENTIONS".id;


--
-- Name: Memory; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Memory" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Memory_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Memory_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Memory_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Memory_id_seq" OWNED BY lumina_knowledge."Memory".id;


--
-- Name: PART_OF; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."PART_OF" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: PART_OF_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."PART_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PART_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."PART_OF_id_seq" OWNED BY lumina_knowledge."PART_OF".id;


--
-- Name: PROVIDES_EMBED; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."PROVIDES_EMBED" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: PROVIDES_EMBED_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."PROVIDES_EMBED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PROVIDES_EMBED_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."PROVIDES_EMBED_id_seq" OWNED BY lumina_knowledge."PROVIDES_EMBED".id;


--
-- Name: Person; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Person" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Person_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Person_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Person_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Person_id_seq" OWNED BY lumina_knowledge."Person".id;


--
-- Name: Project; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Project" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Project_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Project_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Project_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Project_id_seq" OWNED BY lumina_knowledge."Project".id;


--
-- Name: RELATED_TO; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."RELATED_TO" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: RELATED_TO_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."RELATED_TO_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: RELATED_TO_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."RELATED_TO_id_seq" OWNED BY lumina_knowledge."RELATED_TO".id;


--
-- Name: REQUIRES; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."REQUIRES" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: REQUIRES_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."REQUIRES_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: REQUIRES_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."REQUIRES_id_seq" OWNED BY lumina_knowledge."REQUIRES".id;


--
-- Name: SUPERSEDES; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."SUPERSEDES" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: SUPERSEDES_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."SUPERSEDES_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: SUPERSEDES_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."SUPERSEDES_id_seq" OWNED BY lumina_knowledge."SUPERSEDES".id;


--
-- Name: Service; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Service" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Service_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Service_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Service_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Service_id_seq" OWNED BY lumina_knowledge."Service".id;


--
-- Name: TAGGED_WITH; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."TAGGED_WITH" (
)
INHERITS (lumina_knowledge._ag_label_edge);


--
-- Name: TAGGED_WITH_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."TAGGED_WITH_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: TAGGED_WITH_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."TAGGED_WITH_id_seq" OWNED BY lumina_knowledge."TAGGED_WITH".id;


--
-- Name: Tag; Type: TABLE; Schema: lumina_knowledge; Owner: -
--

CREATE TABLE lumina_knowledge."Tag" (
)
INHERITS (lumina_knowledge._ag_label_vertex);


--
-- Name: Tag_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge."Tag_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Tag_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge."Tag_id_seq" OWNED BY lumina_knowledge."Tag".id;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge._ag_label_edge_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge._ag_label_edge_id_seq OWNED BY lumina_knowledge._ag_label_edge.id;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge._ag_label_vertex_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE OWNED BY; Schema: lumina_knowledge; Owner: -
--

ALTER SEQUENCE lumina_knowledge._ag_label_vertex_id_seq OWNED BY lumina_knowledge._ag_label_vertex.id;


--
-- Name: _label_id_seq; Type: SEQUENCE; Schema: lumina_knowledge; Owner: -
--

CREATE SEQUENCE lumina_knowledge._label_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 65535
    CACHE 1
    CYCLE;


--
-- Name: _ag_label_edge; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge._ag_label_edge (
    id ag_catalog.graphid NOT NULL,
    start_id ag_catalog.graphid NOT NULL,
    end_id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: MENTIONS; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."MENTIONS" (
)
INHERITS (opus_knowledge._ag_label_edge);


--
-- Name: MENTIONS_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."MENTIONS_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: MENTIONS_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."MENTIONS_id_seq" OWNED BY opus_knowledge."MENTIONS".id;


--
-- Name: _ag_label_vertex; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge._ag_label_vertex (
    id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: Memory; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."Memory" (
)
INHERITS (opus_knowledge._ag_label_vertex);


--
-- Name: Memory_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."Memory_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Memory_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."Memory_id_seq" OWNED BY opus_knowledge."Memory".id;


--
-- Name: PART_OF; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."PART_OF" (
)
INHERITS (opus_knowledge._ag_label_edge);


--
-- Name: PART_OF_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."PART_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PART_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."PART_OF_id_seq" OWNED BY opus_knowledge."PART_OF".id;


--
-- Name: Person; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."Person" (
)
INHERITS (opus_knowledge._ag_label_vertex);


--
-- Name: Person_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."Person_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Person_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."Person_id_seq" OWNED BY opus_knowledge."Person".id;


--
-- Name: Project; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."Project" (
)
INHERITS (opus_knowledge._ag_label_vertex);


--
-- Name: Project_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."Project_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Project_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."Project_id_seq" OWNED BY opus_knowledge."Project".id;


--
-- Name: RELATED_TO; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."RELATED_TO" (
)
INHERITS (opus_knowledge._ag_label_edge);


--
-- Name: RELATED_TO_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."RELATED_TO_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: RELATED_TO_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."RELATED_TO_id_seq" OWNED BY opus_knowledge."RELATED_TO".id;


--
-- Name: TAGGED_WITH; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."TAGGED_WITH" (
)
INHERITS (opus_knowledge._ag_label_edge);


--
-- Name: TAGGED_WITH_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."TAGGED_WITH_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: TAGGED_WITH_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."TAGGED_WITH_id_seq" OWNED BY opus_knowledge."TAGGED_WITH".id;


--
-- Name: Tag; Type: TABLE; Schema: opus_knowledge; Owner: -
--

CREATE TABLE opus_knowledge."Tag" (
)
INHERITS (opus_knowledge._ag_label_vertex);


--
-- Name: Tag_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge."Tag_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Tag_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge."Tag_id_seq" OWNED BY opus_knowledge."Tag".id;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge._ag_label_edge_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge._ag_label_edge_id_seq OWNED BY opus_knowledge._ag_label_edge.id;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge._ag_label_vertex_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE OWNED BY; Schema: opus_knowledge; Owner: -
--

ALTER SEQUENCE opus_knowledge._ag_label_vertex_id_seq OWNED BY opus_knowledge._ag_label_vertex.id;


--
-- Name: _label_id_seq; Type: SEQUENCE; Schema: opus_knowledge; Owner: -
--

CREATE SEQUENCE opus_knowledge._label_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 65535
    CACHE 1
    CYCLE;


--
-- Name: _ag_label_edge; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history._ag_label_edge (
    id ag_catalog.graphid NOT NULL,
    start_id ag_catalog.graphid NOT NULL,
    end_id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: ACQUITTEE_IN; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."ACQUITTEE_IN" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: ACQUITTEE_IN_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."ACQUITTEE_IN_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: ACQUITTEE_IN_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."ACQUITTEE_IN_id_seq" OWNED BY personal_history."ACQUITTEE_IN".id;


--
-- Name: AFFILIATED_WITH; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."AFFILIATED_WITH" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: AFFILIATED_WITH_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."AFFILIATED_WITH_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: AFFILIATED_WITH_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."AFFILIATED_WITH_id_seq" OWNED BY personal_history."AFFILIATED_WITH".id;


--
-- Name: _ag_label_vertex; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history._ag_label_vertex (
    id ag_catalog.graphid NOT NULL,
    properties ag_catalog.agtype DEFAULT ag_catalog.agtype_build_map() NOT NULL
);


--
-- Name: Attorney; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Attorney" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Attorney_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Attorney_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Attorney_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Attorney_id_seq" OWNED BY personal_history."Attorney".id;


--
-- Name: CourtCase; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."CourtCase" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: CourtCase_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."CourtCase_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: CourtCase_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."CourtCase_id_seq" OWNED BY personal_history."CourtCase".id;


--
-- Name: CourtOrder; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."CourtOrder" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: CourtOrder_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."CourtOrder_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: CourtOrder_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."CourtOrder_id_seq" OWNED BY personal_history."CourtOrder".id;


--
-- Name: EVALUATED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."EVALUATED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: EVALUATED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."EVALUATED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: EVALUATED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."EVALUATED_id_seq" OWNED BY personal_history."EVALUATED".id;


--
-- Name: Facility; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Facility" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Facility_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Facility_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Facility_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Facility_id_seq" OWNED BY personal_history."Facility".id;


--
-- Name: HAS_ORDER; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."HAS_ORDER" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: HAS_ORDER_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."HAS_ORDER_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: HAS_ORDER_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."HAS_ORDER_id_seq" OWNED BY personal_history."HAS_ORDER".id;


--
-- Name: HOSPITALIZED_AT; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."HOSPITALIZED_AT" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: HOSPITALIZED_AT_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."HOSPITALIZED_AT_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: HOSPITALIZED_AT_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."HOSPITALIZED_AT_id_seq" OWNED BY personal_history."HOSPITALIZED_AT".id;


--
-- Name: Hospital; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Hospital" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Hospital_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Hospital_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Hospital_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Hospital_id_seq" OWNED BY personal_history."Hospital".id;


--
-- Name: ISSUED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."ISSUED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: ISSUED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."ISSUED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: ISSUED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."ISSUED_id_seq" OWNED BY personal_history."ISSUED".id;


--
-- Name: Judge; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Judge" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Judge_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Judge_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Judge_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Judge_id_seq" OWNED BY personal_history."Judge".id;


--
-- Name: MARRIED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."MARRIED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: MARRIED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."MARRIED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: MARRIED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."MARRIED_id_seq" OWNED BY personal_history."MARRIED".id;


--
-- Name: Org; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Org" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Org_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Org_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Org_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Org_id_seq" OWNED BY personal_history."Org".id;


--
-- Name: PARENT_OF; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."PARENT_OF" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: PARENT_OF_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."PARENT_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PARENT_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."PARENT_OF_id_seq" OWNED BY personal_history."PARENT_OF".id;


--
-- Name: PRESIDED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."PRESIDED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: PRESIDED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."PRESIDED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PRESIDED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."PRESIDED_id_seq" OWNED BY personal_history."PRESIDED".id;


--
-- Name: PROSECUTED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."PROSECUTED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: PROSECUTED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."PROSECUTED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: PROSECUTED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."PROSECUTED_id_seq" OWNED BY personal_history."PROSECUTED".id;


--
-- Name: Person; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Person" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Person_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Person_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Person_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Person_id_seq" OWNED BY personal_history."Person".id;


--
-- Name: Provider; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."Provider" (
)
INHERITS (personal_history._ag_label_vertex);


--
-- Name: Provider_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."Provider_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: Provider_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."Provider_id_seq" OWNED BY personal_history."Provider".id;


--
-- Name: REFERRED_TO; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."REFERRED_TO" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: REFERRED_TO_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."REFERRED_TO_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: REFERRED_TO_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."REFERRED_TO_id_seq" OWNED BY personal_history."REFERRED_TO".id;


--
-- Name: REPRESENTS; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."REPRESENTS" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: REPRESENTS_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."REPRESENTS_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: REPRESENTS_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."REPRESENTS_id_seq" OWNED BY personal_history."REPRESENTS".id;


--
-- Name: REQUIRES_DRUG_TESTING_BY; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."REQUIRES_DRUG_TESTING_BY" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: REQUIRES_DRUG_TESTING_BY_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."REQUIRES_DRUG_TESTING_BY_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: REQUIRES_DRUG_TESTING_BY_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."REQUIRES_DRUG_TESTING_BY_id_seq" OWNED BY personal_history."REQUIRES_DRUG_TESTING_BY".id;


--
-- Name: REQUIRES_TREATMENT_AT; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."REQUIRES_TREATMENT_AT" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: REQUIRES_TREATMENT_AT_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."REQUIRES_TREATMENT_AT_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: REQUIRES_TREATMENT_AT_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."REQUIRES_TREATMENT_AT_id_seq" OWNED BY personal_history."REQUIRES_TREATMENT_AT".id;


--
-- Name: RESULTED_IN; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."RESULTED_IN" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: RESULTED_IN_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."RESULTED_IN_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: RESULTED_IN_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."RESULTED_IN_id_seq" OWNED BY personal_history."RESULTED_IN".id;


--
-- Name: SIBLING_OF; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."SIBLING_OF" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: SIBLING_OF_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."SIBLING_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: SIBLING_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."SIBLING_OF_id_seq" OWNED BY personal_history."SIBLING_OF".id;


--
-- Name: STEPPARENT_OF; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."STEPPARENT_OF" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: STEPPARENT_OF_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."STEPPARENT_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: STEPPARENT_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."STEPPARENT_OF_id_seq" OWNED BY personal_history."STEPPARENT_OF".id;


--
-- Name: TREATED; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."TREATED" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: TREATED_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."TREATED_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: TREATED_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."TREATED_id_seq" OWNED BY personal_history."TREATED".id;


--
-- Name: UNCLE_OF; Type: TABLE; Schema: personal_history; Owner: -
--

CREATE TABLE personal_history."UNCLE_OF" (
)
INHERITS (personal_history._ag_label_edge);


--
-- Name: UNCLE_OF_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history."UNCLE_OF_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: UNCLE_OF_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history."UNCLE_OF_id_seq" OWNED BY personal_history."UNCLE_OF".id;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history._ag_label_edge_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_edge_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history._ag_label_edge_id_seq OWNED BY personal_history._ag_label_edge.id;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history._ag_label_vertex_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 281474976710655
    CACHE 1;


--
-- Name: _ag_label_vertex_id_seq; Type: SEQUENCE OWNED BY; Schema: personal_history; Owner: -
--

ALTER SEQUENCE personal_history._ag_label_vertex_id_seq OWNED BY personal_history._ag_label_vertex.id;


--
-- Name: _label_id_seq; Type: SEQUENCE; Schema: personal_history; Owner: -
--

CREATE SEQUENCE personal_history._label_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 65535
    CACHE 1
    CYCLE;


--
-- Name: docs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.docs (
    id bigint NOT NULL,
    corpus text,
    source text,
    chunk_idx integer,
    content text,
    meta jsonb DEFAULT '{}'::jsonb,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, COALESCE(content, ''::text))) STORED,
    agent text DEFAULT 'lumina'::text,
    embedding public.vector(1024)
);


--
-- Name: docs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.docs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: docs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.docs_id_seq OWNED BY public.docs.id;


--
-- Name: file_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_locations (
    id bigint NOT NULL,
    node text NOT NULL,
    path text NOT NULL,
    doc_id bigint,
    mtime double precision,
    sha text
);


--
-- Name: file_locations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_locations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_locations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_locations_id_seq OWNED BY public.file_locations.id;


--
-- Name: memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memories (
    id text NOT NULL,
    layer text,
    role text,
    title text,
    content text,
    summary text,
    tags text[] DEFAULT '{}'::text[],
    source text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    memory_json jsonb NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, ((((COALESCE(title, ''::text) || ' '::text) || COALESCE(content, ''::text)) || ' '::text) || COALESCE(summary, ''::text)))) STORED,
    agent text DEFAULT 'lumina'::text,
    embedding public.vector(1024)
);


--
-- Name: Agent id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Agent" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Agent'::name))::integer, nextval('lumina_knowledge."Agent_id_seq"'::regclass));


--
-- Name: Agent properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Agent" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: CITES id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."CITES" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'CITES'::name))::integer, nextval('lumina_knowledge."CITES_id_seq"'::regclass));


--
-- Name: CITES properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."CITES" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: CONTRADICTS id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."CONTRADICTS" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'CONTRADICTS'::name))::integer, nextval('lumina_knowledge."CONTRADICTS_id_seq"'::regclass));


--
-- Name: CONTRADICTS properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."CONTRADICTS" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Concept id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Concept" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Concept'::name))::integer, nextval('lumina_knowledge."Concept_id_seq"'::regclass));


--
-- Name: Concept properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Concept" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: DEFINES id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."DEFINES" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'DEFINES'::name))::integer, nextval('lumina_knowledge."DEFINES_id_seq"'::regclass));


--
-- Name: DEFINES properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."DEFINES" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Document id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Document" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Document'::name))::integer, nextval('lumina_knowledge."Document_id_seq"'::regclass));


--
-- Name: Document properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Document" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: ESTABLISHES id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."ESTABLISHES" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'ESTABLISHES'::name))::integer, nextval('lumina_knowledge."ESTABLISHES_id_seq"'::regclass));


--
-- Name: ESTABLISHES properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."ESTABLISHES" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Host id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Host" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Host'::name))::integer, nextval('lumina_knowledge."Host_id_seq"'::regclass));


--
-- Name: Host properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Host" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: MENTIONS id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."MENTIONS" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'MENTIONS'::name))::integer, nextval('lumina_knowledge."MENTIONS_id_seq"'::regclass));


--
-- Name: MENTIONS properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."MENTIONS" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Memory id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Memory" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Memory'::name))::integer, nextval('lumina_knowledge."Memory_id_seq"'::regclass));


--
-- Name: Memory properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Memory" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PART_OF id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."PART_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'PART_OF'::name))::integer, nextval('lumina_knowledge."PART_OF_id_seq"'::regclass));


--
-- Name: PART_OF properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."PART_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PROVIDES_EMBED id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."PROVIDES_EMBED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'PROVIDES_EMBED'::name))::integer, nextval('lumina_knowledge."PROVIDES_EMBED_id_seq"'::regclass));


--
-- Name: PROVIDES_EMBED properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."PROVIDES_EMBED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Person id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Person" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Person'::name))::integer, nextval('lumina_knowledge."Person_id_seq"'::regclass));


--
-- Name: Person properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Person" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Project id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Project" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Project'::name))::integer, nextval('lumina_knowledge."Project_id_seq"'::regclass));


--
-- Name: Project properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Project" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: RELATED_TO id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."RELATED_TO" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'RELATED_TO'::name))::integer, nextval('lumina_knowledge."RELATED_TO_id_seq"'::regclass));


--
-- Name: RELATED_TO properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."RELATED_TO" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: REQUIRES id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."REQUIRES" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'REQUIRES'::name))::integer, nextval('lumina_knowledge."REQUIRES_id_seq"'::regclass));


--
-- Name: REQUIRES properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."REQUIRES" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: SUPERSEDES id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."SUPERSEDES" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'SUPERSEDES'::name))::integer, nextval('lumina_knowledge."SUPERSEDES_id_seq"'::regclass));


--
-- Name: SUPERSEDES properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."SUPERSEDES" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Service id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Service" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Service'::name))::integer, nextval('lumina_knowledge."Service_id_seq"'::regclass));


--
-- Name: Service properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Service" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: TAGGED_WITH id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."TAGGED_WITH" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'TAGGED_WITH'::name))::integer, nextval('lumina_knowledge."TAGGED_WITH_id_seq"'::regclass));


--
-- Name: TAGGED_WITH properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."TAGGED_WITH" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Tag id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Tag" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, 'Tag'::name))::integer, nextval('lumina_knowledge."Tag_id_seq"'::regclass));


--
-- Name: Tag properties; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Tag" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: _ag_label_edge id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge._ag_label_edge ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, '_ag_label_edge'::name))::integer, nextval('lumina_knowledge._ag_label_edge_id_seq'::regclass));


--
-- Name: _ag_label_vertex id; Type: DEFAULT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge._ag_label_vertex ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('lumina_knowledge'::name, '_ag_label_vertex'::name))::integer, nextval('lumina_knowledge._ag_label_vertex_id_seq'::regclass));


--
-- Name: MENTIONS id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."MENTIONS" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'MENTIONS'::name))::integer, nextval('opus_knowledge."MENTIONS_id_seq"'::regclass));


--
-- Name: MENTIONS properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."MENTIONS" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Memory id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Memory" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'Memory'::name))::integer, nextval('opus_knowledge."Memory_id_seq"'::regclass));


--
-- Name: Memory properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Memory" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PART_OF id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."PART_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'PART_OF'::name))::integer, nextval('opus_knowledge."PART_OF_id_seq"'::regclass));


--
-- Name: PART_OF properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."PART_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Person id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Person" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'Person'::name))::integer, nextval('opus_knowledge."Person_id_seq"'::regclass));


--
-- Name: Person properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Person" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Project id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Project" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'Project'::name))::integer, nextval('opus_knowledge."Project_id_seq"'::regclass));


--
-- Name: Project properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Project" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: RELATED_TO id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."RELATED_TO" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'RELATED_TO'::name))::integer, nextval('opus_knowledge."RELATED_TO_id_seq"'::regclass));


--
-- Name: RELATED_TO properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."RELATED_TO" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: TAGGED_WITH id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."TAGGED_WITH" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'TAGGED_WITH'::name))::integer, nextval('opus_knowledge."TAGGED_WITH_id_seq"'::regclass));


--
-- Name: TAGGED_WITH properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."TAGGED_WITH" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Tag id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Tag" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, 'Tag'::name))::integer, nextval('opus_knowledge."Tag_id_seq"'::regclass));


--
-- Name: Tag properties; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Tag" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: _ag_label_edge id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge._ag_label_edge ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, '_ag_label_edge'::name))::integer, nextval('opus_knowledge._ag_label_edge_id_seq'::regclass));


--
-- Name: _ag_label_vertex id; Type: DEFAULT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge._ag_label_vertex ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('opus_knowledge'::name, '_ag_label_vertex'::name))::integer, nextval('opus_knowledge._ag_label_vertex_id_seq'::regclass));


--
-- Name: ACQUITTEE_IN id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."ACQUITTEE_IN" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'ACQUITTEE_IN'::name))::integer, nextval('personal_history."ACQUITTEE_IN_id_seq"'::regclass));


--
-- Name: ACQUITTEE_IN properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."ACQUITTEE_IN" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: AFFILIATED_WITH id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."AFFILIATED_WITH" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'AFFILIATED_WITH'::name))::integer, nextval('personal_history."AFFILIATED_WITH_id_seq"'::regclass));


--
-- Name: AFFILIATED_WITH properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."AFFILIATED_WITH" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Attorney id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Attorney" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Attorney'::name))::integer, nextval('personal_history."Attorney_id_seq"'::regclass));


--
-- Name: Attorney properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Attorney" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: CourtCase id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtCase" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'CourtCase'::name))::integer, nextval('personal_history."CourtCase_id_seq"'::regclass));


--
-- Name: CourtCase properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtCase" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: CourtOrder id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtOrder" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'CourtOrder'::name))::integer, nextval('personal_history."CourtOrder_id_seq"'::regclass));


--
-- Name: CourtOrder properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtOrder" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: EVALUATED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."EVALUATED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'EVALUATED'::name))::integer, nextval('personal_history."EVALUATED_id_seq"'::regclass));


--
-- Name: EVALUATED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."EVALUATED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Facility id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Facility" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Facility'::name))::integer, nextval('personal_history."Facility_id_seq"'::regclass));


--
-- Name: Facility properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Facility" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: HAS_ORDER id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."HAS_ORDER" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'HAS_ORDER'::name))::integer, nextval('personal_history."HAS_ORDER_id_seq"'::regclass));


--
-- Name: HAS_ORDER properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."HAS_ORDER" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: HOSPITALIZED_AT id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."HOSPITALIZED_AT" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'HOSPITALIZED_AT'::name))::integer, nextval('personal_history."HOSPITALIZED_AT_id_seq"'::regclass));


--
-- Name: HOSPITALIZED_AT properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."HOSPITALIZED_AT" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Hospital id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Hospital" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Hospital'::name))::integer, nextval('personal_history."Hospital_id_seq"'::regclass));


--
-- Name: Hospital properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Hospital" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: ISSUED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."ISSUED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'ISSUED'::name))::integer, nextval('personal_history."ISSUED_id_seq"'::regclass));


--
-- Name: ISSUED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."ISSUED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Judge id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Judge" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Judge'::name))::integer, nextval('personal_history."Judge_id_seq"'::regclass));


--
-- Name: Judge properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Judge" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: MARRIED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."MARRIED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'MARRIED'::name))::integer, nextval('personal_history."MARRIED_id_seq"'::regclass));


--
-- Name: MARRIED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."MARRIED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Org id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Org" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Org'::name))::integer, nextval('personal_history."Org_id_seq"'::regclass));


--
-- Name: Org properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Org" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PARENT_OF id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PARENT_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'PARENT_OF'::name))::integer, nextval('personal_history."PARENT_OF_id_seq"'::regclass));


--
-- Name: PARENT_OF properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PARENT_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PRESIDED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PRESIDED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'PRESIDED'::name))::integer, nextval('personal_history."PRESIDED_id_seq"'::regclass));


--
-- Name: PRESIDED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PRESIDED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: PROSECUTED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PROSECUTED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'PROSECUTED'::name))::integer, nextval('personal_history."PROSECUTED_id_seq"'::regclass));


--
-- Name: PROSECUTED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."PROSECUTED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Person id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Person" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Person'::name))::integer, nextval('personal_history."Person_id_seq"'::regclass));


--
-- Name: Person properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Person" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: Provider id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Provider" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'Provider'::name))::integer, nextval('personal_history."Provider_id_seq"'::regclass));


--
-- Name: Provider properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Provider" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: REFERRED_TO id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REFERRED_TO" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'REFERRED_TO'::name))::integer, nextval('personal_history."REFERRED_TO_id_seq"'::regclass));


--
-- Name: REFERRED_TO properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REFERRED_TO" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: REPRESENTS id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REPRESENTS" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'REPRESENTS'::name))::integer, nextval('personal_history."REPRESENTS_id_seq"'::regclass));


--
-- Name: REPRESENTS properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REPRESENTS" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: REQUIRES_DRUG_TESTING_BY id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REQUIRES_DRUG_TESTING_BY" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'REQUIRES_DRUG_TESTING_BY'::name))::integer, nextval('personal_history."REQUIRES_DRUG_TESTING_BY_id_seq"'::regclass));


--
-- Name: REQUIRES_DRUG_TESTING_BY properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REQUIRES_DRUG_TESTING_BY" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: REQUIRES_TREATMENT_AT id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REQUIRES_TREATMENT_AT" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'REQUIRES_TREATMENT_AT'::name))::integer, nextval('personal_history."REQUIRES_TREATMENT_AT_id_seq"'::regclass));


--
-- Name: REQUIRES_TREATMENT_AT properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."REQUIRES_TREATMENT_AT" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: RESULTED_IN id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."RESULTED_IN" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'RESULTED_IN'::name))::integer, nextval('personal_history."RESULTED_IN_id_seq"'::regclass));


--
-- Name: RESULTED_IN properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."RESULTED_IN" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: SIBLING_OF id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."SIBLING_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'SIBLING_OF'::name))::integer, nextval('personal_history."SIBLING_OF_id_seq"'::regclass));


--
-- Name: SIBLING_OF properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."SIBLING_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: STEPPARENT_OF id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."STEPPARENT_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'STEPPARENT_OF'::name))::integer, nextval('personal_history."STEPPARENT_OF_id_seq"'::regclass));


--
-- Name: STEPPARENT_OF properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."STEPPARENT_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: TREATED id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."TREATED" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'TREATED'::name))::integer, nextval('personal_history."TREATED_id_seq"'::regclass));


--
-- Name: TREATED properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."TREATED" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: UNCLE_OF id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."UNCLE_OF" ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, 'UNCLE_OF'::name))::integer, nextval('personal_history."UNCLE_OF_id_seq"'::regclass));


--
-- Name: UNCLE_OF properties; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."UNCLE_OF" ALTER COLUMN properties SET DEFAULT ag_catalog.agtype_build_map();


--
-- Name: _ag_label_edge id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history._ag_label_edge ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, '_ag_label_edge'::name))::integer, nextval('personal_history._ag_label_edge_id_seq'::regclass));


--
-- Name: _ag_label_vertex id; Type: DEFAULT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history._ag_label_vertex ALTER COLUMN id SET DEFAULT ag_catalog._graphid((ag_catalog._label_id('personal_history'::name, '_ag_label_vertex'::name))::integer, nextval('personal_history._ag_label_vertex_id_seq'::regclass));


--
-- Name: docs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docs ALTER COLUMN id SET DEFAULT nextval('public.docs_id_seq'::regclass);


--
-- Name: file_locations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_locations ALTER COLUMN id SET DEFAULT nextval('public.file_locations_id_seq'::regclass);


--
-- Name: Agent Agent_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Agent"
    ADD CONSTRAINT "Agent_pkey" PRIMARY KEY (id);


--
-- Name: Concept Concept_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Concept"
    ADD CONSTRAINT "Concept_pkey" PRIMARY KEY (id);


--
-- Name: Document Document_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Document"
    ADD CONSTRAINT "Document_pkey" PRIMARY KEY (id);


--
-- Name: Host Host_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Host"
    ADD CONSTRAINT "Host_pkey" PRIMARY KEY (id);


--
-- Name: Memory Memory_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Memory"
    ADD CONSTRAINT "Memory_pkey" PRIMARY KEY (id);


--
-- Name: Person Person_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Person"
    ADD CONSTRAINT "Person_pkey" PRIMARY KEY (id);


--
-- Name: Project Project_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Project"
    ADD CONSTRAINT "Project_pkey" PRIMARY KEY (id);


--
-- Name: Service Service_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Service"
    ADD CONSTRAINT "Service_pkey" PRIMARY KEY (id);


--
-- Name: Tag Tag_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge."Tag"
    ADD CONSTRAINT "Tag_pkey" PRIMARY KEY (id);


--
-- Name: _ag_label_edge _ag_label_edge_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge._ag_label_edge
    ADD CONSTRAINT _ag_label_edge_pkey PRIMARY KEY (id);


--
-- Name: _ag_label_vertex _ag_label_vertex_pkey; Type: CONSTRAINT; Schema: lumina_knowledge; Owner: -
--

ALTER TABLE ONLY lumina_knowledge._ag_label_vertex
    ADD CONSTRAINT _ag_label_vertex_pkey PRIMARY KEY (id);


--
-- Name: Memory Memory_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Memory"
    ADD CONSTRAINT "Memory_pkey" PRIMARY KEY (id);


--
-- Name: Person Person_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Person"
    ADD CONSTRAINT "Person_pkey" PRIMARY KEY (id);


--
-- Name: Project Project_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Project"
    ADD CONSTRAINT "Project_pkey" PRIMARY KEY (id);


--
-- Name: Tag Tag_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge."Tag"
    ADD CONSTRAINT "Tag_pkey" PRIMARY KEY (id);


--
-- Name: _ag_label_edge _ag_label_edge_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge._ag_label_edge
    ADD CONSTRAINT _ag_label_edge_pkey PRIMARY KEY (id);


--
-- Name: _ag_label_vertex _ag_label_vertex_pkey; Type: CONSTRAINT; Schema: opus_knowledge; Owner: -
--

ALTER TABLE ONLY opus_knowledge._ag_label_vertex
    ADD CONSTRAINT _ag_label_vertex_pkey PRIMARY KEY (id);


--
-- Name: Attorney Attorney_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Attorney"
    ADD CONSTRAINT "Attorney_pkey" PRIMARY KEY (id);


--
-- Name: CourtCase CourtCase_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtCase"
    ADD CONSTRAINT "CourtCase_pkey" PRIMARY KEY (id);


--
-- Name: CourtOrder CourtOrder_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."CourtOrder"
    ADD CONSTRAINT "CourtOrder_pkey" PRIMARY KEY (id);


--
-- Name: Facility Facility_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Facility"
    ADD CONSTRAINT "Facility_pkey" PRIMARY KEY (id);


--
-- Name: Hospital Hospital_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Hospital"
    ADD CONSTRAINT "Hospital_pkey" PRIMARY KEY (id);


--
-- Name: Judge Judge_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Judge"
    ADD CONSTRAINT "Judge_pkey" PRIMARY KEY (id);


--
-- Name: Org Org_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Org"
    ADD CONSTRAINT "Org_pkey" PRIMARY KEY (id);


--
-- Name: Person Person_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Person"
    ADD CONSTRAINT "Person_pkey" PRIMARY KEY (id);


--
-- Name: Provider Provider_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history."Provider"
    ADD CONSTRAINT "Provider_pkey" PRIMARY KEY (id);


--
-- Name: _ag_label_edge _ag_label_edge_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history._ag_label_edge
    ADD CONSTRAINT _ag_label_edge_pkey PRIMARY KEY (id);


--
-- Name: _ag_label_vertex _ag_label_vertex_pkey; Type: CONSTRAINT; Schema: personal_history; Owner: -
--

ALTER TABLE ONLY personal_history._ag_label_vertex
    ADD CONSTRAINT _ag_label_vertex_pkey PRIMARY KEY (id);


--
-- Name: docs docs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docs
    ADD CONSTRAINT docs_pkey PRIMARY KEY (id);


--
-- Name: file_locations file_locations_node_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_locations
    ADD CONSTRAINT file_locations_node_path_key UNIQUE (node, path);


--
-- Name: file_locations file_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_locations
    ADD CONSTRAINT file_locations_pkey PRIMARY KEY (id);


--
-- Name: memories memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_pkey PRIMARY KEY (id);


--
-- Name: CITES_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "CITES_end_id_idx" ON lumina_knowledge."CITES" USING btree (end_id);


--
-- Name: CITES_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "CITES_start_id_idx" ON lumina_knowledge."CITES" USING btree (start_id);


--
-- Name: CONTRADICTS_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "CONTRADICTS_end_id_idx" ON lumina_knowledge."CONTRADICTS" USING btree (end_id);


--
-- Name: CONTRADICTS_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "CONTRADICTS_start_id_idx" ON lumina_knowledge."CONTRADICTS" USING btree (start_id);


--
-- Name: DEFINES_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "DEFINES_end_id_idx" ON lumina_knowledge."DEFINES" USING btree (end_id);


--
-- Name: DEFINES_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "DEFINES_start_id_idx" ON lumina_knowledge."DEFINES" USING btree (start_id);


--
-- Name: ESTABLISHES_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "ESTABLISHES_end_id_idx" ON lumina_knowledge."ESTABLISHES" USING btree (end_id);


--
-- Name: ESTABLISHES_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "ESTABLISHES_start_id_idx" ON lumina_knowledge."ESTABLISHES" USING btree (start_id);


--
-- Name: MENTIONS_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "MENTIONS_end_id_idx" ON lumina_knowledge."MENTIONS" USING btree (end_id);


--
-- Name: MENTIONS_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "MENTIONS_start_id_idx" ON lumina_knowledge."MENTIONS" USING btree (start_id);


--
-- Name: PART_OF_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "PART_OF_end_id_idx" ON lumina_knowledge."PART_OF" USING btree (end_id);


--
-- Name: PART_OF_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "PART_OF_start_id_idx" ON lumina_knowledge."PART_OF" USING btree (start_id);


--
-- Name: PROVIDES_EMBED_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "PROVIDES_EMBED_end_id_idx" ON lumina_knowledge."PROVIDES_EMBED" USING btree (end_id);


--
-- Name: PROVIDES_EMBED_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "PROVIDES_EMBED_start_id_idx" ON lumina_knowledge."PROVIDES_EMBED" USING btree (start_id);


--
-- Name: RELATED_TO_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "RELATED_TO_end_id_idx" ON lumina_knowledge."RELATED_TO" USING btree (end_id);


--
-- Name: RELATED_TO_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "RELATED_TO_start_id_idx" ON lumina_knowledge."RELATED_TO" USING btree (start_id);


--
-- Name: REQUIRES_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "REQUIRES_end_id_idx" ON lumina_knowledge."REQUIRES" USING btree (end_id);


--
-- Name: REQUIRES_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "REQUIRES_start_id_idx" ON lumina_knowledge."REQUIRES" USING btree (start_id);


--
-- Name: SUPERSEDES_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "SUPERSEDES_end_id_idx" ON lumina_knowledge."SUPERSEDES" USING btree (end_id);


--
-- Name: SUPERSEDES_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "SUPERSEDES_start_id_idx" ON lumina_knowledge."SUPERSEDES" USING btree (start_id);


--
-- Name: TAGGED_WITH_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "TAGGED_WITH_end_id_idx" ON lumina_knowledge."TAGGED_WITH" USING btree (end_id);


--
-- Name: TAGGED_WITH_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX "TAGGED_WITH_start_id_idx" ON lumina_knowledge."TAGGED_WITH" USING btree (start_id);


--
-- Name: _ag_label_edge_end_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX _ag_label_edge_end_id_idx ON lumina_knowledge._ag_label_edge USING btree (end_id);


--
-- Name: _ag_label_edge_start_id_idx; Type: INDEX; Schema: lumina_knowledge; Owner: -
--

CREATE INDEX _ag_label_edge_start_id_idx ON lumina_knowledge._ag_label_edge USING btree (start_id);


--
-- Name: MENTIONS_end_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "MENTIONS_end_id_idx" ON opus_knowledge."MENTIONS" USING btree (end_id);


--
-- Name: MENTIONS_start_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "MENTIONS_start_id_idx" ON opus_knowledge."MENTIONS" USING btree (start_id);


--
-- Name: PART_OF_end_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "PART_OF_end_id_idx" ON opus_knowledge."PART_OF" USING btree (end_id);


--
-- Name: PART_OF_start_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "PART_OF_start_id_idx" ON opus_knowledge."PART_OF" USING btree (start_id);


--
-- Name: RELATED_TO_end_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "RELATED_TO_end_id_idx" ON opus_knowledge."RELATED_TO" USING btree (end_id);


--
-- Name: RELATED_TO_start_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "RELATED_TO_start_id_idx" ON opus_knowledge."RELATED_TO" USING btree (start_id);


--
-- Name: TAGGED_WITH_end_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "TAGGED_WITH_end_id_idx" ON opus_knowledge."TAGGED_WITH" USING btree (end_id);


--
-- Name: TAGGED_WITH_start_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX "TAGGED_WITH_start_id_idx" ON opus_knowledge."TAGGED_WITH" USING btree (start_id);


--
-- Name: _ag_label_edge_end_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX _ag_label_edge_end_id_idx ON opus_knowledge._ag_label_edge USING btree (end_id);


--
-- Name: _ag_label_edge_start_id_idx; Type: INDEX; Schema: opus_knowledge; Owner: -
--

CREATE INDEX _ag_label_edge_start_id_idx ON opus_knowledge._ag_label_edge USING btree (start_id);


--
-- Name: ACQUITTEE_IN_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "ACQUITTEE_IN_end_id_idx" ON personal_history."ACQUITTEE_IN" USING btree (end_id);


--
-- Name: ACQUITTEE_IN_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "ACQUITTEE_IN_start_id_idx" ON personal_history."ACQUITTEE_IN" USING btree (start_id);


--
-- Name: AFFILIATED_WITH_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "AFFILIATED_WITH_end_id_idx" ON personal_history."AFFILIATED_WITH" USING btree (end_id);


--
-- Name: AFFILIATED_WITH_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "AFFILIATED_WITH_start_id_idx" ON personal_history."AFFILIATED_WITH" USING btree (start_id);


--
-- Name: EVALUATED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "EVALUATED_end_id_idx" ON personal_history."EVALUATED" USING btree (end_id);


--
-- Name: EVALUATED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "EVALUATED_start_id_idx" ON personal_history."EVALUATED" USING btree (start_id);


--
-- Name: HAS_ORDER_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "HAS_ORDER_end_id_idx" ON personal_history."HAS_ORDER" USING btree (end_id);


--
-- Name: HAS_ORDER_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "HAS_ORDER_start_id_idx" ON personal_history."HAS_ORDER" USING btree (start_id);


--
-- Name: HOSPITALIZED_AT_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "HOSPITALIZED_AT_end_id_idx" ON personal_history."HOSPITALIZED_AT" USING btree (end_id);


--
-- Name: HOSPITALIZED_AT_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "HOSPITALIZED_AT_start_id_idx" ON personal_history."HOSPITALIZED_AT" USING btree (start_id);


--
-- Name: ISSUED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "ISSUED_end_id_idx" ON personal_history."ISSUED" USING btree (end_id);


--
-- Name: ISSUED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "ISSUED_start_id_idx" ON personal_history."ISSUED" USING btree (start_id);


--
-- Name: MARRIED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "MARRIED_end_id_idx" ON personal_history."MARRIED" USING btree (end_id);


--
-- Name: MARRIED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "MARRIED_start_id_idx" ON personal_history."MARRIED" USING btree (start_id);


--
-- Name: PARENT_OF_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PARENT_OF_end_id_idx" ON personal_history."PARENT_OF" USING btree (end_id);


--
-- Name: PARENT_OF_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PARENT_OF_start_id_idx" ON personal_history."PARENT_OF" USING btree (start_id);


--
-- Name: PRESIDED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PRESIDED_end_id_idx" ON personal_history."PRESIDED" USING btree (end_id);


--
-- Name: PRESIDED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PRESIDED_start_id_idx" ON personal_history."PRESIDED" USING btree (start_id);


--
-- Name: PROSECUTED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PROSECUTED_end_id_idx" ON personal_history."PROSECUTED" USING btree (end_id);


--
-- Name: PROSECUTED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "PROSECUTED_start_id_idx" ON personal_history."PROSECUTED" USING btree (start_id);


--
-- Name: REFERRED_TO_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REFERRED_TO_end_id_idx" ON personal_history."REFERRED_TO" USING btree (end_id);


--
-- Name: REFERRED_TO_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REFERRED_TO_start_id_idx" ON personal_history."REFERRED_TO" USING btree (start_id);


--
-- Name: REPRESENTS_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REPRESENTS_end_id_idx" ON personal_history."REPRESENTS" USING btree (end_id);


--
-- Name: REPRESENTS_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REPRESENTS_start_id_idx" ON personal_history."REPRESENTS" USING btree (start_id);


--
-- Name: REQUIRES_DRUG_TESTING_BY_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REQUIRES_DRUG_TESTING_BY_end_id_idx" ON personal_history."REQUIRES_DRUG_TESTING_BY" USING btree (end_id);


--
-- Name: REQUIRES_DRUG_TESTING_BY_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REQUIRES_DRUG_TESTING_BY_start_id_idx" ON personal_history."REQUIRES_DRUG_TESTING_BY" USING btree (start_id);


--
-- Name: REQUIRES_TREATMENT_AT_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REQUIRES_TREATMENT_AT_end_id_idx" ON personal_history."REQUIRES_TREATMENT_AT" USING btree (end_id);


--
-- Name: REQUIRES_TREATMENT_AT_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "REQUIRES_TREATMENT_AT_start_id_idx" ON personal_history."REQUIRES_TREATMENT_AT" USING btree (start_id);


--
-- Name: RESULTED_IN_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "RESULTED_IN_end_id_idx" ON personal_history."RESULTED_IN" USING btree (end_id);


--
-- Name: RESULTED_IN_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "RESULTED_IN_start_id_idx" ON personal_history."RESULTED_IN" USING btree (start_id);


--
-- Name: SIBLING_OF_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "SIBLING_OF_end_id_idx" ON personal_history."SIBLING_OF" USING btree (end_id);


--
-- Name: SIBLING_OF_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "SIBLING_OF_start_id_idx" ON personal_history."SIBLING_OF" USING btree (start_id);


--
-- Name: STEPPARENT_OF_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "STEPPARENT_OF_end_id_idx" ON personal_history."STEPPARENT_OF" USING btree (end_id);


--
-- Name: STEPPARENT_OF_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "STEPPARENT_OF_start_id_idx" ON personal_history."STEPPARENT_OF" USING btree (start_id);


--
-- Name: TREATED_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "TREATED_end_id_idx" ON personal_history."TREATED" USING btree (end_id);


--
-- Name: TREATED_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "TREATED_start_id_idx" ON personal_history."TREATED" USING btree (start_id);


--
-- Name: UNCLE_OF_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "UNCLE_OF_end_id_idx" ON personal_history."UNCLE_OF" USING btree (end_id);


--
-- Name: UNCLE_OF_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX "UNCLE_OF_start_id_idx" ON personal_history."UNCLE_OF" USING btree (start_id);


--
-- Name: _ag_label_edge_end_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX _ag_label_edge_end_id_idx ON personal_history._ag_label_edge USING btree (end_id);


--
-- Name: _ag_label_edge_start_id_idx; Type: INDEX; Schema: personal_history; Owner: -
--

CREATE INDEX _ag_label_edge_start_id_idx ON personal_history._ag_label_edge USING btree (start_id);


--
-- Name: docs_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX docs_agent ON public.docs USING btree (agent);


--
-- Name: docs_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX docs_bm25 ON public.docs USING bm25 (id, content, corpus, source) WITH (key_field=id, text_fields='{
   "content": {"tokenizer": {"type": "default", "stemmer": "English", "stopwords_language": "English"}},
   "corpus":  {"tokenizer": {"type": "default", "stemmer": "English"}},
   "source":  {"tokenizer": {"type": "default"}}}');


--
-- Name: docs_corpus; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX docs_corpus ON public.docs USING btree (corpus);


--
-- Name: docs_emb_mxbai_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX docs_emb_mxbai_hnsw ON public.docs USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: docs_source_chunk_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX docs_source_chunk_idx ON public.docs USING btree (source, chunk_idx);


--
-- Name: docs_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX docs_tsv ON public.docs USING gin (tsv);


--
-- Name: file_locations_doc_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_locations_doc_id_idx ON public.file_locations USING btree (doc_id);


--
-- Name: file_locations_path_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_locations_path_idx ON public.file_locations USING btree (path);


--
-- Name: memories_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_agent ON public.memories USING btree (agent);


--
-- Name: memories_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_bm25 ON public.memories USING bm25 (id, title, content, summary) WITH (key_field=id, text_fields='{
   "title":   {"tokenizer": {"type": "default", "stemmer": "English", "stopwords_language": "English"}},
   "content": {"tokenizer": {"type": "default", "stemmer": "English", "stopwords_language": "English"}},
   "summary": {"tokenizer": {"type": "default", "stemmer": "English", "stopwords_language": "English"}}}');


--
-- Name: memories_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_created ON public.memories USING btree (created_at DESC);


--
-- Name: memories_emb_mxbai_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_emb_mxbai_hnsw ON public.memories USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: memories_layer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_layer ON public.memories USING btree (layer);


--
-- Name: memories_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_tags ON public.memories USING gin (tags);


--
-- Name: memories_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memories_tsv ON public.memories USING gin (tsv);


--
-- PostgreSQL database dump complete
--

\unrestrict fCV4A5dss8tPEjUHrbNJmFAgFBRxgRSI5LX9VhiAyWJ2TFfPbrc0gbPii3MADoc

