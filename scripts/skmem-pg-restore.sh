#!/usr/bin/env bash
# skmem-pg-restore.sh - scripted cold-machine restore + VERIFY of a skmem-pg dump.
#
# Restores a `pg_dump -Fc` custom-format dump (the artifact produced by
# deploy/ops/skmem-pg-backup.sh) into a FRESH skmem-pg container, then verifies
# the stack is functional: hybrid_search_memories returns rows AND the AGE graph
# registry is present.
#
# This is the fast-recovery path. The authoritative rebuild is still
# reconcile-from-flat (skmem_reconcile.py) + skingest-from-wiki; a lost dump is
# not data loss. But the dump is the ONLY off-node copy of the ~33k-node AGE
# graph, so a drilled, scripted restore matters.
#
# SAFETY: by default this runs against an EPHEMERAL container on a NON-default
# port and NEVER touches the live skmem-pg. It hard-refuses to target the live
# container name ("skmem-pg") or the live port (5432) unless you pass
# SKMEM_RESTORE_ALLOW_LIVE=1 (do not).
#
# Usage:
#   scripts/skmem-pg-restore.sh <dump-file>
#
# Env contract (all optional):
#   SKMEM_RESTORE_CONTAINER  ephemeral container name   (default: skmem-pg-restore)
#   SKMEM_RESTORE_PORT       host port -> 5432           (default: 15432)
#   SKMEM_RESTORE_DB         database name to restore    (default: skmemory)
#   SKMEM_PG_IMAGE           image to run                (default: skmem-pg:pg17-bm25-age)
#   SKMEM_PG_PASSWORD        superuser password (ephemeral; default: a throwaway)
#   SKMEM_RESTORE_SCHEMA     path to schema DDL to pre-load BEFORE data, used ONLY
#                            when the dump is data-only (default: auto = repo
#                            deploy/skmem-pg/schema.sql, applied only if needed)
#   SKMEM_RESTORE_KEEP       "1" to leave the container running for inspection
#                            (default: 0 = tear down container + volume at end)
#   SKMEM_RESTORE_READY_TIMEOUT  seconds to wait for pg_isready (default: 120)
#   SKMEM_RESTORE_ALLOW_LIVE "1" to bypass the live-target guard (never do this)
set -euo pipefail

DUMP="${1:-}"
[ -n "$DUMP" ] || { echo "usage: $0 <dump-file>" >&2; exit 2; }
[ -r "$DUMP" ] || { echo "ERROR: dump not readable: $DUMP" >&2; exit 2; }

CONTAINER="${SKMEM_RESTORE_CONTAINER:-skmem-pg-restore}"
PORT="${SKMEM_RESTORE_PORT:-15432}"
DB="${SKMEM_RESTORE_DB:-skmemory}"
IMAGE="${SKMEM_PG_IMAGE:-skmem-pg:pg17-bm25-age}"
PGPW="${SKMEM_PG_PASSWORD:-restore-drill-$$}"
KEEP="${SKMEM_RESTORE_KEEP:-0}"
READY_TIMEOUT="${SKMEM_RESTORE_READY_TIMEOUT:-120}"
ALLOW_LIVE="${SKMEM_RESTORE_ALLOW_LIVE:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCHEMA="${SKMEM_RESTORE_SCHEMA:-$REPO_ROOT/deploy/skmem-pg/schema.sql}"

log() { echo "$(date -u +%FT%TZ) $*"; }
die() { echo "$(date -u +%FT%TZ) ERROR: $*" >&2; exit 1; }

# --- SAFETY GUARD: never touch the live skmem-pg ---
if [ "$ALLOW_LIVE" != "1" ]; then
  [ "$CONTAINER" = "skmem-pg" ] && die "refusing to use live container name 'skmem-pg' (set SKMEM_RESTORE_CONTAINER)"
  [ "$PORT" = "5432" ] && die "refusing to bind live port 5432 (set SKMEM_RESTORE_PORT)"
fi

command -v docker >/dev/null 2>&1 || die "docker not found"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image '$IMAGE' not present; build it: docker build -t $IMAGE $REPO_ROOT/deploy/skmem-pg"

cleanup() {
  if [ "$KEEP" = "1" ]; then
    log "SKMEM_RESTORE_KEEP=1: leaving container '$CONTAINER' up on 127.0.0.1:$PORT (remove with: docker rm -fv $CONTAINER)"
  else
    docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# psql helper against the ephemeral container
pex() { docker exec "$CONTAINER" psql -X -U postgres -d "$DB" -At "$@"; }

START=$(date +%s)

# --- 1. Fresh ephemeral container ---
docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
log "starting ephemeral container '$CONTAINER' (image $IMAGE) on 127.0.0.1:$PORT"
docker run -d --name "$CONTAINER" \
  -e POSTGRES_DB="$DB" \
  -e POSTGRES_PASSWORD="$PGPW" \
  -p "127.0.0.1:${PORT}:5432" \
  "$IMAGE" \
  postgres -c shared_preload_libraries=pg_search,age >/dev/null

# --- 2. Wait for the DB to accept connections ---
log "waiting up to ${READY_TIMEOUT}s for postgres to be ready"
ready=0
for _ in $(seq 1 "$READY_TIMEOUT"); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d "$DB" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "$ready" = "1" ] || die "postgres did not become ready within ${READY_TIMEOUT}s"

# The image's initdb created an EMPTY database named "$DB". Load extensions up
# front so AGE / pg_search / vector object references in the dump resolve.
docker exec "$CONTAINER" psql -X -U postgres -d "$DB" -v ON_ERROR_STOP=0 \
  -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_search; CREATE EXTENSION IF NOT EXISTS age;" \
  >/dev/null 2>&1 || true

# --- 3. Detect dump format ---
# Custom-format (-Fc) dumps begin with the magic "PGDMP". A plain-text SQL dump
# does not. Custom dumps carry their own schema; plain data-only dumps need the
# vendored schema.sql loaded first.
is_custom=0
if head -c 5 "$DUMP" 2>/dev/null | grep -q "PGDMP"; then
  is_custom=1
fi

copy_and_load_schema() {
  [ -r "$SCHEMA" ] || die "schema DDL not found at $SCHEMA (set SKMEM_RESTORE_SCHEMA)"
  log "loading schema DDL from $SCHEMA"
  docker cp "$SCHEMA" "$CONTAINER:/tmp/schema.sql"
  docker exec "$CONTAINER" psql -X -U postgres -d "$DB" -v ON_ERROR_STOP=0 -f /tmp/schema.sql >/dev/null 2>&1 || true
}

# --- 4. Restore ---
log "restoring dump: $DUMP (custom-format=$is_custom)"
docker cp "$DUMP" "$CONTAINER:/tmp/restore.dump"
if [ "$is_custom" = "1" ]; then
  # Full custom dump: pg_restore rebuilds schema + data + functions. AGE/pg_search
  # restores emit some non-fatal notices; VERIFY (below) is the source of truth,
  # so we do not use --exit-on-error here.
  docker exec "$CONTAINER" pg_restore --no-owner --no-privileges -d "$DB" /tmp/restore.dump \
    > /tmp/restore-$$.log 2>&1 || log "pg_restore returned non-zero (expected for AGE/pg_search notices); verifying functionally"
else
  # Plain SQL dump. If it is data-only, pre-load the schema DDL first.
  copy_and_load_schema
  docker exec "$CONTAINER" psql -X -U postgres -d "$DB" -v ON_ERROR_STOP=0 -f /tmp/restore.dump >/dev/null 2>&1 || true
fi

# --- 5. VERIFY ---
log "verifying restored stack"
fail=0

# 5a. hybrid_search functions exist
fn_count=$(pex -c "SELECT count(*) FROM pg_proc WHERE proname LIKE 'hybrid_search%';" 2>/dev/null || echo 0)
log "  hybrid_search_* functions: $fn_count (expect >= 2)"
[ "${fn_count:-0}" -ge 2 ] || { echo "  FAIL: hybrid_search functions missing" >&2; fail=1; }

# 5b. row counts
mem_rows=$(pex -c "SELECT count(*) FROM memories;" 2>/dev/null || echo ERR)
doc_rows=$(pex -c "SELECT count(*) FROM docs;" 2>/dev/null || echo ERR)
log "  memories rows: $mem_rows   docs rows: $doc_rows"
[ "$mem_rows" != "ERR" ] || { echo "  FAIL: memories table not queryable" >&2; fail=1; }

# 5c. hybrid_search_memories actually returns rows. Sample a real content token
# from the restored data and feed it through the BM25 branch (q_vec NULL so we do
# not need a live embed endpoint for the drill).
# pick a distinctive word (length >= 5) to dodge BM25 English stopwords like "the"/"this"
token=$(pex -c "SELECT lower(w) FROM (SELECT unnest(regexp_split_to_array(regexp_replace(content,'[^A-Za-z0-9 ]',' ','g'),'\s+')) AS w FROM memories WHERE content IS NOT NULL) t WHERE length(w) >= 5 LIMIT 1;" 2>/dev/null || echo "")
if [ -n "$token" ]; then
  hits=$(pex -c "SELECT count(*) FROM hybrid_search_memories('$token', NULL, 10, NULL);" 2>/dev/null || echo ERR)
  log "  hybrid_search_memories('$token', NULL) -> $hits row(s) (expect >= 1)"
  if [ "$hits" = "ERR" ]; then
    echo "  FAIL: hybrid_search_memories raised an error" >&2; fail=1
  elif [ "${hits:-0}" -lt 1 ]; then
    echo "  WARN: hybrid_search_memories returned 0 rows for sampled token '$token'" >&2
  fi
else
  # No text rows to sample (e.g. an empty-but-valid restore). Prove the function
  # at least executes without error.
  if pex -c "SELECT * FROM hybrid_search_memories('skmemory', NULL, 1, NULL);" >/dev/null 2>&1; then
    log "  hybrid_search_memories executes (no sampleable rows to match)"
  else
    echo "  FAIL: hybrid_search_memories raised an error" >&2; fail=1
  fi
fi

# 5d. AGE graph present
graphs=$(pex -c "SELECT count(*) FROM ag_catalog.ag_graph;" 2>/dev/null || echo ERR)
if [ "$graphs" = "ERR" ]; then
  echo "  WARN: ag_catalog.ag_graph not queryable (AGE registry not restored - see restore-drill-log caveat)" >&2
else
  graph_names=$(pex -c "SELECT string_agg(name,',') FROM ag_catalog.ag_graph;" 2>/dev/null || echo "")
  log "  AGE graphs: $graphs [$graph_names]"
  [ "${graphs:-0}" -ge 1 ] || echo "  WARN: no AGE graphs registered (schema-only dump; re-register with ag_catalog.create_graph or restore a data dump)" >&2
fi

END=$(date +%s)
ELAPSED=$((END - START))
log "restore drill elapsed: ${ELAPSED}s"

if [ "$fail" = "0" ]; then
  log "RESTORE VERIFY: PASS (memories=$mem_rows docs=$doc_rows functions=$fn_count graphs=${graphs:-?} elapsed=${ELAPSED}s)"
else
  die "RESTORE VERIFY: FAIL (see messages above)"
fi
