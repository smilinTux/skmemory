# Runbook: restore an agent's memory from backup (drilled)

Scope: restore ONE agent (example: `lumina`) from its two backup artifacts into a
scratch location, prove the copy is healthy, then delete it. The same steps, with
the targets changed as noted in "Real disaster", rebuild a lost node.

Drilled 2026-11-06 on noroc2027 against lumina (card SKMEM-W-04). Every command in
a fenced `bash` block below runs top to bottom in ONE shell. The doc holds no DSN,
password or memory content: credentials stay in files you point at by path.

## Measured on lumina (2026-11-06, noroc2027)

| Step | Size | Time |
| --- | --- | --- |
| Flat copy (44,088 tier files + 8,549 tombstones, 220 MB) | 220 MB | 12 s |
| `pg_dump -Fc` of the whole `skmemory` DB | 2.9 GB DB, 701 MB dump | 79 to 88 s |
| `pg_restore` into a new DB (single-threaded index builds) | | 301 to 496 s (two runs) |
| AGE oid repair | | under 1 s |
| `skmemory --no-vector reindex` (SQLite from 44,085 files) | 86 MB | 242 to 253 s |
| `skmemory health` | | about 25 s |
| Whole runbook, end to end | about 4 GB peak scratch | 12 to 15 min |

## What gets backed up, and what does not

| Artifact | Produced by | Holds |
| --- | --- | --- |
| Flat tiers (source of truth) | `memory/{short-term,mid-term,long-term}` | every memory as one JSON file |
| Tombstones | `memory/tombstones/` | ids that were forgotten and must stay gone |
| Postgres dump | `deploy/ops/skmem-pg-backup.sh` (`pg_dump -Fc`, whole `skmemory` DB) | memories + embeddings, docs, BM25 and HNSW indexes, ALL agents' AGE graphs |
| SQLite index | derived | rebuilt from the flat files, never restored |

Found by this drill and since fixed: the nightly tarball from
`scripts/memory-backup.sh` used to leave out `memory/tombstones/` and
`memory/archive/`, so restoring it alone could resurrect forgotten memories on the
next reconcile. The script now includes both and fails if either is missing. This
runbook still copies tombstones explicitly. What is backed up, how often and for
how long is the standard in [BACKUP_SOP.md](BACKUP_SOP.md).

## 0. Variables and preflight

Set these once. Nothing below targets a production path or database.

```bash
set -euo pipefail
export AGENT=lumina
export SRC_HOME="$HOME/.skcapstone"                          # live skcapstone home (read only)
export DRILL="$HOME/.local/state/skmem-overhaul/scratch/drill-rb"   # NEW empty dir, nothing else
export PG_SRC_CONTAINER=skmem-pg                              # production, dump only
export PG_SRC_DB=skmemory
export PG_SCRATCH_CONTAINER=skmem-pg-throwaway               # scratch server, never production
export PG_SCRATCH_PORT=55432
export SCRATCH_DB=skmem_restore_drill                        # NEW db name, must not exist
export SCRATCH_DSN_FILE="$HOME/.local/state/skmem-overhaul/throwaway.dsn"  # DSN of the scratch server, mode 600
export SK_PY="$HOME/.skenv/bin/python"
export SKMEM="$HOME/.skenv/bin/skmemory"
export M="$SRC_HOME/agents/$AGENT/memory"

# Refuse to run against anything that looks like production.
[ "$PG_SCRATCH_CONTAINER" != "$PG_SRC_CONTAINER" ] || { echo "scratch == source, stop"; exit 1; }
[ "$PG_SCRATCH_PORT" != "5432" ] || { echo "port 5432 is production, stop"; exit 1; }
[ ! -e "$DRILL" ] || { echo "$DRILL already exists, pick a new path"; exit 1; }

# Size and disk. Need roughly 4x the dump (about 4 GB for lumina) plus the flat copy.
du -sh "$M/short-term" "$M/mid-term" "$M/long-term" "$M/tombstones"
docker exec "$PG_SRC_CONTAINER" psql -X -U postgres -d "$PG_SRC_DB" -At -c "select pg_size_pretty(pg_database_size('$PG_SRC_DB'))"
df -h /
```

Stop if `df -h /` would leave under 8 GB free after roughly 4 GB of restore. In
that case sample instead (copy only `short-term`, restore a subset) and say so in
the drill record. The scratch Postgres needs the extensions `vector`, `pg_search`,
`age` and the same major version as the source (17).

## 1. Flat snapshot (read only on the source)

```bash
mkdir -p "$DRILL/home/agents/$AGENT/memory" "$DRILL/home/agents/$AGENT/config" "$DRILL/home/config"
START=$(date +%s)
rsync -a "$M/short-term" "$M/mid-term" "$M/long-term" "$M/tombstones" \
  "$DRILL/home/agents/$AGENT/memory/"
# Profile files the CLI needs to resolve the agent (no secrets in these).
cp "$SRC_HOME/config/profile-registry.json" "$DRILL/home/config/"
cp "$SRC_HOME/agents/$AGENT/profile.json" "$DRILL/home/agents/$AGENT/"
cp "$SRC_HOME/agents/$AGENT/config/skmemory.yaml" "$DRILL/home/agents/$AGENT/config/"
grep -c 'postgres' "$DRILL/home/agents/$AGENT/config/skmemory.yaml" || true   # expect 0: no DSN in it
echo "flat copy seconds: $(( $(date +%s) - START ))"

# Verify: same file list and content hashes on both sides.
sumtree() { (cd "$1" && find short-term mid-term long-term tombstones -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum); }
A=$(sumtree "$M"); B=$(sumtree "$DRILL/home/agents/$AGENT/memory")
echo "$A"; echo "$B"
[ "$A" = "$B" ] || echo "NOTE: differs. If a live writer added files since the copy, re-run; otherwise STOP."
for t in short-term mid-term long-term tombstones; do
  echo "$t files: $(find "$DRILL/home/agents/$AGENT/memory/$t" -type f | wc -l)"
done
```

## 2. Postgres dump (read only on the source)

`pg_dump` runs in a read-only snapshot transaction. Use the CONTAINER's client:
the host `pg_dump`/`pg_restore` is 16.x and the server is 17, and a v16 client
lists a v17 archive as zero entries and exits 0.

```bash
START=$(date +%s)
docker exec "$PG_SRC_CONTAINER" pg_dump -U postgres -d "$PG_SRC_DB" -Fc > "$DRILL/skmemory.dump"
echo "dump seconds: $(( $(date +%s) - START )), bytes: $(stat -c %s "$DRILL/skmemory.dump")"
head -c 5 "$DRILL/skmemory.dump"; echo            # expect PGDMP
docker cp "$DRILL/skmemory.dump" "$PG_SCRATCH_CONTAINER:/tmp/$SCRATCH_DB.dump"
docker exec "$PG_SCRATCH_CONTAINER" pg_restore --list "/tmp/$SCRATCH_DB.dump" | grep -c 'TABLE DATA'   # expect > 100
```

## 3. Restore into a NEW scratch database

Two traps handled here. Extensions must exist first (the dump reuses their
schemas, so `pg_restore` reports "schema paradedb/ag_catalog already exists":
harmless). And a parallel HNSW build in a Docker container with the default 64 MB
`/dev/shm` fails with "could not resize shared memory segment", so parallel
maintenance workers are turned off for the restore session.

```bash
PSQL_S() { docker exec -i "$PG_SCRATCH_CONTAINER" psql -X -U postgres -At -v ON_ERROR_STOP=1 "$@"; }
[ "$(PSQL_S -d postgres -c "select count(*) from pg_database where datname='$SCRATCH_DB'")" = "0" ] \
  || { echo "$SCRATCH_DB exists, STOP"; exit 1; }
PSQL_S -d postgres -c "CREATE DATABASE $SCRATCH_DB"
PSQL_S -d "$SCRATCH_DB" -c "CREATE EXTENSION vector; CREATE EXTENSION pg_search; CREATE EXTENSION age;"
START=$(date +%s)
docker exec -e PGOPTIONS='-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=512MB' \
  "$PG_SCRATCH_CONTAINER" pg_restore --no-owner --no-privileges -d "$SCRATCH_DB" \
  "/tmp/$SCRATCH_DB.dump" > "$DRILL/restore.log" 2>&1 || echo "pg_restore rc=$? (expected 1, see log)"
echo "restore seconds: $(( $(date +%s) - START ))"
grep -c 'error:' "$DRILL/restore.log" || true
grep 'error:' "$DRILL/restore.log" | cut -c1-160 || true   # expect only: schema paradedb / ag_catalog already exists
```

Expected: exactly 2 errors, both "schema ... already exists", and
"errors ignored on restore: 2". Anything else (especially `CREATE INDEX`) means the
restore is incomplete: stop and read `restore.log`.

## 4. Repair the AGE graph registry (REQUIRED, not done by pg_restore)

`pg_restore` brings back `ag_catalog.ag_graph.graphid` with the SOURCE server's
namespace oids, but the new server assigned different oids to the graph schemas.
Every Cypher query then fails with `graph with oid N does not exist`, while the
registry row count and every graph table row count look perfect. Re-point
`graphid` (and `ag_label.graph`) at the new namespace oids.

```bash
PSQL_S -d "$SCRATCH_DB" -c "select count(*) filter (where graphid<>namespace::oid) as stale from ag_catalog.ag_graph"   # expect 16 for lumina's server
FKDEF=$(PSQL_S -d "$SCRATCH_DB" -c "select pg_get_constraintdef(oid) from pg_constraint where conname='fk_graph_oid'")
PSQL_S -d "$SCRATCH_DB" <<SQL
BEGIN;
CREATE TEMP TABLE gmap AS SELECT graphid AS old, namespace::oid AS new FROM ag_catalog.ag_graph;
ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT fk_graph_oid;
UPDATE ag_catalog.ag_label l SET graph = m.new FROM gmap m WHERE l.graph = m.old;
UPDATE ag_catalog.ag_graph g SET graphid = m.new FROM gmap m WHERE g.graphid = m.old;
ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT fk_graph_oid $FKDEF;
COMMIT;
SQL
PSQL_S -d "$SCRATCH_DB" -c "select count(*) filter (where graphid<>namespace::oid) as stale from ag_catalog.ag_graph"    # expect 0
PSQL_S -d "$SCRATCH_DB" -c "LOAD 'age'; SET search_path=ag_catalog,public; select * from cypher('${AGENT}_knowledge', \$\$ MATCH (m:Memory) RETURN count(m) \$\$) as (c agtype)" | tail -1
```

The Cypher count must be non-zero and equal to the `Memory` label table count
(61645 for lumina on 2026-11-06).

## 5. Point skmemory at the scratch copy and run health

Everything here uses the scratch home and the scratch database. The DSN is derived
into a mode 600 file and never printed.

```bash
"$SK_PY" -I - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
u = urlsplit(open(os.environ["SCRATCH_DSN_FILE"]).read().strip())
assert u.hostname in ("127.0.0.1", "localhost") and u.port != 5432, "scratch DSN must be local and not :5432"
out = os.path.join(os.environ["DRILL"], "scratch.dsn")
open(out, "w").write(urlunsplit(u._replace(path="/" + os.environ["SCRATCH_DB"])))
os.chmod(out, 0o600)
PY
# SKMEMORY_HOME is the AGENTS BASE for profile lookup but ALSO the root that the
# flat store appends "memory/" to, so the base needs a "memory" link to the agent's.
ln -s "$AGENT/memory" "$DRILL/home/agents/memory"
cat > "$DRILL/env.sh" <<ENVSH
export SKMEMORY_HOME=$DRILL/home/agents
export SKCAPSTONE_HOME=$DRILL/home
export SKMEMORY_PG_DSN="\$(cat $DRILL/scratch.dsn)"
export SKMEMORY_EMBED_URL=http://127.0.0.1:1/api/embed
export SKMEMORY_EMBED_URLS=http://127.0.0.1:1/api/embed
ENVSH
. "$DRILL/env.sh"
env | grep -E '^(SKAGENT|SKCAPSTONE_AGENT|SKMEMORY_AGENT|SK_DEFAULT_AGENT)=' | sed 's/=.*/=<set>/'   # selectors must agree
START=$(date +%s)
"$SKMEM" --no-vector reindex | tail -1                 # rebuild the derived SQLite index, no pg writes
echo "reindex seconds: $(( $(date +%s) - START ))"
"$SKMEM" health 2>&1 | sed -E 's#postgres(ql)?://[^ "]*#<dsn>#g' > "$DRILL/health.out"
"$SK_PY" -I - <<'PY'
import json, os
t = open(os.path.join(os.environ["DRILL"], "health.out")).read()
d = json.loads(t[t.index("{"):])
print({k: v for k, v in d.items() if not isinstance(v, (dict, list))})
print("sqlite", d["primary"]["total_memories"], d["primary"]["by_layer"], d["primary"]["sync"]["in_sync"])
print("pg", d["pg"], "graph nodes", d["graph"].get("node_count"), "graph_ok", d["graph_ok"])
PY
```

Expected: `in_sync` true, `sqlite_only`, `flat_only`, `pg_only`, `unloadable` all 0,
`graph_ok` true. `status` reads `degraded` and `embed_ok`/`vector_ok` false ONLY
because this step deliberately points the embed URL at a dead port so the drill
cannot call a real embedder. That is the expected result of the drill. On a real
rebuilt node, drop those two exports and expect `green`.

## 6. Counts: restored vs source

```bash
PSQL_S -d "$SCRATCH_DB" -c "
select 'memories_total', count(*) from memories
union all select 'docs', count(*) from docs
union all select 'file_locations', count(*) from file_locations
union all select 'graphs', count(*) from ag_catalog.ag_graph
union all select 'indexes', count(*) from pg_indexes where schemaname not in ('pg_catalog','information_schema')
union all select 'hybrid_fns', count(*) from pg_proc where proname like 'hybrid_search%'
union all select 'memories_with_tsv', count(tsv) from memories
union all select 'memories_with_embedding', count(embedding) from memories
union all select 'restored_agent_rows', count(*) from memories where agent='$AGENT'"
docker exec -i "$PG_SRC_CONTAINER" psql -X -U postgres -d "$PG_SRC_DB" -At -c "
select 'src_memories_total', count(*) from memories
union all select 'src_agent_rows', count(*) from memories where agent='$AGENT'
union all select 'src_docs', count(*) from docs
union all select 'src_graphs', count(*) from ag_catalog.ag_graph"
```

`agent_rows`, `docs`, `graphs`, `indexes` and `file_locations` must match the
source. `memories_total` may be a few rows lower: the live store keeps writing
after the dump (the 2026-11-06 drill saw one row of drift).

## 7. 20-query spot check (ids and counts only, no content)

Q1 to Q10 pick 10 reproducible memory ids from the restored database and require a
row, an embedding and a matching flat file. Q11 to Q20 run 10 keyword queries through
the restored `hybrid_search_memories` (BM25 branch, no embed endpoint needed).

```bash
PSQL_S -d "$SCRATCH_DB" -c "select setseed(0.42); select id from memories where agent='$AGENT' order by random() limit 10" | tail -10 > "$DRILL/ids.txt"
mapfile -t IDS < "$DRILL/ids.txt"     # array, not a pipe: psql via docker exec -i would eat a while-read's stdin
n=0; pass=0
for id in "${IDS[@]}"; do n=$((n+1))
  row=$(PSQL_S -d "$SCRATCH_DB" -c "select (embedding is not null), (tsv is not null) from memories where id='$id'")
  flat=$(find "$DRILL/home/agents/$AGENT/memory" -name "$id*" -not -path '*/tombstones/*' | wc -l)
  ok=FAIL; if [ "$row" = "t|t" ] && [ "$flat" -ge 1 ]; then ok=ok; pass=$((pass+1)); fi
  echo "Q$n id=$id pg_row_emb_tsv=$row flat_files=$flat $ok"
done
for w in restore backup docker postgres skmemory gateway telegram nextcloud embedding systemd; do n=$((n+1))
  c=$(PSQL_S -d "$SCRATCH_DB" -c "select count(*) from hybrid_search_memories('$w', NULL, 10, '$AGENT')")
  ok=FAIL; if [ "$c" -ge 1 ]; then ok=ok; pass=$((pass+1)); fi
  echo "Q$n term=$w hits=$c $ok"
done
echo "spot check: $pass of $n passed"
[ "$pass" = "$n" ] || echo "SPOT CHECK FAILED"
```

Terms are generic infrastructure words so the output is safe to paste. When the
source is still reachable you can also compare per-id content hashes on both sides
with `md5(title||content||memory_json::text)`; in the 2026-11-06 drill 10 of 10
matched. Top-10 BM25 sets matched exactly for 5 of 10 terms and overlapped 8 to 9
of 10 for the other 5; every id that differed existed in the restore, so the
difference is rank ordering at the cut (live writes since the dump, rebuilt BM25
statistics), not missing data.

## 8. Cleanup (exact path, exact database)

```bash
PSQL_S -d postgres -c "DROP DATABASE $SCRATCH_DB"
docker exec -u root "$PG_SCRATCH_CONTAINER" rm -f "/tmp/$SCRATCH_DB.dump"   # docker cp files are root-owned
if [ "$DRILL" = "$HOME/.local/state/skmem-overhaul/scratch/drill-rb" ]; then rm -rf "$DRILL"; fi
PSQL_S -d postgres -c "select datname from pg_database order by 1"     # $SCRATCH_DB gone
ls "$DRILL" 2>&1 | head -1                                             # No such file
df -h / | tail -1
```

Never run `DROP DATABASE` against `$PG_SRC_DB` or `rm -rf` on anything but the
`$DRILL` path above. The guard on the `rm` line refuses any other path: if you
used a different `$DRILL`, delete that exact directory by hand.

## What does not restore by itself

| Item | State after `pg_restore` | Action |
| --- | --- | --- |
| AGE graphs | Registry and all graph rows restored, but every Cypher query fails (stale oids) | Step 4, required |
| HNSW index on `docs` | Fails in a default-`shm` container (parallel build) | Step 3 disables parallel workers; else rebuild by hand |
| SQLite `index.db` | Not in the dump, derived | `skmemory --no-vector reindex` (about 4 min for 44k files) |
| Tombstones, `memory/archive/` | Not in the nightly tarball nor the dump | Copy `tombstones/` explicitly (step 1). `archive/` (cold tier) is a separate copy |
| FTS | The `tsv` column and the three `*_bm25` indexes restore intact | None |
| Embeddings | Stored in the dump, restored intact | Embed endpoint is needed only for new writes and vector queries |
| Docs corpus | `docs` rows restored; `file_locations` rows restored but the source files they point at are out of scope | Re-ingest from source if paths moved |

## Real disaster (not a drill)

Same steps with these changes: the target is a fresh server on the rebuilt node
(`scripts/skmem-pg-restore.sh` builds an ephemeral container; for production use
the production container name and port and an empty `skmemory` database), flat tiers
go back under `~/.skcapstone/agents/<agent>/memory/` from the Syncthing copy or the
nightly tarball PLUS the tombstones, step 4 is mandatory, step 5 uses the node's
real embed URLs, and the health result should be `green`. This production path was
NOT exercised in the drill; only the scratch path above was.
