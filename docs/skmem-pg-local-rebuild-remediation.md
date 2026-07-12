# skmem-pg Remediation Plan — prb-6f069c5e (streaming-replication → local-per-node rebuild)

## 1. Executive summary

The .41 skmem-pg is a frozen, non-functional streaming standby (ParadeDB Community cannot serve `pg_search` reads in recovery, so BM25/hybrid RRF has never worked on .41), while every agent's Syncthing-synced `skmemory.yaml` hardcodes `pgvector_dsn=192.168.0.158:5432` — making .158 a live SPOF that the local-per-node decision has not actually retired. The fix is to `pg_promote` .41 in place, reconcile its `memories` table to the synced flat-file truth, logical-copy the `docs`/`file_locations` corpora from .158, re-home .41 agents to `localhost`, and install per-node reconcile timers. In parallel, the repos must be corrected: the `:5433` standby-port defaults and all "central/streaming-replication" framing in code and docs are stale and actively dangerous under the new model.

---

## 2. .41 re-home procedure (RECOMMENDED — Option A: promote in place)

Run on **.41** unless noted. **.158 is never modified** (it is the ultimate rollback). Sequencing rule: promote + reconcile + verify FIRST, flip agent DSN LAST, so no writes are lost.

**Step 0 — Pre-flight (verify, deploy tooling)**
```
# Confirm opus/jarvis still write to .158 (no write-loss window yet)
grep pgvector_dsn ~/.skcapstone/agents/{opus,jarvis}/config/skmemory.yaml   # expect 192.168.0.158:5432

# Deploy the reconcile engine to .41 (absent today)
scp noroc2027:~/skmem-build/skmem_reconcile.py 192.168.0.41:~/skmem-build/

# Confirm .100 mxbai embed reachable FROM .41
curl -s http://192.168.0.100:11434/api/embed -d '{"model":"mxbai-embed-large","input":"x"}' | head -c 80
```

**Step 1 — Mandatory backup (promote is irreversible)**
```
docker stop skmem-pg
docker run --rm -v skmem_pgdata:/v -v ~/skmem-backup:/b busybox \
  tar czf /b/skmem_pgdata-pre-promote-$(date +%F).tgz -C /v .
docker start skmem-pg
```

**Step 2 — Promote to writable primary**
```
docker exec skmem-pg psql -U postgres -d skmemory -c "select pg_promote(true,60);"
docker exec skmem-pg psql -U postgres -d skmemory -c "select pg_is_in_recovery();"   # expect f
```

**Step 3 — Clean standby config (prevent accidental re-standby / new WAL slot on .158)**
```
docker exec skmem-pg psql -U postgres -d skmemory -c \
  "ALTER SYSTEM RESET primary_conninfo; ALTER SYSTEM RESET primary_slot_name;"
docker exec skmem-pg psql -U postgres -c "select pg_reload_conf();"
# Confirm no standby.signal remains in the volume
docker run --rm -v skmem_pgdata:/v busybox ls /v/standby.signal 2>&1   # expect: not found
```

**Step 4 — Reconcile `memories` to local flat truth (idempotent, agent-scoped)**
```
for a in lumina opus jarvis; do python3 ~/skmem-build/skmem_reconcile.py $a; done
# Expect: lumina PRUNES ~2821 stale orphans (17160→14339); opus backfills ~3857 (10→3867);
#         jarvis backfills ~2689 (0→2689); null vectors embedded via mxbai on .100.
```

**Step 5 — Refresh shared corpora from .158 (NOT covered by reconcile; no local builder on .41)**
```
# On .158:
ssh noroc2027 'docker exec skmem-pg pg_dump -U postgres -Fc -t docs -t file_locations skmemory' \
  > /tmp/skmem_corpora.dump
# On .41:
docker exec -i skmem-pg pg_restore -U postgres -d skmemory --clean --if-exists \
  -t docs -t file_locations < /tmp/skmem_corpora.dump
# (Alternative: defer and keep Jun22-stale docs 53651, flagged known-stale.)
```

**Step 6 — Re-home .41 agents (do this LAST)**
Edit on .41: `~/.skcapstone/agents/opus/config/skmemory.yaml` (line 22) and `~/.skcapstone/agents/jarvis/config/skmemory.yaml` (line 9):
`postgresql://postgres:skmemory@192.168.0.158:5432/skmemory` → `postgresql://postgres:skmemory@localhost:5433/skmemory`.
**Preferred (SPOF-proof, see §4):** instead remove `pgvector_dsn` from the synced yaml and let node-local `SKMEMORY_PG_DSN` in `~/.bashrc` drive it. Then restart the opus/jarvis daemons.

**Step 7 — Install per-node reconcile + corpora-refresh timers on .41** (see §3).

**Verification**
```
docker exec skmem-pg psql -U postgres -d skmemory -c "select pg_is_in_recovery();"   # f
docker exec skmem-pg psql -U postgres -d skmemory -c \
  "select agent, count(*), count(embedding) from memories group by agent;"
#  lumina ~14339 / all-embedded, opus ~3867, jarvis ~2689  (== .158 and == local flat counts)
docker exec skmem-pg psql -U postgres -d skmemory -c "select count(*) from docs;"           # 60562
docker exec skmem-pg psql -U postgres -d skmemory -c "select count(*) from file_locations;" # 11653
# A hybrid/RRF memory_search for ONE agent returns sane hits with NO ParadeDB-standby error,
# via localhost:5433, BEFORE flipping the other agents.
```

**Rollback**
- `pg_promote` is one-way. Volume rollback = `docker stop skmem-pg` → wipe `skmem_pgdata` → `tar xzf` the Step-1 backup → start (returns .41 to the non-functional standby snapshot).
- **Operational rollback** (preferred) = revert .41 agent DSN back to `192.168.0.158:5432` (unchanged, authoritative) and restart daemons. .158 is untouched throughout.

---

## 3. Per-node reconcile scheduling design

**Invariant:** each node runs its OWN writable skmem-pg on `localhost`; agents point only at `localhost`; the pg is a derived, rebuildable cache (same class as `index.db`) — `memories` rebuilt from Syncthing-synced flat JSON via `skmem_reconcile.py`, `docs`/`file_locations` rebuilt from the wiki via skingest. No streaming replication, no failover, no remote primary.

**Reconcile is structurally node-local:** `skmem_reconcile.py` reaches pg via `docker exec skmem-pg psql` with no host param — it can only ever act on the box it runs on. It must therefore be *present and scheduled on every node*, for every agent whose flat files that node serves.

| Node | Local pg | Agents to reconcile | Current state | Action |
|---|---|---|---|---|
| .158 (noroc2027) | :5432 | lumina 4:15, opus 4:25, jarvis 4:35 | **already cronned for all three** (context "only-lumina" premise is stale for .158) | keep; add drift-check |
| .41 | :5433 | opus, jarvis (+ lumina optional) | **no script, no cron, no timers** | deploy + schedule |

**.41 scheduling (systemd --user template preferred over cron):**
- `skmem-reconcile@.service` (Type=oneshot, `ExecStart=python3 %h/skmem-build/skmem_reconcile.py %i`) + `skmem-reconcile@.timer` instantiated for `opus` and `jarvis` on the same cadence as .158 (~04:2x, staggered).
- Confirm `.100` mxbai embed reachable from .41 at timer time (it embeds null vectors).

**Docs corpus handling (two independent rebuild paths — both must be covered):**
1. `memories` ← `skmem_reconcile.py` from `~/.skcapstone/agents/<a>/memory/**` (synced flat JSON). Covered by the timers above.
2. `docs` + `file_locations` ← skingest from the wiki. **Not rebuildable on .41 today** (no `~/clawd/wiki`, no skingest, no `~/.skingest` ledger).
   - **Interim (recommended now):** a per-node timer `skmem-corpora-pull@.41` that runs the Step-5 `pg_dump -t docs -t file_locations` from .158 → `pg_restore` locally, daily. This keeps .41 from drifting (currently ~7k rows stale) until skingest is local.
   - **Long-term (decide first):** either (a) deploy wiki + skingest to .41 for true local rebuild (git-clone the wiki — it is NOT a Syncthing folder — and install `skingest-maintain.timer`/`skingest-sync.timer`, seeding with a one-shot `FULL=1` or a `pg_dump` baseline to avoid the ~13k-file re-embed that tripped the 900s timeout), OR (b) **confirm .41's opus/jarvis never call `retrieve.search`/`hybrid_search_docs`** and skip docs on .41 entirely (halves the per-node burden).
   - **Never sync `~/.skingest/state/corpus-processing-state.json` to .41** — if that ledger arrives, skingest thinks all files are ingested and leaves the empty local pg empty. Keep `~/.skingest` strictly per-node.

**Health/backup parity on .41 (currently zero):**
- `skmem-health.sh` must loop over the node-resident agents (opus, jarvis) and honor the node-local port (`:5433` on .41 — the `localhost:5432` default false-FAILs there). Schedule a per-node run.
- `skmem-pg-backup.sh` optional (cache is rebuildable) but schedule for redundancy; write dumps to a node-neutral path, not `agents/lumina/`.
- Add a **per-node drift detector** (flat-count vs pg-count per agent) that alerts via sk-alert — the only signal that a silent rebuild produced a partial graph/table.

---

## 4. Code changes needed (grouped by repo)

### skmemory (`~/clawd/skcapstone-repos/skmemory`)
| File / line | Change |
|---|---|
| `skmemory/backends/pgvector_backend.py` **:33-34** | `DEFAULT_DSN` `localhost:5433` → **`localhost:5432`**. `:5433` is the retired standby port; an unconfigured node currently targets a read-only standby and every `save()`/`delete()` raises. |
| `skmemory/backends/pgvector_backend.py` **:1-17** | Rewrite module docstring: remove "central Postgres" and "streaming/logical replication"/"snapshot-shipped"; state **local writable per-node pg; embeddings = rebuildable cache from flat + deterministic mxbai via `skmem_reconcile.py`**. |
| `skmemory/backends/pgvector_backend.py` **:71** | `_connection()` opens `autocommit=True` with no read-only guard. Add a startup assertion / `health_check` that `pg_is_in_recovery()=false` (fail loud if pointed at a replica). |
| `skmemory/backends/age_backend.py` **:60** | Already `localhost:5432` — keep, but **centralize the local-DSN default** so pgvector/age/reconcile can't drift to different ports/DBs on the same node. |
| `skmemory/config.py` **:87, :165** | Add `pgvector_dsn` to `merge_env_and_config` precedence (**CLI > `SKMEMORY_PG_DSN` env > `cfg.pgvector_dsn` > `localhost:5432`**), mirroring skvector resolution; update field comment to state node-LOCAL writable DSN. |
| `skmemory/cli.py` **:154** | Add `--pg-dsn` and wire the single precedence chain above (today pg DSN precedence is split/implicit → `:5433`). |
| `skmemory/mcp_server.py` **:72** | Same `:5433→:5432` flows through. **Reconsider the Chroma fallback** — Chroma is RETIRED; an unreachable local pg should surface loudly (or fall back to SQLite/BM25 recency), not silently init a retired empty vector store. |

### skingest (`~/clawd/skingest`)
| File / line | Change |
|---|---|
| `src/skingest/config.py` **:96** | `PG_DSN` already `localhost:5432` (localhost-relative) — **no code change**; this is why skingest is a near-zero-change fit. Document per-node intent. |
| `src/skingest/distributed.py` **:181** | Latent bug: `SELECT DISTINCT source_file FROM docs` — column is `source`. Query raises → skip-check silently disabled. Fix column name (only matters if the deferred distributed fan-out is revived; note that a coordinator-side skip-check is semantically wrong under per-node pg). |
| `cluster-inventory.json` **:9, :30, :50** | Node roles hardcode noroc2027 as the single central `skdata(skmem-pg)` / "single writer". Reword to **per-node localhost sink** (coordinator orchestrates embed fan-out; each node writes/rebuilds its own `docs`). |
| `src/skingest/config.py` **:118** | `docs.agent`/`PG_AGE_GRAPH` default `lumina`/`lumina_knowledge` for agent-agnostic wiki canon. Decide: keep a shared `canon` scope, or have .41 agents query `hybrid_search_docs(agent_filter=NULL)`. |

### Out-of-repo ops scripts + node config
| File | Change |
|---|---|
| `~/.skcapstone/agents/{lumina,opus,jarvis}/config/skmemory.yaml` (lines 10 / 22 / 9) | **Remove the hardcoded `pgvector_dsn=192.168.0.158:5432`** from these Syncthing-synced files (a synced file must never carry a node IP). Let node-local `SKMEMORY_PG_DSN` resolve. **Verify precedence in `context_loader.py` FIRST** — confirm env wins over yaml before flipping, or the change silently no-ops. |
| `~/skmem-build/skmem_reconcile.py` | Deploy to .41 (absent). Runs unmodified against the local container. Vendor into skmemory (see §6). Default `AGENT=lumina` (line 11) — always invoke explicitly per agent. |
| `~/.hermes/scripts/skmem-health.sh` **:8** | Parametrize/loop over node-resident agents; honor node-local port (`:5433` on .41). `SKMEMORY_HEALTH_DSN` default won't match .41. |
| `~/.skcapstone/scripts/skmem-pg-backup.sh` **:11** | Node-neutral dump path (not `agents/lumina/`); schedule on .41 too. |
| `~/.config/skmemory/pg.env` | Per-node template; ensure .41 has `SKMEMORY_PG_DSN=localhost:5433` (never `@192.168.0.158`, never bare `:5432` unless the container is remapped). Drop ".158-only" header framing. |
| `~/.bashrc` (.158 `:5432`, .41 `:5433`) | Make node-local env the single source of truth once yaml override is removed. (Optionally remap the .41 container to host `:5432` for fleet-wide port uniformity — then set .41 env to `:5432` and standardize everything on `:5432`.) |

---

## 5. Docs to update

### skmemory
- **`docs/deploy-plan/skmemory-bulletproof-deploy.md`** — PRIMARY REWRITE. Goal #3 (L27), G1 (L37-38), P2a (L69), P3b lag-check (L75), P4a replication-auth (L80), item #6 (L92): replace ".158→.41 streaming/logical replication + failover" with per-node writable pg rebuilt from synced flat + git-wiki; SPOF resolved by node self-sufficiency, not replication. Keep P2b off-box dump as backup-only. Promote P3c multi-agent+multi-node reconcile to the **core HA mechanism**.
- **`docs/ARCHITECTURE.md`** (2026-07 callout, L3-18 + rebuildable-index §98-114) — add explicit topology paragraph: skmem-pg is local-per-node (`localhost:5432`), a rebuildable derived cache (same class as `index.db`), NOT streaming-replicated, NOT a SPOF; reconcile runs per-node; flag that docs are canon-sourced (not agent flat).
- **`skmemory/README.md`** (L9-16) — stop calling skmem-pg "the central pgvector store" for agent memory; separate per-agent local store from shared recall corpora; drop "central" as a physical claim.
- **`README.md`** (L742-743 `docs` "shared"; L570-580 HA primary/replica) — `docs` is a per-node local rebuild via skingest; scope the primary/replica/failover language to the **retired** SKVector(Qdrant)/SKGraph(FalkorDB) endpoints ONLY, with an explicit "skmem-pg is not run primary/replica" note.
- **`SOP.md`** (L162-165 "shared … over the tailnet"; L108 `:5433` drift; L101-103 invariant list) — change to localhost per-node; strengthen the key-invariant list to name skmem-pg as per-node-rebuildable with a "no remote primary" clause.
- **`skmemory/HA.md`** (L261) — scope the primary/replica selector to shared Qdrant/SKVector recall endpoints only; state skmem-pg is never a replicated primary/replica.
- **`CHANGELOG.md`** — leave history (L83-101) intact; add a new entry documenting the move from replicated-central to local-per-node rebuild-from-flat, including the `:5433→:5432` default fix.

### skingest
- **`docs/CLUSTER-DISTRIBUTED-EMBEDDING.md`** (L5, L20-23, L35) — embed fan-out stays distributed, but the pg write target is each node's LOCAL skmem-pg; docs rebuilt per-node from canon.
- **`SOP.md`** (L42, L58, L137) — ingest runs per-node against localhost; each node rebuilds its own docs from synced canon. (L32 "git wiki canonical; skmem-pg derived index" already correct.)
- **`cluster-inventory.json`** (L30, L50) — per-node local skmem-pg; coordinator does not own the only writable copy.
- **`docs/SOVEREIGNTY.md`** (L25-35) — "your local Postgres on noroc2027" → "each node's local Postgres".
- **`README.md`** (L96-101) and **`ADAPTATION-PLAN.md`** (L26 "noroc2027:5432") — each node has its own skmem-pg; skingest populates the LOCAL docs table.

### skcapstone
- **`README.md`** (L496/499/509 "skmem-pg + Syncthing … knowledge substrate") and **`docs/ARCHITECTURE.md`** (L881, L912) — frame skmem-pg as a per-node derived index rebuilt from synced flat+wiki, not a shared/replicated substrate.
- **`docs/BACKUP.md`** (L86, L127-132) — extend restore to cover skmem-pg rebuild (memories via reconcile AND docs via skingest re-ingest); note the pg dump is optional/backup-only.

### Runbooks & agent-facing memory
- **`~/clawd/runbooks/runbook-skmem-pg-rehome-41.md`** (NEW) — capture §2 (promote+reconcile+corpora-copy+verify+rollback) for prb-6f069c5e. (No skmem runbook exists today.)
- **`~/clawd/runbooks/runbook-skmem-pg-down.md`** — per-node remediation: restart LOCAL container → reconcile from flat (+ skingest for docs); NO failover to .158; make verification counts per-node; fix `:5432` vs `:5433` note.
- **`~/.claude/CLAUDE.md`** (Infrastructure note) — ".158 = central Postgres … Mirrored .41 (:5433, empty)" is now false. Rewrite to local-per-node: each node runs its own writable skmem-pg; agents → localhost; rebuilt from synced flat + wiki; record that **ParadeDB Community cannot serve `pg_search` reads on a standby** (why streaming was abandoned) and that `docs`/`file_locations` are a .158-owned shared corpus needing a local builder or logical push.
- **`~/.claude/projects/-home-cbrd21-clawd/memory/MEMORY.md`** (L81 topology; L49 "NEXT: skmem-pg replication") — .41 runs its OWN writable skmem-pg (not an empty standby); retire the replication NEXT item.
- **`~/clawd/skos/docs/deploy-plan/skos-bulletproof-deploy.md`** (L40, L98, L126) — lower priority: scope the standby/failover language to the **scheduler**, or align to per-node rebuild, to stay consistent with the memory-layer decision.

---

## 6. Tests / TDD to add or update

**Unresolved decision blocking the docs:** whether `docs`/shared recall corpora become per-node rebuilds or remain a deliberate central exception. Resolve this before finalizing docs, or the SPOF just moves to the corpora store. Tests below assume the per-node model.

| Repo / file | Invariant to assert |
|---|---|
| **skmemory `tests/test_reconcile_invariant.py`** (NEW; vendor `skmem_reconcile.py` from `~/skmem-build` into the repo first) | From an EMPTY pg + flat fixtures, reconcile: (a) backfills every flat memory with a non-null embedding, (b) prunes pg rows whose flat file is gone, (c) `flat_count == pg_count` per agent, (d) is idempotent (2nd run = 0 backfilled / 0 pruned). **Zero coverage today** despite this being the linchpin of the model. |
| **skmemory `tests/test_pgvector_backend.py`** (NEW) | (1) `DEFAULT_DSN`/`SKMEMORY_PG_DSN` resolves to **localhost:5432** when env unset (no hardcoded `192.168.0.158`/`.41` in the write path); (2) `save()`→`load()`/`delete()` round-trip; (3) idempotent-rebuild (`ON CONFLICT (id) DO UPDATE` yields identical rows); (4) when local pg is unreachable, backend fails loud / falls back to local SQLite — never silently reads a remote primary. |
| **skmemory `tests/test_age_backend.py`** (extend `test_sync_all_indexes_flat_files` @652; DSN @33) | AGE graph fully rebuildable-from-empty (node/edge-count parity after clean `sync_all` from flat); DSN default localhost-only; no-remote-primary assertion. The 33k-node graph is the biggest thing losing its only off-node copy when replication is dropped. |
| **skmemory `tests/test_live_dedup_e2e.py`** (@27) & **`test_age_backend.py`** (@33) | Both already hardcode `localhost:5432` (contradicting the old `:5433` backend default). Parametrize DSN from `SKMEMORY_PG_DSN` so they pass on a node whose container maps `:5433`; add an explicit assertion that the resolved DSN is a LOCAL writable port, not the standby. |
| **skmemory `tests/test_endpoint_selector.py`** | Regression: the primary/replica failover selector is NOT applied to the skmem-pg write path (skmem-pg DSN must resolve local, never replica-of-remote-primary). |
| **skingest `tests/test_phase1.py`** | Docs rebuild-parity: `delete_source` then re-ingest same file → identical chunk set (idempotent from canon); upsert writes only to LOCAL `PG_DSN`; assert `PG_DSN` resolves per-node (localhost, never `@192.168.0.158`). |
| **skingest `tests/test_health.py`** / **`test_cluster.py`** | Preflight passes on a node with its OWN local skmem-pg and does NOT require reachability to a remote/central primary (probe target = localhost); pg write target resolves to the local node, not forced to the coordinator. |
| **skingest `tests/test_p2_p3_p4.py`** | After the `distributed.py:181` `source_file`→`source` fix, assert the docs skip-check returns non-empty against a populated local pg. |

---

## 7. Ordered execution checklist (with risk notes)

1. **Resolve the two open decisions first** — (a) config precedence: confirm in `context_loader.py` whether env `SKMEMORY_PG_DSN` beats yaml `pgvector_dsn` (today yaml wins → .41 writes to .158). (b) Does .41 opus/jarvis actually query `docs`/`hybrid_search_docs`? If no, skip skingest on .41 entirely.
   *Risk: changing the DSN lever before verifying precedence can silently flip which pg agents write to.*
2. **Fix the latent write-path bug in code (repo, low-risk, do before touching prod):** `pgvector_backend.py` `DEFAULT_DSN :5433→:5432` + docstring rewrite; `config.py`/`cli.py` precedence chain; align `age_backend.py` port. Land the `test_pgvector_backend.py` DSN assertions in the same PR.
   *Risk: any node constructing the backend without an explicit env override currently targets the read-only standby and every write raises — this is already latent.*
3. **Standardize the local port decision** (recommend `:5432` fleet-wide; if keeping .41 on `:5433`, document it as the node-local port everywhere). Update `pg.env`/`~/.bashrc` per node.
   *Risk: port drift (`:5432` vs `:5433` across container/backend/age/bashrc) makes vector vs graph backends resolve to different DBs on the same node.*
4. **Execute the .41 re-home (§2), steps 0-5** — pre-flight, backup, promote, clean config, reconcile, corpora copy. Verify before touching agents.
   *Risk: `pg_promote` is irreversible — Step-1 backup is mandatory. Verify the lumina PRUNE count (~2821) before flipping, or .41 recall surfaces already-deleted memories.*
5. **Remove hardcoded host DSN from the three synced `skmemory.yaml`** (or set to `localhost` per §2 step 6), then **re-home .41 agents LAST** and restart daemons; verify one agent's recall via `localhost` before flipping the rest.
   *Risk: this is the live SPOF fix — until the synced yaml stops carrying `192.168.0.158`, .41 agents depend on .158 despite having a local pg.*
6. **Install per-node timers on .41 (§3):** `skmem-reconcile@{opus,jarvis}` + interim `skmem-corpora-pull` (docs/file_locations from .158) + `skmem-health` + optional backup. Confirm `.100` mxbai reachable from .41.
   *Risk: without these, .41's local pg silently drifts (docs already ~7k rows stale); the "rebuilt per-node" claim isn't true until the timers exist.*
7. **Vendor `skmem_reconcile.py` into skmemory + land `test_reconcile_invariant.py`** and the remaining skingest/age/endpoint tests.
   *Risk: the linchpin rebuild engine lives out-of-repo with zero tests; don't let docs claim it as the HA mechanism until it's tested.*
8. **Fix `skingest/distributed.py:181` column bug** and update `cluster-inventory.json`/skingest docs (deferred-path correctness; do opportunistically).
9. **Update all docs + agent memory (§5)** to the per-node model — do this AFTER the decisions in step 1 and the timers in step 6 exist, so docs don't claim per-node rebuild before it's enforced.
   *Risk: docs/code split-brain gives future agents contradictory topology guidance; and the "flat files are source of truth" line is memories-only — docs are canon-sourced and must be documented as such.*
10. **Long-term:** decide wiki+skingest-on-.41 vs docs-central-exception; retain off-box dumps (bulletproof P2b) as the safety net for the 33k-node AGE graph, which loses its only off-node copy when replication is dropped; keep the per-node flat-vs-pg drift detector running as the only signal of a partial rebuild.