#!/usr/bin/env bash
# =====================================================================
# 00-run-init.sh  --  skmem-pg compose first-boot init (card OPS1.3)
# =====================================================================
# Runs ONCE, during the Postgres docker-entrypoint initdb phase (fresh data
# volume only), from /docker-entrypoint-initdb.d/. It applies, in order:
#
#   1. schema.sql            -- the live post-cutover base snapshot
#   2. every forward migration listed in migrations.txt, in order
#      (currently 03-ops-namespace.sql: the skbrain ops namespace)
#
# so a clean `docker compose up` yields a FULLY-MIGRATED instance: BM25 + AGE +
# hybrid_search_* (from schema.sql) AND the ops schema/graph/privacy-wall (from
# the forward migrations). This closes G2's "fresh compose omits the DDL" gap.
#
# The source files are mounted read-only at $SKMEM_INIT_SRC (see docker-compose.yml).
# Every step runs with ON_ERROR_STOP=1: any failure aborts initdb loudly rather
# than leaving a half-migrated DB. Each forward migration is idempotent, so a
# re-run (e.g. a manual replay) is a safe no-op.
#
# The historical, superseded migrations (02-enable-bm25-age.sql,
# 03-cutover-mxbai.sql) are intentionally NOT applied here: they are already
# baked into schema.sql and 03-cutover is not idempotent. migrations.txt is the
# single source of truth for what applies on top of the snapshot.
# =====================================================================
set -euo pipefail

SRC="${SKMEM_INIT_SRC:-/skmem-initdb-src}"
DB="${POSTGRES_DB:-skmemory}"
USER="${POSTGRES_USER:-postgres}"
MANIFEST="${SRC}/migrations.txt"

psql_apply() {
  # Apply a .sql file on the local init socket, fail-closed.
  local file="$1"
  echo "skmem-pg init: applying ${file}"
  psql -v ON_ERROR_STOP=1 --username "${USER}" --dbname "${DB}" -f "${file}"
}

psql_run() {
  # Run a (read-only) .sql file, echoing results; never fatal.
  local file="$1"
  echo "skmem-pg init: verify via ${file}"
  psql -v ON_ERROR_STOP=1 --username "${USER}" --dbname "${DB}" -f "${file}" || \
    echo "skmem-pg init: verify ${file} returned non-zero (non-fatal)"
}

echo "skmem-pg init: base snapshot schema.sql"
psql_apply "${SRC}/schema.sql"

if [[ ! -f "${MANIFEST}" ]]; then
  echo "skmem-pg init: no migrations.txt found at ${MANIFEST}; base schema only"
  exit 0
fi

# Parse migrations.txt: '<migration.sql> [| <verify.sql>]', skip blanks/comments.
while IFS= read -r raw || [[ -n "${raw}" ]]; do
  line="${raw%%#*}"                          # strip trailing comments
  line="$(echo "${line}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [[ -z "${line}" ]] && continue

  migration="${line%%|*}"
  verify=""
  if [[ "${line}" == *"|"* ]]; then
    verify="${line##*|}"
  fi
  migration="$(echo "${migration}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  verify="$(echo "${verify}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"

  psql_apply "${SRC}/${migration}"
  if [[ -n "${verify}" && -f "${SRC}/${verify}" ]]; then
    psql_run "${SRC}/${verify}"
  fi
done < "${MANIFEST}"

echo "skmem-pg init: complete (schema + all forward migrations applied)"
