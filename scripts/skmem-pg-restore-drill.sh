#!/usr/bin/env bash
# skmem-pg-restore-drill.sh - safe, self-contained restore drill.
#
# Purpose: exercise the FULL backup -> restore -> verify path WITHOUT ever
# reading the live skmem-pg or the live backups. It stands up a THROWAWAY source
# container, loads the vendored schema, seeds synthetic memories + a small AGE
# graph, dumps it exactly like deploy/ops/skmem-pg-backup.sh (pg_dump -Fc), then
# hands that dump to scripts/skmem-pg-restore.sh and tears everything down.
#
# Operators: to drill against a REAL dump instead of a synthetic one, skip this
# harness and run the restore directly:
#     scripts/skmem-pg-restore.sh /path/to/real.dump
# (that path is read-only and lands in a fresh ephemeral container; the live
# skmem-pg is never touched).
#
# Env:
#   SKMEM_PG_IMAGE   image to use (default: skmem-pg:pg17-bm25-age)
#   SKMEM_DRILL_KEEP "1" to keep the synthetic dump on disk after the run
set -euo pipefail

IMAGE="${SKMEM_PG_IMAGE:-skmem-pg:pg17-bm25-age}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCHEMA="$REPO_ROOT/deploy/skmem-pg/schema.sql"

SRC="skmem-pg-drill-src"
DUMP_DIR="$(mktemp -d)"
DUMP="$DUMP_DIR/skmem-pg-skmemory-drill.dump"

log() { echo "$(date -u +%FT%TZ) [drill] $*"; }
die() { echo "$(date -u +%FT%TZ) [drill] ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image '$IMAGE' not present; build: docker build -t $IMAGE $REPO_ROOT/deploy/skmem-pg"
[ -r "$SCHEMA" ] || die "schema not found: $SCHEMA"

cleanup() {
  docker rm -fv "$SRC" >/dev/null 2>&1 || true
  if [ "${SKMEM_DRILL_KEEP:-0}" = "1" ]; then
    log "kept synthetic dump: $DUMP"
  else
    rm -rf "$DUMP_DIR" || true
  fi
}
trap cleanup EXIT

spx() { docker exec "$SRC" psql -X -U postgres -d skmemory -v ON_ERROR_STOP=0 "$@"; }

# --- 1. Throwaway SOURCE container (never the live 'skmem-pg', never port 5432) ---
docker rm -fv "$SRC" >/dev/null 2>&1 || true
log "starting synthetic source container '$SRC'"
docker run -d --name "$SRC" \
  -e POSTGRES_DB=skmemory \
  -e POSTGRES_PASSWORD="drill-src-$$" \
  "$IMAGE" postgres -c shared_preload_libraries=pg_search,age >/dev/null

ready=0
for _ in $(seq 1 120); do
  docker exec "$SRC" pg_isready -U postgres -d skmemory >/dev/null 2>&1 && { ready=1; break; }
  sleep 1
done
[ "$ready" = "1" ] || die "source postgres did not become ready"

# --- 2. Load the vendored schema (extensions, tables, hybrid functions, indexes) ---
# schema.sql references the paradedb / ag_catalog schemas, so the extensions that
# own them must exist first (schema.sql itself creates them mid-file but ordering
# is not guaranteed on a fresh initdb). Pre-create them, exactly like the restore
# path does, then load the DDL.
log "loading vendored schema.sql into source"
spx -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_search; CREATE EXTENSION IF NOT EXISTS age;" >/dev/null 2>&1 || true
docker cp "$SCHEMA" "$SRC:/tmp/schema.sql"
spx -f /tmp/schema.sql >/dev/null 2>&1 || true

# --- 3. Seed synthetic memories + a small AGE graph ---
log "seeding synthetic memories + AGE graph"
spx -c "
INSERT INTO memories (id, agent, layer, title, content, memory_json, created_at)
SELECT 'drill-'||g, 'drilltest', 'short-term',
       'drill memory '||g,
       'This synthetic drill memory number '||g||' mentions skmemory restore recovery ceremony bulletproof.',
       jsonb_build_object('id','drill-'||g,'synthetic',true),
       now()
FROM generate_series(1,25) g
ON CONFLICT (id) DO NOTHING;
" 2>&1 | tail -3
mrows=$(docker exec "$SRC" psql -X -U postgres -d skmemory -At -c "SELECT count(*) FROM memories WHERE agent='drilltest';" 2>/dev/null || echo 0)
[ "${mrows:-0}" -ge 1 ] || die "seed memories failed (0 rows inserted; schema mismatch?)"

# small AGE graph so the restore VERIFY has a registered graph to find. Seed via
# a copied SQL file: create_graph + cypher use $$ and "$user", which get mangled
# by shell quoting if passed inline with psql -c.
AGE_SEED="$DUMP_DIR/age-seed.sql"
cat > "$AGE_SEED" <<'SQL'
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('drilltest_knowledge');
SELECT * FROM cypher('drilltest_knowledge', $$
  CREATE (a:Concept {name:'restore'})-[:RELATES]->(b:Concept {name:'recovery'})
  RETURN a
$$) AS (v agtype);
SQL
docker cp "$AGE_SEED" "$SRC:/tmp/age-seed.sql" >/dev/null
spx -f /tmp/age-seed.sql >/dev/null 2>&1 || log "AGE seed note"
age_graphs=$(docker exec "$SRC" psql -X -U postgres -d skmemory -At -c "SELECT count(*) FROM ag_catalog.ag_graph;" 2>/dev/null || echo 0)
log "source AGE graphs registered: $age_graphs"

seed_rows=$(docker exec "$SRC" psql -X -U postgres -d skmemory -At -c "SELECT count(*) FROM memories WHERE agent='drilltest';" 2>/dev/null || echo "?")
log "seeded memories rows: $seed_rows"

# --- 4. Dump EXACTLY like skmem-pg-backup.sh (pg_dump -Fc, full schema+data) ---
log "dumping source (pg_dump -Fc) -> $DUMP"
docker exec "$SRC" pg_dump -U postgres -d skmemory -Fc > "$DUMP"
[ -s "$DUMP" ] || die "synthetic dump is empty"
log "dump size: $(du -h "$DUMP" | cut -f1)"

# --- 5. Hand the dump to the real restore script (fresh ephemeral target) ---
log "invoking scripts/skmem-pg-restore.sh against the synthetic dump"
SKMEM_RESTORE_CONTAINER=skmem-pg-drill-restore \
SKMEM_RESTORE_PORT=15477 \
SKMEM_RESTORE_DB=skmemory \
SKMEM_PG_IMAGE="$IMAGE" \
"$SCRIPT_DIR/skmem-pg-restore.sh" "$DUMP"

log "DRILL COMPLETE"
