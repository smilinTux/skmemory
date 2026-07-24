# Derived-Store Inventory (forget-cascade)

Status: reference doc. Card `11d1693f` (epic `ep-forget-cascade`).

Every place a memory id (skmemory) or a document id (skingest) materializes,
the key it lives under, and the exact delete/forget primitive that removes it.
The goal is a `memory_forget` (and a skingest source-forget) that leaves no
orphan in any derived store. Stores with a MISSING primitive are flagged; each
is a candidate follow-up card.

All citations are `file:line` against the source at the time of writing
(skmemory `origin/main`, skingest `~/clawd/skingest`).

---

## Part 1: skmemory (memory id)

A memory is identified by `memory.id`. The `MemoryStore` fans out to three
optional roles: `primary`, `vector`, `graph`
(`skmemory/store.py:113-129`). Which concrete backend fills each role is chosen
at construction time (`skmemory/cli.py:131-276`, `skmemory/mcp_server.py:60-102`).

The forget path is `MemoryStore.forget()` (`skmemory/store.py:990-1022`):

1. `self.primary.delete(memory_id)` (line 1001)
2. `self.vector.remove(memory_id)` if a vector backend is set (line 1011)
3. `self.graph.remove_memory(memory_id)` if a graph backend is set (line 1018)

Steps 2 and 3 are best-effort: any exception is caught and logged, never
re-raised (lines 1012-1013, 1019-1020). Only the primary delete determines the
return value. This is the root of several gaps below: a role that is either
unwired or lacks the exact method name that `forget()` calls silently leaves
its rows behind.

### 1.1 Flat JSON files (source of truth)

- Store: `~/.skcapstone/agents/<agent>/memory/{short-term,mid-term,long-term}/<id>.json`, one file per memory (plus optional detached-signature sidecar).
- Key: `memory.id` resolved to a path by `_find_file(memory_id)`.
- Role: part of `primary` (both `SQLiteBackend` and `FileBackend` own the flat files).
- Delete primitive:
  - `SQLiteBackend.delete()` unlinks the file: `skmemory/backends/sqlite_backend.py:548-549`.
  - `FileBackend.delete()` unlinks the file plus its seal sidecar: `skmemory/backends/file_backend.py:185-189`.
- Cascade status: COVERED by `forget()` step 1.

### 1.2 SQLite index (`index.db`, table `memories`)

- Store: `~/.skcapstone/agents/<agent>/memory/index.db`, a local working index rebuilt from flat files.
- Key: `id` column.
- Role: `primary` when `use_sqlite` is true (the default; `skmemory/cli.py:276`, `skmemory/store.py:125`).
- Delete primitive: `DELETE FROM memories WHERE id = ?` in `SQLiteBackend.delete()`: `skmemory/backends/sqlite_backend.py:539`. Index-delete failure is tolerated (a later reindex drops the stale row): `sqlite_backend.py:541-544`.
- Secondary primitive: `db_delete()` in the cleanup script (`scripts/memory-cleanup.py:95-101`, called from dedup at `:140` and archive at `:217`) runs the same `DELETE FROM memories WHERE id = ?`.
- Cascade status: COVERED by `forget()` step 1.

### 1.3 skmem-pg pgvector (`memories` table, per-node Postgres)

- Store: local per-node `skmem-pg` on `localhost:5432`, table `memories` (mxbai vectors + BM25 + AGE). Scoped by `agent`.
- Key: `(id, agent)`.
- Role: `vector` when `pgvector` is the enabled backend. This is the current sovereign default (`skmemory/mcp_server.py:68-76`; `skmemory/cli.py:154-179`).
- Delete primitive: `PGVectorBackend.delete()` runs `DELETE FROM memories WHERE id=%s AND agent=%s`: `skmemory/backends/pgvector_backend.py:180`.
- Orphan-prune primitive (the path that actually cleans forgotten ids today): `reconcile()` deletes pg rows whose flat file is gone: `skmemory/reconcile.py:196` (and the standalone `deploy/skmem-pg/skmem_reconcile.py:75`). Runs daily / on demand, agent-scoped.
- Cascade status: GAP (see Gap A). `forget()` step 2 calls `self.vector.remove(...)`, but `PGVectorBackend` exposes only `delete()`, no `remove()`. When pgvector is the vector role, `forget()` raises `AttributeError`, which is swallowed at `store.py:1012-1013` and logged as "SKVector remove failed". The pg row is NOT deleted at forget time; it survives until the next `reconcile` prune. On the default deployment this is the live behavior.

### 1.4 ChromaDB vector store

- Store: `~/.skcapstone/agents/<agent>/memory/chroma/`, collection `skmemory` (documents + chunk rows). A chunk-tracker sidecar (`chroma-state.json`) tracks chunk ids.
- Key: `memory_id` as the point id; chunk points carry `parent_id = memory_id`.
- Role: `vector` fallback when no other vector backend is enabled (`skmemory/cli.py:184-211`; `skmemory/mcp_server.py:90-98`). Noted as RETIRED in favor of pgvector but still the fallback.
- Delete primitives:
  - `SKChromaBackend.delete()` deletes the single point and updates the tracker: `skmemory/backends/chroma_backend.py:270-284`.
  - `SKChromaBackend.remove()` deletes the point AND all chunks where `parent_id == memory_id` (cascade): `skmemory/backends/chroma_backend.py:286` onward.
- Cascade status: COVERED. `forget()` step 2 calls `.remove()`, which Chroma implements with chunk cascade.

### 1.5 Qdrant / SKVector vector store

- Store: external Qdrant collection (shared vector store), deterministic point id derived from `memory_id`.
- Key: `_id_to_point_id(memory_id)`; chunk points carry `parent_id`.
- Role: `vector` when a skvector URL / `skvector` backend is configured (`skmemory/cli.py:217-229`).
- Delete primitives:
  - `SKVectorBackend.delete()` removes the single point: `skmemory/backends/skvector_backend.py:573-602`.
  - `SKVectorBackend.remove()` removes the point plus all chunks with matching `parent_id` (cascade): `skmemory/backends/skvector_backend.py:604` onward.
- Cascade status: COVERED. `forget()` step 2 calls `.remove()`.

### 1.6 skmem-pg AGE knowledge graph (`<agent>_knowledge`)

- Store: AGE property graph inside skmem-pg, e.g. `lumina_knowledge`, node label `Memory {id}` plus incident edges.
- Key: `Memory.id`.
- Role: intended `graph`, but built only by `context_loader._build_age_backend` (`skmemory/context_loader.py:837-851`), which the whisper / context path uses. It is NOT wired as `store.graph` in either `cli._get_store` (`skmemory/cli.py:262-274` wires only `SKGraphBackend`) or `mcp_server._get_store` (`skmemory/mcp_server.py:101` passes no graph).
- Delete primitive (exists, just unreached by forget): `AGEGraphBackend.remove_memory()` runs `MATCH (m:Memory {id: $id}) DETACH DELETE m`: `skmemory/backends/age_backend.py:418-443`; `delete()` is an alias: `age_backend.py:453-455`.
- Cascade status: GAP (see Gap B). The primitive exists but `MemoryStore.forget()` never calls it in the shipped CLI or MCP wiring, so AGE `Memory` nodes are orphaned on forget. `reconcile` prunes the `memories` table only, not AGE nodes.

### 1.7 FalkorDB SKGraph (`{agent}_knowledge`)

- Store: FalkorDB graph at `192.168.0.59:16379`, node label `Memory` plus edges.
- Key: `Memory.id`.
- Role: `graph` when an skgraph URL is configured (`skmemory/cli.py:262-271`).
- Delete primitive: `SKGraphBackend.remove_memory()` runs `Q.DELETE_MEMORY` (a `DETACH DELETE`): `skmemory/backends/skgraph_backend.py:713-733`; `delete()` alias at `:698-711`.
- Cascade status: COVERED when configured. `forget()` step 3 calls `self.graph.remove_memory(...)` and `SKGraphBackend` is the one backend actually wired into the `graph` role. Note: only one graph role can be set, so a deployment gets FalkorDB OR AGE cleanup from `forget()`, never both, and by default (no skgraph URL) neither.

### 1.8 Write-ahead log (WAL)

- Store: the store's WAL file, append-only.
- Key: `memory_id` recorded in each `forget` pending / done / failed entry (`skmemory/wal.py:47-89`; written by `forget()` at `store.py:999,1002,1005`).
- Delete primitive: NONE by design. The WAL is an append-only audit trail; it is not purged on forget.
- Cascade status: INTENTIONALLY RETAINED. The forgotten id remains referenced in the WAL. If a "forget" must be evidence-free (redaction / sovereignty), the WAL is a residual reference worth a policy decision, not a bug. Flagged as Gap D (low priority, audit-vs-erasure tension).

### 1.9 Song anchors (sonic FEBs)

- Store: `~/.skcapstone/agents/<agent>/memory/songs/<anchor_id>/` directories (audio + spectrogram + meta.json).
- Key: `anchor_id` (directory name), NOT a `memory.id`.
- Delete primitive: NONE. `songs.py` and `songs_cli.py` only scan / score / match anchors; there is no delete or rmtree (`skmemory/songs.py`, `skmemory/songs_cli.py`).
- Cascade status: OUT OF SCOPE for `memory_forget` (anchors are not keyed by memory id), but flagged as Gap E: there is no supported "forget a song anchor" primitive at all; removal is a manual `rm -rf` of the directory.

### 1.10 Recall / graph-projection cache

- Store: recall cache docs and graph-state under the memory dir, keyed by `source_ref` (`skmemory/recall_cache.py:54,60,65`).
- Key: `source_ref` (a source path / reference), NOT `memory.id`.
- Delete primitive: NONE per-item. Caches are rebuilt wholesale by the build scripts; there is no `forget(source_ref)` invalidation.
- Cascade status: LOW-RISK GAP (Gap F). Because it is keyed by source and rebuilt, a forgotten memory leaves no id here directly, but a stale projection can persist until the next full cache rebuild.

---

## Part 2: skingest (document id)

A skingest document is identified by its relative source path `rel` (the source
file). Chunks add `chunk_idx`. The forget path is the `deleted` branch of the
pipeline (`~/clawd/skingest/src/skingest/pipeline.py:537-558`): for each removed
source it calls `delete_source(rel)` then `delete_document(rel)`, then
`mark_tombstone(purged)`.

### 2.1 skmem-pg `docs` table

- Store: skmem-pg table `docs` (`PG_DOCS_TABLE = "docs"`, `~/clawd/skingest/src/skingest/config.py:123`), columns `id, corpus, source, chunk_idx, content, meta(jsonb), embedding, agent`.
- Key: `source` (one document = many rows, one per `chunk_idx`).
- Delete primitives:
  - `delete_source(source_file)` runs `DELETE FROM docs WHERE source=%s` (all chunks): `~/clawd/skingest/src/skingest/stores/pg_upsert.py:163-176`.
  - `delete_chunks_above(source_file, min_idx)` trims stale tail chunks on re-ingest shrink: `pg_upsert.py:181-197` (called at `pipeline.py:289`).
- Cascade status: COVERED. `pipeline.py:542` calls `delete_source(rel)` for deleted sources.

### 2.2 skmem-pg AGE graph Document node

- Store: AGE graph (`PG_AGE_GRAPH`, default `lumina_knowledge`, `config.py:125`), node label `Document {path}` plus edges to Entity nodes.
- Key: `path` (the source file).
- Delete primitive: `delete_document(source_file)` runs `MATCH (n:Document {path: '...'}) DETACH DELETE n`: `~/clawd/skingest/src/skingest/stores/pg_graph.py:256-270`.
- Cascade status: PARTIAL (Gap C-graph). `pipeline.py:549` calls it best-effort (failure is logged, not fatal: `pipeline.py:550-553`). `DETACH DELETE` on the Document removes the node and its direct edges, but Entity / Concept / Person nodes that were linked to it (`upsert_relationships`, `pg_graph.py:207` onward) are NOT deleted. They are intentionally shared across documents, so leaving them is usually correct, but a document that introduced a now-unreferenced entity leaves that entity orphaned. Worth an entity-GC follow-up.

### 2.3 Wiki canon entity node (`.md`)

- Store: `<wiki_root>/pages/entities/<namespace>/<slug>.md`, written by `emit_entity_node()` (`~/clawd/skingest/src/skingest/canon.py:76,113`), git-committed (`canon.py:116`). `wiki_root` defaults to `SKINGEST_WIKI` (`~/clawd/wiki`, `config.py:23`).
- Key: `slug` / `_make_entity_id(source_file)` derived deterministically from the source path (`canon.py:28-42`).
- Delete primitive: MISSING. `canon.py` has `emit_entity_node` and `promote_lifecycle` (`canon.py:136`) but no delete / unlink for a node file. The pipeline delete branch (`pipeline.py:539-558`) removes docs rows and the AGE Document node but never removes the wiki `.md`.
- Cascade status: GAP (see Gap G). Forgetting a source leaves its wiki entity page (and its git history) on disk.

### 2.4 skingest tombstone / processed state

- Store: skingest control-plane state (per-node), tombstone + processed records.
- Key: `rel` (source path).
- Primitive: `mark_tombstone(purged)` records the deletion (`~/clawd/skingest/src/skingest/control_plane.py:158`), called at `pipeline.py:558`. This is a record-of-deletion, not a purge; it is the mechanism that prevents a tombstoned source from being re-ingested.
- Cascade status: COVERED (records deletion; intentionally retained).

---

## Part 3: Forget-cascade (ordered deletes)

### 3.1 A full `memory_forget(id)` should perform, in order

1. Primary flat JSON + SQLite index. `SQLiteBackend.delete(id)` (`sqlite_backend.py:526-550`). Removes the source-of-truth file and the `memories` index row. This is `forget()` step 1 and already runs.
2. Vector store for the memory (exactly one of the three, whichever is wired):
   - Chroma: `SKChromaBackend.remove(id)` (chunk cascade). COVERED.
   - Qdrant: `SKVectorBackend.remove(id)` (chunk cascade). COVERED.
   - pgvector: MUST call `PGVectorBackend.delete(id)`. Today `forget()` calls `.remove()`, which does not exist, so this step no-ops (Gap A). Interim safety net: `reconcile` prune (`reconcile.py:196`).
3. Knowledge graph node(s) for the memory:
   - FalkorDB: `SKGraphBackend.remove_memory(id)`. COVERED when wired.
   - skmem-pg AGE: `AGEGraphBackend.remove_memory(id)` (`age_backend.py:418`). NOT wired into `forget()` (Gap B). A complete cascade must delete the `Memory` node in BOTH graphs the deployment populates, not just the single `graph` role.
4. (Policy) WAL: decide whether the forgotten id is scrubbed or retained for audit (Gap D).

To leave zero orphans on the default (pgvector + optional graphs) deployment,
`forget()` must additionally: call `PGVectorBackend.delete` (not `remove`), and
DETACH DELETE the AGE `Memory` node. Neither happens today.

### 3.2 A full skingest source-forget(rel) should perform, in order

1. `delete_source(rel)` on the `docs` table (`pg_upsert.py:163`). COVERED (`pipeline.py:542`).
2. `delete_document(rel)` on the AGE graph Document node (`pg_graph.py:256`). COVERED best-effort (`pipeline.py:549`); does not GC now-orphaned entities (Gap C-graph).
3. Delete the wiki entity `.md` at `pages/entities/<ns>/<slug>.md` and git-commit the removal. MISSING primitive (Gap G).
4. `mark_tombstone([rel])` (`control_plane.py:158`). COVERED (`pipeline.py:558`).

---

## Part 4: Missing-primitive gaps (follow-up cards)

| Gap | Store | Problem | Fix direction |
|-----|-------|---------|---------------|
| A | skmem-pg pgvector `memories` | `MemoryStore.forget()` calls `self.vector.remove()` (`store.py:1011`) but `PGVectorBackend` has only `delete()` (`pgvector_backend.py:177`), no `remove()`. On the default deployment the pg row is not deleted at forget time; it survives until `reconcile` prune. | Add `PGVectorBackend.remove` (alias to `delete`, optional chunk cascade) OR make `forget()` fall back to `delete()` when `remove` is absent. |
| B | skmem-pg AGE `<agent>_knowledge` | `AGEGraphBackend.remove_memory` exists (`age_backend.py:418`) but AGE is never wired as `store.graph` in `cli._get_store` / `mcp_server._get_store`; `forget()` never deletes the AGE `Memory` node. Orphan on every forget. | Wire AGE into the `graph` role, or have `forget()` fan out to all populated graphs (AGE + FalkorDB), not a single role. |
| C-graph | skmem-pg AGE (skingest) | `delete_document` DETACH-deletes the Document node but leaves Entity/Concept/Person nodes it introduced (`pg_graph.py:207,256`). | Add an entity garbage-collect pass (delete Entity nodes with zero remaining Document edges). |
| D | WAL | Forgotten id remains in the append-only WAL (`wal.py:47`). Audit-vs-erasure tension for a true redaction. | Policy decision: WAL scrub / tombstone mode for redaction-grade forgets. |
| E | Song anchors | No delete primitive anywhere (`songs.py`, `songs_cli.py`); removal is manual `rm -rf`. | Add `skmemory songs forget <anchor_id>` (rmtree + index update). Not memory-id-keyed, low priority. |
| F | Recall / projection cache | Keyed by `source_ref`, rebuilt wholesale, no per-item invalidation (`recall_cache.py:54`). | Add targeted cache invalidation on forget, or document rebuild-on-forget. Low risk. |
| G | Wiki canon `.md` (skingest) | `emit_entity_node` writes `pages/entities/.../slug.md` (`canon.py:113`) but there is no delete primitive; source-forget leaves the wiki page and its git history. | Add `canon.delete_entity_node(source_file)` (unlink + git-commit removal) and call it from `pipeline.py`'s deleted branch. |

### Gap severity

- HIGH: A and B. Both leave a forgotten memory recallable from a live derived store (pgvector search, AGE traversal) on the default deployment. These are the two that break "forget really forgot it."
- MEDIUM: C-graph and G. Residual graph/wiki artifacts that leak the fact and content of a forgotten document.
- LOW / policy: D, E, F.
