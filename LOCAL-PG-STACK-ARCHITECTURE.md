# Sovereign Local Vector/Graph Stack — Build & Deployment Plan

**Status:** consolidating an in-flight migration to the standard.
**Owner:** Chef + Lumina · **Authored:** 2026-06-08 (Opus)

> **⚠️ STATE UPDATE (2026-06-09) — DB side is DONE; skwhisper is mid-migration (active work, likely Lumina):**
> - Postgres upgraded to custom image **`skmem-pg:pg17-bm25-age`** = pgvector + ParadeDB **BM25** (`pg_search`) + **Apache AGE 1.7.0** in ONE Postgres. Dockerfile `~/skmem-build/`. Data preserved (memories=15070, docs=27314).
> - **AGE graph `lumina_knowledge` created**; hybrid RRF functions `hybrid_search_memories()` / `hybrid_search_docs()` live (`~/skmem-build/02-enable-bm25-age.sql`). So §7-Q3 "graph" = **AGE-in-pg, done** (FalkorDB superseded).
> - **`.41` mirrored**: same image, container on **port 5433** (5432 = skgentis-postgres), schema+indexes+hybrid fns applied, **data not yet replicated** (engine ready, empty).
> - skwhisper: `clients/skmemory.py` (flat-file `SKMemoryWriter`) **added but not wired**; `curator.py`/`daemon.py` **still on Qdrant**. → search must move to `hybrid_search_memories()`; graph to AGE.
> - Eval note: on `docs`, **BM25 ≫ vector** (bge-legal-v2 domain-mismatched) → prefer **hybrid (RRF)** for search, not pure vector.
**Goal:** all SK framework components (skmemory, skwhisper, skcapstone, gbrain) use the **local Postgres + pgvector** stack — no dependency on the down `douno` cluster (Qdrant/skvector + FalkorDB/skgraph).

---

## 1. Why
The `douno` cluster is down for good for our purposes. skvector (Qdrant) and skgraph (FalkorDB) are unreachable. We already stood up a **sovereign local equivalent** — finish moving everything onto it and make local-first the standard.

## 2. Target architecture

```
            ┌──────────────────────── .158 (noroc2027) — PRIMARY ────────────────────────┐
            │  Postgres 17 + pgvector  (docker: skmem-pg, :5432, db=skmemory)             │
            │    • memories(embedding vector(1024), tsv)  HNSW(cosine)+GIN(tsv,tags)      │
            │    • docs(embedding vector(1024), tsv)      ← gbrain corpus/wiki            │
            │  Consumers (local):  skmemory · skwhisper · skcapstone · Hermes · gbrain    │
            └────────────────────────────────────────────────────────────────────────────┘
                        ▲ embeddings (1024-dim)                ▲ DSN :5432
                        │                                      │
   ┌──────── .100 (5060 Ti) ────────┐            ┌──────── .41 (laptop / edge) ────────┐
   │ bge-embed.service :11435        │            │ skmemory + skwhisper (edge)         │
   │   bge-legal-v2, /api/embed      │            │ → embeds via .100:11435             │
   │ qwen3.6-27b-abliterated :8082   │            │ → reads/writes .158:5432            │
   │   (OpenAI /v1 — reasoning/LLM)  │            │ (later: local pg logical replica)   │
   │ Ollama :11434 (qwen3.5:4b, etc) │            └─────────────────────────────────────┘
   └─────────────────────────────────┘
```

**Single source of truth:** `.158` Postgres. Embeddings: `.100` bge-legal-v2 (1024-dim, fixed). Reasoning LLM: `qwen3.6-27b-abliterated` (`.100:8082`); lightweight summarization: `qwen3.5:4b` (Ollama `.100:11434`).

## 3. Configuration standard (every host, every agent)
Set in the environment (e.g. `~/.bashrc` + each service unit's `Environment=`):

```bash
SKMEMORY_VECTOR_BACKEND=pgvector
SKMEMORY_PG_DSN=postgresql://postgres:skmemory@<HOST>:5432/skmemory   # .158=localhost; others=192.168.0.158
SKMEMORY_EMBED_URL=http://192.168.0.100:11435/api/embed
SKMEMORY_EMBED_MODEL=bge-legal-v2
```
- **Vector dim is 1024** everywhere (bge-legal-v2). Never mix embedders into the same column.
- LLM endpoints (skwhisper / gbrain / agent) configured per-component, not via skmemory env.

## 4. Component status & required work

| Component | Vector store today | Action |
|---|---|---|
| **skmemory** | ✅ pgvector (live, 15k rows) | none — it's the reference impl |
| **skwhisper** | ❌ Qdrant (`skvector`) + FalkorDB | **migrate** → reuse `PGVectorBackend`; make graph optional |
| **skcapstone** | mixed | point shared-store reads at local pg; drop hard skvector dep |
| **gbrain** | cluster/qdrant | point retrieval at `.158` `docs` table + `.100` embed |

### 4a. skwhisper migration (the main remaining piece)
skwhisper touches the cluster in exactly 3 spots:
- `curator.py` → `QdrantClient.search()` (the curate ConnectError)
- `daemon.py` → `QdrantClient.upsert()` (digest writes) + `SKGraphWriter` (FalkorDB)

**Plan (reuse, don't reinvent):**
1. Add a thin `clients/pgvector.py` in skwhisper that wraps skmemory's `PGVectorBackend` (or calls it directly) exposing the same `search(vector, top_k)` / `upsert(...)` / `close()` shape the curator/daemon already expect → **minimal diff, no logic rewrite.**
2. Select backend by config: `vector_backend = "pgvector" | "qdrant"` (default pgvector). Keep Qdrant class as dormant fallback.
3. FalkorDB graph: `SKGraphWriter.from_config()` already returns `None` when unavailable → **already graceful**; leave graph off until a pg-graph (Apache AGE) is decided. Topic patterns persist fine without it.
4. Config: point skwhisper embed at the bge-legal-v2 server (align with skmemory's 1024-dim space) OR keep Ollama `mxbai-embed-large` **only if** skwhisper keeps its own separate collection. ⚠️ **Decision needed** (see §7).

### 4b. .41 edge deployment
- Install env standard (DSN → `192.168.0.158:5432`, embed → `.100:11435`).
- Run skmemory + skwhisper as `--user` systemd services (mirror `.158`).
- Phase 2 (offline resilience): Postgres **logical replica** on `.41` subscribing to `.158`; flip DSN to localhost when offline.

### 4c. gbrain
- Retrieval → `.158` `docs` table; ingestion embeds via `.100`. Tracked separately from this migration.

## 5. Best practices (the standard going forward)
- **Local-first / single source of truth:** `.158` pg. No service hard-fails because a remote cluster is gone.
- **Graceful degradation:** every external store wrapped so "unavailable" = skip/fallback, never crash (skwhisper graph already does this; apply same to vector).
- **One embedder, one dim:** bge-legal-v2 / 1024. Re-index only on embedder change.
- **Config via env + unit `Environment=`**, not hard-coded hosts. DSN differs only by host.
- **Idempotent migrations**, **health_check() before use**, **backup unit files** (`*.bak-vN`) on edits.
- **Ownership/portability:** schema + backend in git (`smilinTux/skmemory`); reproducible on any host.

## 6. Build / deploy steps (remaining)
1. **skwhisper pgvector client** (4a.1–4a.3) → commit to repo.
2. Update `skwhisper.toml`: `vector_backend=pgvector` + endpoints; restart `skwhisper@lumina`; **verify `skwhisper curate` succeeds** (the acceptance test).
3. Backfill/confirm skwhisper's own vectors in pg (or share skmemory's `memories`).
4. **.41**: env + services; verify curate + digest against `.158` pg.
5. **gbrain**: repoint (separate ticket).
6. Update memories/docs; mark cluster deps deprecated.

## 7. Open decisions
1. **Does skwhisper share skmemory's `memories` table, or get its own (e.g. `whisper_vectors`)?** Sharing = unified recall; separate = cleaner isolation. *Rec: share `memories` (skwhisper digests ARE memories).*
2. **skwhisper embedder:** switch to bge-legal-v2 (unify vector space, enables sharing) vs keep mxbai. *Rec: bge-legal-v2* (required if sharing the table).
3. **Graph:** leave off, or stand up **Apache AGE** in the same Postgres for skgraph parity? *Rec: defer; topics work without it.*
4. **.41 mode:** live-DSN to `.158` now, logical replica later — confirm.

## 8. Rollback
Qdrant/FalkorDB client classes stay in-tree (dormant). Revert = flip `vector_backend=qdrant` + restore cluster. No data loss (pg is additive; cluster stores untouched).
