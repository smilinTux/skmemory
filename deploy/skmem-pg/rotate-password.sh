#!/usr/bin/env bash
# rotate-password.sh - rotate the skmem-pg postgres superuser password and,
# optionally, recreate the container bound to loopback only.
#
# Rolled into source from a live rotation (2026-11-07). Procedure, lessons and
# rollback are in LOCAL-PG-STACK-ARCHITECTURE.md ("Rotating the skmem-pg
# password"). Run it as the owner of the node (the user whose environment
# holds SKMEMORY_PG_DSN), never as root.
#
# Steps implemented:
#   1  backups (env file, docker inspect, pg_dump -Fc, pg_dumpall --globals-only), mode 600
#   2  new password generated to a 600 file; ALTER USER sent over stdin, never argv
#   3  only the ':<password>@' slot of SKMEMORY_PG_DSN is replaced (exact match, then asserted gone)
#   4  with --bind-loopback: stop, rename old, set its restart policy to no, docker run with
#      the same volume, shm, command and 127.0.0.1:PORT:5432 (secrets via a temp --env-file)
#   5  verify: pg_isready, loopback-only port, extensions, row count, new login ok, old refused
#   6  push the new DSN into the systemd user manager and the dbus activation environment
#   7  list consumers still holding the old value; restart SK services only with --restart-units
#   8  --remove-old NAME removes the stopped old container after your soak
#
# Usage:
#   rotate-password.sh [--dry-run] [--bind-loopback] [--env-file PATH] [--container NAME]
#                      [--port N] [--user NAME] [--db NAME] [--backup-dir DIR]
#                      [--restart-units] [--propagate [--canonical-env-file PATH]]
#   rotate-password.sh --remove-old NAME     (step 8, after the soak)
#
# Defaults: env file ~/.config/skmemory/pg.env, container skmem-pg, port 5432, role
# postgres, db skmemory, backups under ~/.local/state/skmem-pg-rotate/<UTC stamp>.
# Without --bind-loopback the container is NOT recreated (password change only).
# Without --restart-units nothing is restarted (candidates are only printed).
# Step 6 is OPT-IN (--propagate): it writes the DSN into the live systemd user manager and the
# dbus activation environment, so it refuses unless --env-file resolves to the canonical env
# file (default ~/.config/skmemory/pg.env, or the path given by --canonical-env-file).
# --no-propagate is accepted and does nothing (propagation is already off by default).
# Never run under bash -x: the script refuses.
# This script never prints a password or a DSN.

# Never run under xtrace: it would print the passwords. Must stay the first statement.
case "$-" in *x*) set +x; echo "[rotate] ERROR: refusing to run under xtrace (bash -x / set -x), it would print secrets" >&2; exit 2 ;; esac
if [ -n "${BASH_XTRACEFD:-}" ]; then echo "[rotate] ERROR: refusing to run with BASH_XTRACEFD set" >&2; exit 2; fi
set -euo pipefail
umask 077
shopt -u patsub_replacement 2>/dev/null || true

ENV_FILE="${HOME}/.config/skmemory/pg.env"
CONTAINER="skmem-pg"
PORT="5432"
PGUSER_NAME="postgres"
PGDB_NAME="skmemory"
BACKUP_DIR=""
DRY_RUN=0
BIND_LOOPBACK=0
RESTART_UNITS=0
PROPAGATE=0
CANONICAL_ENV="${HOME}/.config/skmemory/pg.env"
REMOVE_OLD=""

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
log() { printf '[rotate] %s\n' "$*"; }
die() { printf '[rotate] ERROR: %s\n' "$*" >&2; exit 1; }
need_val() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "option $1 needs a value"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --bind-loopback) BIND_LOOPBACK=1 ;;
    --restart-units) RESTART_UNITS=1 ;;
    --propagate) PROPAGATE=1 ;;
    --no-propagate) PROPAGATE=0 ;;   # no-op alias: propagation is off unless --propagate
    --canonical-env-file) need_val "$@"; CANONICAL_ENV="$2"; shift ;;
    --env-file) need_val "$@"; ENV_FILE="$2"; shift ;;
    --container) need_val "$@"; CONTAINER="$2"; shift ;;
    --port) need_val "$@"; PORT="$2"; shift ;;
    --user) need_val "$@"; PGUSER_NAME="$2"; shift ;;
    --db) need_val "$@"; PGDB_NAME="$2"; shift ;;
    --backup-dir) need_val "$@"; BACKUP_DIR="$2"; shift ;;
    --remove-old) need_val "$@"; REMOVE_OLD="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

case "$PORT" in ''|*[!0-9]*) die "--port must be numeric" ;; esac
case "$PGUSER_NAME$PGDB_NAME" in *[!A-Za-z0-9_]*) die "--user and --db must be [A-Za-z0-9_]" ;; esac
case "$CONTAINER" in *[!A-Za-z0-9_.-]*|'') die "invalid container name" ;; esac

command -v docker >/dev/null || die "docker not found"

# ---- step 8: remove the old container after the soak -------------------------------------
if [ -n "$REMOVE_OLD" ]; then
  case "$REMOVE_OLD" in *-old-*) ;; *) die "refusing: '$REMOVE_OLD' does not look like a renamed old container (*-old-*)" ;; esac
  running="$(docker inspect --format '{{.State.Running}}' "$REMOVE_OLD" 2>/dev/null)" \
    || die "no such container: $REMOVE_OLD"
  [ "$running" = "false" ] || die "$REMOVE_OLD is running; refusing to remove it"
  if [ "$DRY_RUN" = 1 ]; then log "dry-run: would docker rm $REMOVE_OLD"; exit 0; fi
  docker rm "$REMOVE_OLD" >/dev/null
  log "removed $REMOVE_OLD"
  exit 0
fi

[ "$(id -u)" -ne 0 ] || die "run as the node owner, not root"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
ENV_FILE="$(readlink -f "$ENV_FILE")"
if [ "$PROPAGATE" = 1 ] && [ "$ENV_FILE" != "$(readlink -f "$CANONICAL_ENV" 2>/dev/null || true)" ]; then
  die "--propagate refused: --env-file is not the canonical env file ($CANONICAL_ENV). Name it with --canonical-env-file if this node really uses another path"
fi

# ---- parse the DSN line (secret stays in shell variables only) ---------------------------
dsn_line=""
while IFS= read -r l || [ -n "$l" ]; do
  case "$l" in
    SKMEMORY_PG_DSN=*|"export SKMEMORY_PG_DSN="*) dsn_line="$l" ;;
  esac
done < "$ENV_FILE"
[ -n "$dsn_line" ] || die "no SKMEMORY_PG_DSN line in $ENV_FILE"
dsn="${dsn_line#*SKMEMORY_PG_DSN=}"
dsn="${dsn%\"}"; dsn="${dsn#\"}"; dsn="${dsn%\'}"; dsn="${dsn#\'}"
case "$dsn" in *://*:*@*) ;; *) die "SKMEMORY_PG_DSN has no ':<password>@' slot" ;; esac
rest="${dsn#*://}"
userinfo="${rest%%@*}"
case "$userinfo" in *:*) ;; *) die "SKMEMORY_PG_DSN has no password" ;; esac
OLD_PW="${userinfo#*:}"
[ -n "$OLD_PW" ] || die "SKMEMORY_PG_DSN password is empty"
unset rest userinfo dsn dsn_line

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OLD_NAME="${CONTAINER}-old-${STAMP}"
[ -n "$BACKUP_DIR" ] || BACKUP_DIR="${HOME}/.local/state/skmem-pg-rotate/${STAMP}"

docker inspect "$CONTAINER" >/dev/null 2>&1 || die "no such container: $CONTAINER"

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: nothing will be changed"
  log "  env file      : $ENV_FILE (DSN password slot found, value not shown)"
  log "  container     : $CONTAINER   role=$PGUSER_NAME db=$PGDB_NAME"
  log "  backups to    : $BACKUP_DIR (dir 700, files 600)"
  log "  1 backup      : env file copy, docker inspect, pg_dump -Fc, pg_dumpall --globals-only"
  log "  2 new password: generated to $BACKUP_DIR/new-password; ALTER USER over stdin"
  log "  3 env file    : replace only the ':<password>@' slot of SKMEMORY_PG_DSN, assert old gone"
  if [ "$BIND_LOOPBACK" = 1 ]; then
    log "  4 recreate    : stop, rename to $OLD_NAME, restart=no on it, docker run -p 127.0.0.1:${PORT}:5432"
  else
    log "  4 recreate    : skipped (no --bind-loopback)"
  fi
  log "  5 verify      : pg_isready, docker port, extensions, row count, new login, old refused"
  if [ "$PROPAGATE" = 1 ]; then log "  6 environment : systemctl --user import-environment + dbus-update-activation-environment"
  else log "  6 environment : skipped (pass --propagate to push the DSN to the user manager)"; fi
  if [ "$RESTART_UNITS" = 1 ]; then log "  7 consumers   : candidate SK units WOULD be restarted"
  else log "  7 consumers   : candidate units printed only (no --restart-units)"; fi
  log "  8 cleanup     : later, rotate-password.sh --remove-old $OLD_NAME"
  exit 0
fi

# ---- helpers -----------------------------------------------------------------------------
psql_in() { docker exec -i "$CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$PGUSER_NAME" -d "$PGDB_NAME" "$@"; }
psql_val() { printf '%s\n' "$1" | psql_in -At; }
wait_ready() {
  local i
  for i in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -q -U "$PGUSER_NAME" -d "$PGDB_NAME" && return 0
    sleep 1
  done
  return 1
}
tcp_login() { # $1 = password; connects over the non-loopback address so scram is enforced
  PGPASSWORD="$1" docker exec -e PGPASSWORD -i "$CONTAINER" sh -c \
    'psql -X -At -h "$(hostname -i | cut -d" " -f1)" -U "$1" -d "$2" -c "select 1"' _ \
    "$PGUSER_NAME" "$PGDB_NAME" >/dev/null 2>&1
}

mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
NEW_PW_FILE="$BACKUP_DIR/new-password"
TMP_ENVFILE=""
cleanup() { [ -z "$TMP_ENVFILE" ] || rm -f "$TMP_ENVFILE"; }
trap cleanup EXIT
trap 'printf "[rotate] FAILED at line %s. Backups: %s. If the ALTER ran, the new password is in %s\n" "$LINENO" "$BACKUP_DIR" "$NEW_PW_FILE" >&2' ERR

# ---- 1 backups ---------------------------------------------------------------------------
log "1/8 backups to $BACKUP_DIR"
cp -p "$ENV_FILE" "$BACKUP_DIR/pg.env.bak"; chmod 600 "$BACKUP_DIR/pg.env.bak"
docker inspect "$CONTAINER" > "$BACKUP_DIR/docker-inspect.json"
docker exec "$CONTAINER" pg_dump -U "$PGUSER_NAME" -Fc "$PGDB_NAME" > "$BACKUP_DIR/${PGDB_NAME}.dump"
docker exec "$CONTAINER" pg_dumpall -U "$PGUSER_NAME" --globals-only > "$BACKUP_DIR/globals.sql"
chmod 600 "$BACKUP_DIR"/*
[ -s "$BACKUP_DIR/${PGDB_NAME}.dump" ] || die "pg_dump produced an empty file"

# ---- 2 new password + ALTER over stdin ---------------------------------------------------
log "2/8 generating new password to $NEW_PW_FILE and applying it"
if command -v openssl >/dev/null; then NEW_PW="$(openssl rand -hex 24)"
else NEW_PW="$(head -c 256 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-48)"; fi
[ "${#NEW_PW}" -ge 32 ] || die "password generation failed"
( umask 077; printf '%s\n' "$NEW_PW" > "$NEW_PW_FILE" ); chmod 600 "$NEW_PW_FILE"
# log_statement=none for this session so ALTER ... PASSWORD does not reach the server log
printf "SET log_statement = 'none';\nALTER USER %s PASSWORD '%s';\n" "$PGUSER_NAME" "$NEW_PW" | psql_in >/dev/null

# ---- 3 rewrite only the password slot of the DSN -----------------------------------------
log "3/8 updating the DSN password slot in $ENV_FILE"
TMP_ENVFILE="$(mktemp "$(dirname "$ENV_FILE")/.pg.env.XXXXXX")"
found=0
while IFS= read -r l || [ -n "$l" ]; do
  case "$l" in
    *SKMEMORY_PG_DSN=*":${OLD_PW}@"*) l="${l//":${OLD_PW}@"/":${NEW_PW}@"}"; found=1 ;;
  esac
  printf '%s\n' "$l"
done < "$ENV_FILE" > "$TMP_ENVFILE"
[ "$found" = 1 ] || die "old password slot not found by exact match; env file untouched"
content="$(cat "$TMP_ENVFILE")"
case "$content" in *":${OLD_PW}@"*) die "old password still present after replace; env file untouched" ;; esac
case "$content" in *":${NEW_PW}@"*) ;; *) die "new password not present after replace; env file untouched" ;; esac
unset content
chmod 600 "$TMP_ENVFILE"
mv -f "$TMP_ENVFILE" "$ENV_FILE"; TMP_ENVFILE=""

# ---- 4 recreate bound to loopback --------------------------------------------------------
if [ "$BIND_LOOPBACK" = 1 ]; then
  log "4/8 recreating $CONTAINER bound to 127.0.0.1:${PORT}"
  image_id="$(docker inspect --format '{{.Image}}' "$CONTAINER")"
  image_ref="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")"
  restart_pol="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")"
  [ -n "$restart_pol" ] || restart_pol="unless-stopped"
  shm="$(docker inspect --format '{{.HostConfig.ShmSize}}' "$CONTAINER")"
  run_args=()
  while IFS='|' read -r mtype mname msrc mdst mrw; do
    [ -n "$mdst" ] || continue
    src="$msrc"; [ "$mtype" = volume ] && src="$mname"
    spec="$src:$mdst"; [ "$mrw" = "false" ] && spec="$spec:ro"
    run_args+=(-v "$spec")
  done < <(docker inspect --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}|{{.RW}}{{"\n"}}{{end}}' "$CONTAINER")
  [ "${#run_args[@]}" -gt 0 ] || die "container has no mounts; refusing to recreate (data would be lost)"
  case "$shm" in ''|0|*[!0-9]*) ;; *) run_args+=(--shm-size "$shm") ;; esac
  cmd_args=()
  while IFS= read -r a; do [ -n "$a" ] && cmd_args+=("$a"); done \
    < <(docker inspect --format '{{range .Config.Cmd}}{{println .}}{{end}}' "$CONTAINER")
  [ "${#cmd_args[@]}" -gt 0 ] || cmd_args=(postgres -c shared_preload_libraries=pg_search,age)

  # LESSON: the tag can be missing locally even though the old container runs. Re-tag from the image ID.
  if ! docker image inspect "$image_ref" >/dev/null 2>&1; then
    log "    image tag $image_ref missing locally; re-tagging from the old container's image ID"
    docker tag "$image_id" "$image_ref"
  fi

  # Preserve the old container's custom env (not image defaults) EXCEPT POSTGRES_PASSWORD: the data
  # directory is already initialised, so the entrypoint never reads it, and passing it would
  # persist the new secret in `docker inspect` Config.Env.
  declare -A img_env=() img_lbl=()
  while IFS= read -r e; do [ -n "$e" ] && img_env["$e"]=1; done \
    < <(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image_id")
  while IFS= read -r e; do [ -n "$e" ] && img_lbl["$e"]=1; done \
    < <(docker image inspect --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$image_id")
  tmp_env="$(mktemp "$BACKUP_DIR/run-env.XXXXXX")"
  TMP_ENVFILE="$tmp_env"
  : > "$tmp_env"
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    case "$e" in POSTGRES_PASSWORD=*) continue ;; esac
    [ -z "${img_env[$e]:-}" ] || continue
    printf '%s\n' "$e" >> "$tmp_env"
  done < <(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER")
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    [ -z "${img_lbl[$e]:-}" ] || continue
    run_args+=(--label "$e")
  done < <(docker inspect --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$CONTAINER")
  nets=()
  while IFS= read -r n; do
    [ -n "$n" ] && [ "$n" != bridge ] && nets+=("$n")
  done < <(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$CONTAINER")
  [ "${#nets[@]}" -eq 0 ] || run_args+=(--network "${nets[0]}")

  docker stop "$CONTAINER" >/dev/null
  docker rename "$CONTAINER" "$OLD_NAME"
  docker update --restart=no "$OLD_NAME" >/dev/null
  if ! docker run -d --name "$CONTAINER" --restart "$restart_pol" --env-file "$tmp_env" \
        -p "127.0.0.1:${PORT}:5432" "${run_args[@]}" "$image_ref" "${cmd_args[@]}" >/dev/null; then
    log "docker run failed; rolling back to the old container"
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker rename "$OLD_NAME" "$CONTAINER"
    docker update --restart="$restart_pol" "$CONTAINER" >/dev/null || true
    docker start "$CONTAINER" >/dev/null || true
    die "recreate failed; old container restored and started (password already changed, see $NEW_PW_FILE)"
  fi
  for n in "${nets[@]:1}"; do docker network connect "$n" "$CONTAINER" >/dev/null 2>&1 || log "    could not join network $n"; done
  rm -f "$tmp_env"; TMP_ENVFILE=""
  log "    old container kept stopped as $OLD_NAME for the soak"
else
  log "4/8 recreate skipped (no --bind-loopback)"
fi

# ---- 5 verify ----------------------------------------------------------------------------
log "5/8 verifying"
wait_ready || die "pg_isready never succeeded"
ports="$(docker port "$CONTAINER" 2>/dev/null || true)"
bad=0
while IFS= read -r p; do
  [ -n "$p" ] || continue
  case "$p" in *"-> 127.0.0.1:"*|*"-> [::1]:"*) ;; *) bad=1 ;; esac
done <<< "$ports"
if [ "$bad" = 1 ]; then
  if [ "$BIND_LOOPBACK" = 1 ]; then die "container publishes a non-loopback port"
  else log "    WARNING: a port is published beyond loopback; rerun with --bind-loopback"; fi
else log "    published ports are loopback only"; fi
exts="$(psql_val "select string_agg(extname, ',' order by extname) from pg_extension where extname in ('vector','pg_search','age');")"
[ "$exts" = "age,pg_search,vector" ] || die "extensions missing (got: ${exts:-none})"
log "    extensions: $exts"
rows="n/a"
if [ "$(psql_val "select to_regclass('public.memories') is not null;" 2>/dev/null || true)" = "t" ]; then
  rows="$(psql_val "select count(*) from public.memories;" 2>/dev/null || true)"; [ -n "$rows" ] || rows="n/a"
fi
log "    memories rows: $rows"
tcp_login "$NEW_PW" || die "new password does not log in"
log "    new password logs in"
if tcp_login "$OLD_PW"; then die "OLD password is still accepted"; fi
log "    old password refused"

# ---- 6 propagate to the systemd user manager ---------------------------------------------
# LESSON: 'systemctl --user daemon-reload' does NOT re-read environment.d. The manager keeps the
# environment it started with until it is told otherwise.
if [ "$PROPAGATE" = 0 ]; then
  log "6/8 skipped (no --propagate); push the DSN yourself, see the runbook"
else
log "6/8 pushing SKMEMORY_PG_DSN into the systemd user manager"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
if [ -z "${SKMEMORY_PG_DSN:-}" ]; then
  log "    could not load SKMEMORY_PG_DSN from $ENV_FILE"
else
  command -v systemctl >/dev/null && systemctl --user import-environment SKMEMORY_PG_DSN \
    || log "    systemctl --user import-environment unavailable or failed"
  command -v dbus-update-activation-environment >/dev/null \
    && dbus-update-activation-environment --systemd SKMEMORY_PG_DSN \
    || log "    dbus-update-activation-environment unavailable or failed"
fi
fi

# ---- 7 consumers still on the old value --------------------------------------------------
log "7/8 consumers still holding the old DSN"
units=(); scopes=()
for d in /proc/[0-9]*; do
  [ -r "$d/environ" ] && [ -O "$d" ] || continue
  hit=0
  while IFS= read -r -d '' kv; do
    case "$kv" in SKMEMORY_PG_DSN=*":${OLD_PW}@"*) hit=1; break ;; esac
  done < "$d/environ" 2>/dev/null || true
  [ "$hit" = 1 ] || continue
  u=""
  while IFS= read -r cg; do
    last="${cg##*/}"
    case "$last" in *.service) u="$last" ;; *.scope) [ -n "$u" ] || u="$last" ;; esac
  done < "$d/cgroup" 2>/dev/null || true
  case "$u" in
    *.service) units+=("$u") ;;
    *) scopes+=("pid ${d#/proc/} ${u:-unknown}") ;;
  esac
done
mapfile -t units < <(printf '%s\n' "${units[@]:-}" | sed '/^$/d' | sort -u)
sk_units=(); other_units=()
for u in "${units[@]:-}"; do
  [ -n "$u" ] || continue
  case "$u" in
    sk*) sk_units+=("$u") ;;
    *) other_units+=("$u") ;;
  esac
done
log "    SK service candidates: ${#sk_units[@]}"
for u in "${sk_units[@]:-}"; do [ -n "$u" ] && log "      $u"; done
log "    not restarted (desktop or non-SK services): ${#other_units[@]}"
for u in "${other_units[@]:-}"; do [ -n "$u" ] && log "      $u"; done
log "    interactive sessions and scopes (owner must restart): ${#scopes[@]}"
for s in "${scopes[@]:-}"; do [ -n "$s" ] && log "      $s"; done
if [ "$RESTART_UNITS" = 1 ]; then
  for u in "${sk_units[@]:-}"; do
    [ -n "$u" ] || continue
    systemctl --user restart "$u" && log "    restarted $u" || log "    FAILED to restart $u"
  done
else
  log "    pass --restart-units to restart the SK service candidates"
fi

# ---- 8 next ------------------------------------------------------------------------------
log "8/8 done. Move $NEW_PW_FILE into your secret store, then delete it."
[ "$BIND_LOOPBACK" = 1 ] && log "    after the soak: $0 --remove-old $OLD_NAME"
exit 0
