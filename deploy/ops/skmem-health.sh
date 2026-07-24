#!/usr/bin/env bash
# skmem-health.sh — daily skmemory stack health probe. Emits a deterministic
# digest to STDOUT (for Hermes to inject into the agent's synthesis prompt) and
# archives a dated copy. Checks: flat-file writes, SQLite index, skmem-pg,
# backups (tarball + daily rotation), skwhisper session ingestion, and functional
# retrieval (SQLite + pg vector/hybrid).
#
# Each check prints one line: [PASS]/[WARN]/[FAIL] <check> — <detail>
# The final SUMMARY line carries the overall verdict (worst of all checks).
#
# All checks are DETERMINISTIC — no LLM decides "healthy". Endpoints are pinned
# to verified-live values (skmem-pg is docker :5432 user=postgres, NOT the stale
# SKMEMORY_PG_DSN=:5433 env var).
#
# Vendored into the repo (coord ce559215) so the production monitor is versioned
# and a node can be rebuilt from source. Previously only lived at
# ~/.hermes/scripts/skmem-health.sh on .158. Host-specifics are env-parametrized;
# defaults preserve the original .158 behavior so it runs unchanged there.
#
# Env contract (all optional):
#   SKAGENT              agent to probe                  (default: lumina)
#   SKMEMORY_HEALTH_DSN  pg DSN for the health probe     (default:
#                        postgresql://postgres:skmemory@localhost:5432/skmemory
#                        — the node-local dev default, same as the repo tests)
#   SKMEM_EMBED_URL      embed endpoint for the pg retrieval probe (default:
#                        http://192.168.0.100:11434/api/embed)
#   SKMEM_EMBED_MODEL    embed model                     (default: mxbai-embed-large)
#   SKMEM_BACKUP_ROOT    backup lineage root             (default:
#                        $HOME/.skcapstone/backups)
#   SKALERT_LIB          sk-alert helper lib to source   (default:
#                        $HOME/.hermes/scripts/lib/skalert.sh; optional — the
#                        Telegram alert is simply skipped if absent)
#
# Schedule: daily via cron/systemd-timer, typically wrapped by Hermes so its
# stdout digest feeds the synthesis prompt (see deploy/ops/README.md).
set -uo pipefail

AGENT="${SKAGENT:-lumina}"
AGENT_DIR="$HOME/.skcapstone/agents/$AGENT"
MEM="$AGENT_DIR/memory"
STATE="$AGENT_DIR/skmem-health-state.json"
OUTDIR="$AGENT_DIR/logs/skmem-health"; mkdir -p "$OUTDIR"
D="$(date +%F)"; REPORT="$OUTDIR/${D}-skmem-health.md"
PG_DSN="${SKMEMORY_HEALTH_DSN:-postgresql://postgres:skmemory@localhost:5432/skmemory}"
EMBED_URL="${SKMEM_EMBED_URL:-http://192.168.0.100:11434/api/embed}"
EMBED_MODEL="${SKMEM_EMBED_MODEL:-mxbai-embed-large}"
BROOT="${SKMEM_BACKUP_ROOT:-$HOME/.skcapstone/backups}"
NOW=$(date +%s)
# Pin an interpreter that actually has psycopg2 — cron runs under a bare PATH
# where /usr/bin/python3 lacks the driver (would false-report PG unreachable).
PYBIN="$HOME/.skenv/bin/python3"; [ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
# Resolve the sqlite3 CLI — cron/Hermes run under a bare PATH that lacks
# linuxbrew, so a plain `sqlite3` is "command not found" and the query check
# false-FAILs "not queryable". Prefer PATH, then linuxbrew, else python3 shim.
SQLITE="$(command -v sqlite3 || true)"
[ -n "$SQLITE" ] || { [ -x /home/linuxbrew/.linuxbrew/bin/sqlite3 ] && SQLITE=/home/linuxbrew/.linuxbrew/bin/sqlite3; }
# sq <db> <sql> — run a query, falling back to python3 if no sqlite3 binary.
# Uses an 8s busy-timeout so a concurrent skingest write (large WAL held mid-txn)
# makes the reader WAIT rather than instantly return empty and false-FAIL as
# "not queryable (schema/corruption)".
sq() {
  if [ -n "$SQLITE" ]; then "$SQLITE" -cmd ".timeout 8000" "$1" "$2" 2>/dev/null
  else "$PYBIN" - "$1" "$2" 2>/dev/null <<'PY'
import sqlite3, sys
db, q = sys.argv[1], sys.argv[2]
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=8)
    con.execute("PRAGMA busy_timeout=8000;")
    print(con.execute(q).fetchone()[0])
except Exception:
    pass
PY
  fi
}

# Mirror all stdout to a dated report file while keeping WORST logic in-process.
exec > >(tee "$REPORT"); ln -sf "$REPORT" "$OUTDIR/latest.md"

WORST=0   # 0=PASS 1=WARN 2=FAIL
np=0; nw=0; nf=0
emit() {  # emit LEVEL "check" "detail"
  local lvl="$1"; shift
  case "$lvl" in
    PASS) np=$((np+1));;
    WARN) nw=$((nw+1)); [ "$WORST" -lt 1 ] && WORST=1;;
    FAIL) nf=$((nf+1)); WORST=2;;
  esac
  printf '[%s] %s — %s\n' "$lvl" "$1" "$2"
}
# newest mtime (epoch) under a set of paths matching a find expr; echoes epoch or 0
newest_epoch() { find "$@" -printf '%T@\n' 2>/dev/null | cut -d. -f1 | sort -rn | head -1; }
age_h() { local e="${1:-0}"; [ "$e" -gt 0 ] 2>/dev/null && echo $(( (NOW - e) / 3600 )) || echo 999999; }
hb() { local h="$1"; if [ "$h" -lt 48 ]; then echo "${h}h"; else echo "$((h/24))d"; fi; }

echo "# skmemory Health — $(date '+%Y-%m-%d %H:%M %Z')  (agent: $AGENT)"
echo

# ── 1. Flat-file memory writes ────────────────────────────────────────────────
newest=$(newest_epoch "$MEM/short-term" "$MEM/mid-term" "$MEM/long-term" -type f -name '*.json')
h=$(age_h "$newest")
added24=$(find "$MEM/short-term" "$MEM/mid-term" -type f -name '*.json' -newermt '24 hours ago' 2>/dev/null | wc -l)
if   [ "$h" -lt 24 ]; then emit PASS "flat-file writes" "newest memory $(hb $h) old; ${added24} file(s) added/changed in 24h"
elif [ "$h" -lt 48 ]; then emit WARN "flat-file writes" "newest memory $(hb $h) old — writes slowing (${added24} in 24h)"
else                        emit FAIL "flat-file writes" "newest memory $(hb $h) old — memory pipeline appears stalled"
fi

# ── 2. SQLite index freshness + WAL health ───────────────────────────────────
# Retry: a periodic index rebuild swaps index.db and can be caught mid-flight
# (WAL checkpoint), briefly making the file appear absent — don't false-FAIL on it.
for _try in 1 2 3; do [ -f "$MEM/index.db" ] && break; sleep 1; done
if [ -f "$MEM/index.db" ]; then
  ih=$(age_h "$(stat -c %Y "$MEM/index.db")")
  walsz=$(stat -c %s "$MEM/index.db-wal" 2>/dev/null || echo 0); walmb=$((walsz/1048576))
  if   [ "$walmb" -gt 256 ]; then emit WARN "sqlite index" "index.db $(hb $ih) old; WAL ${walmb}MB — checkpoint may be stuck"
  elif [ "$ih" -lt 48 ];     then emit PASS "sqlite index" "index.db $(hb $ih) old; WAL ${walmb}MB"
  else                            emit WARN "sqlite index" "index.db $(hb $ih) old — not rebuilt recently; WAL ${walmb}MB"
  fi
else
  emit FAIL "sqlite index" "index.db missing at $MEM/index.db"
fi

# ── 3. skmem-pg (pgvector store) ─────────────────────────────────────────────
prev_rows=$("$PYBIN" -c "import json,sys;print(json.load(open('$STATE')).get('pg_rows','?'))" 2>/dev/null || echo '?')
pgout=$("$PYBIN" - "$PG_DSN" <<'PY' 2>&1
import sys,datetime
dsn=sys.argv[1]
try:
    import psycopg2
except Exception as e:
    print("ERR\tpsycopg2 unavailable in interpreter ("+str(e)[:50]+")"); sys.exit(0)
try:
    c=psycopg2.connect(dsn,connect_timeout=6);cur=c.cursor()
    cur.execute("select count(*), max(created_at) from memories")
    n,mx=cur.fetchone();c.close()
    age=(datetime.datetime.now(datetime.timezone.utc)-mx).total_seconds()/3600 if mx else 999999
    print(f"OK\t{n}\t{age:.1f}")
except Exception as e:
    msg=(str(e).splitlines() or [""])[0].strip() or type(e).__name__
    print("ERR\t"+msg[:90])
PY
)
# Guard against any stray non-tab output (e.g. a traceback leaking to stdout).
pgout=$(printf '%s' "$pgout" | grep -E '^(OK|ERR)\b' | head -1)
[ -z "$pgout" ] && pgout="ERR	no parseable result from PG probe"
if [ "${pgout%%$'\t'*}" = "OK" ]; then
  rows=$(echo "$pgout" | cut -f2); pgage=$(echo "$pgout" | cut -f3); pgageh=${pgage%.*}
  delta=""; [ "$prev_rows" != "?" ] && delta=" (Δ$(( rows - prev_rows )) since last check)"
  if   [ "${pgageh:-999999}" -lt 48 ]; then emit PASS "skmem-pg" "${rows} rows${delta}; newest write ${pgage}h ago"
  elif [ "${pgageh:-999999}" -lt 168 ]; then emit WARN "skmem-pg" "${rows} rows${delta}; newest write ${pgage}h ago — ingest slowing"
  else emit FAIL "skmem-pg" "${rows} rows${delta}; newest write ${pgage}h ago — PG ingest stalled"
  fi
  PG_ROWS="$rows"
else
  emit FAIL "skmem-pg" "unreachable at ${PG_DSN%%\?*} — ${pgout#ERR$'\t'}"
  PG_ROWS="$prev_rows"
fi

# ── 4. Backups — lineage-aware (GFS state + memory-flat rotations) ───────────
gfs_h=$(age_h "$(newest_epoch "$BROOT/gfs/daily" -type f -name '*.tar.gz' 2>/dev/null)")
mem_h=$(age_h "$(newest_epoch "$BROOT/$AGENT-memory/daily" -type f -name '*.tar.gz' 2>/dev/null)")
fresh=$(( gfs_h < mem_h ? gfs_h : mem_h ))          # freshest of the two lineages
detail="gfs $(hb $gfs_h) · mem-flat $(hb $mem_h)"
# A stalled memory-specific lineage matters even when GFS is fresh.
[ "$mem_h" -gt 96 ] && [ "$mem_h" -lt 999999 ] && detail="$detail  ⚠ mem-flat lineage behind"
[ "$mem_h" -ge 999999 ] && detail="gfs $(hb $gfs_h) · mem-flat MISSING"
if   [ "$fresh" -lt 36 ] && [ "$mem_h" -lt 192 ]; then emit PASS "backups" "$detail"
elif [ "$fresh" -lt 36 ];                          then emit WARN "backups" "a lineage is current but $detail"
elif [ "$fresh" -lt 192 ];                         then emit WARN "backups" "newest backup $(hb $fresh) old — $detail"
else emit FAIL "backups" "no backup in $(hb $fresh) — rotation stopped — $detail"
fi

# ── 5. skwhisper session ingestion ───────────────────────────────────────────
# Primary liveness signal is whisper.md's real mtime (daemon regenerates it every
# ~30m). systemd status is secondary — it can false-report in a stripped env that
# can't reach the user D-Bus, so it never alone drives a FAIL.
WHISPER="$AGENT_DIR/skwhisper/whisper.md"
sw=$(SKAGENT="$AGENT" "$HOME/.skenv/bin/skwhisper" status 2>&1)
pending=$(echo "$sw" | grep -iE 'Pending:' | grep -oE '[0-9]+' | head -1); pending=${pending:-0}
missing=$(echo "$sw" | grep -iE 'Missing file:' | grep -oE '[0-9]+' | head -1); missing=${missing:-0}
daemon_bad=$(echo "$sw" | grep -i 'Daemon' | grep -oiE '✗|inactive|dead|not' | head -1)
if [ -f "$WHISPER" ]; then wmin=$(( (NOW - $(stat -c %Y "$WHISPER")) / 60 )); else wmin=999999; fi
whlbl() { if [ "$1" -lt 90 ]; then echo "${1}m"; elif [ "$1" -lt 2880 ]; then echo "$(( $1/60 ))h"; else echo "$(( $1/1440 ))d"; fi; }
if [ ! -f "$WHISPER" ]; then
  emit FAIL "skwhisper" "whisper.md missing at $WHISPER — curation never ran"
elif [ "$wmin" -lt 90 ]; then
  # Fresh whisper.md ⇒ daemon is alive regardless of systemd query.
  if [ "$pending" -gt 10 ] || [ "$missing" -gt 0 ]; then
    emit WARN "skwhisper" "curating (whisper.md $(whlbl $wmin) old) but pending=$pending missing=$missing — digester behind"
  else
    emit PASS "skwhisper" "curating; pending=$pending missing=$missing; whisper.md refreshed $(whlbl $wmin) ago"
  fi
elif [ "$wmin" -lt 180 ]; then
  emit WARN "skwhisper" "whisper.md $(whlbl $wmin) old (regen is ~30m) — curate may be lagging; pending=$pending"
else
  # Stale whisper.md AND a bad systemd reading = genuine stop.
  if [ -n "$daemon_bad" ]; then emit FAIL "skwhisper" "daemon NOT active & whisper.md $(whlbl $wmin) old — curation stopped (pending=$pending)"
  else emit WARN "skwhisper" "whisper.md $(whlbl $wmin) old — curation stalled though daemon reports up (pending=$pending)"; fi
fi

# ── 6. SQLite returns CORRECT results (functional, not just fresh) ───────────
# Drift = mismatch between the index and the ACTIVE flat tiers. Archived memories
# (memory/archive/**) are intentional cold storage: the promoter moves them out
# of the tiers and prunes their index row, so they belong to NEITHER side of this
# comparison. If a just-archived row hasn't been pruned yet (archiver lag), it can
# transiently inflate sqlite_ct — that excess is bounded by the archive backlog,
# so we discount up to arch_ct of it rather than false-report "reindex stale".
flat_ct=$(find "$MEM/short-term" "$MEM/mid-term" "$MEM/long-term" -type f -name '*.json' 2>/dev/null | wc -l)
arch_ct=$(find "$MEM/archive" -type f -name '*.json' 2>/dev/null | wc -l)
sqlite_ct=$(sq "$MEM/index.db" "select count(*) from memories;"); sqlite_ct=${sqlite_ct:-'-1'}
sqlite_recent=$(sq "$MEM/index.db" "select count(*) from active_memories where context_tier in ('today','yesterday','week');"); sqlite_recent=${sqlite_recent:-'-1'}
if   [ "$sqlite_ct" -lt 0 ] 2>/dev/null; then emit FAIL "sqlite query" "index.db not queryable (schema/corruption)"
elif [ "$sqlite_ct" -eq 0 ]; then          emit FAIL "sqlite query" "returns 0 memories — empty index (needs 'skmemory reindex')"
else
  # Discount an index surplus that the archive backlog explains (archived-but-not-yet-pruned rows).
  if [ "$sqlite_ct" -gt "$flat_ct" ]; then
    over=$(( sqlite_ct - flat_ct )); over=$(( over > arch_ct ? over - arch_ct : 0 )); diff="$over"
  else
    diff=$(( flat_ct - sqlite_ct ))
  fi
  pct=$(( flat_ct > 0 ? diff*100/flat_ct : 0 ))
  if   [ "$pct" -gt 15 ]; then emit WARN "sqlite query" "$sqlite_ct rows vs flat $flat_ct (${pct}% drift, ${arch_ct} archived); recent-view=$sqlite_recent — reindex stale"
  else                         emit PASS "sqlite query" "$sqlite_ct rows (flat $flat_ct, ${arch_ct} archived, ${pct}% drift); recent-view=$sqlite_recent"
  fi
fi

# ── 7. skmem-pg RETURNS RESULTS CORRECTLY + is being updated (functional) ─────
# Embeds a probe query, runs a real vector search AND the hybrid function, and checks
# for un-embedded rows + drift vs flat truth. Catches the failure classes that pure
# freshness/count checks miss (broken retrieval fn, unindexed memories, silent drift).
pgfunc=$("$PYBIN" - "$PG_DSN" "$AGENT" "$flat_ct" "$EMBED_URL" "$EMBED_MODEL" <<'PY' 2>&1
import sys, json, urllib.request
dsn, agent, flat_ct, EMBED, MODEL = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
try:
    import psycopg2
except Exception as e:
    print("ERR psycopg2 unavailable: " + str(e)[:60]); sys.exit(0)
try:
    req = urllib.request.Request(EMBED,
        data=json.dumps({"model": MODEL, "input": agent + " memory"}).encode(),
        headers={"Content-Type": "application/json"})
    emb = json.load(urllib.request.urlopen(req, timeout=15))["embeddings"][0]
    vec = "[" + ",".join(f"{x:.6f}" for x in emb) + "]"
except Exception as e:
    print("ERR embed server (" + EMBED + ") unreachable: " + str(e).splitlines()[0][:50]); sys.exit(0)
try:
    c = psycopg2.connect(dsn, connect_timeout=6); cur = c.cursor()
    cur.execute("select count(*) filter (where embedding is null), count(*) from memories where agent=%s", (agent,))
    nulls, total = cur.fetchone()
    cur.execute("select count(*) from (select id from memories where embedding is not null and agent=%s "
                "order by embedding <=> %s::vector limit 5) t", (agent, vec))
    vec_hits = cur.fetchone()[0]
    try:
        cur.execute("select count(*) from hybrid_search_memories(%s, %s::vector, 5, %s)", (agent + " memory", vec, agent))
        hyb = cur.fetchone()[0]; hyberr = ""
    except Exception as e:
        hyb = -1; hyberr = str(e).splitlines()[0][:70]
    c.close()
    print(json.dumps({"nulls": nulls, "total": total, "vec_hits": vec_hits, "hyb": hyb, "hyberr": hyberr, "flat": flat_ct}))
except Exception as e:
    print("ERR pg query failed: " + str(e).splitlines()[0][:80])
PY
)
pgfunc=$(printf '%s' "$pgfunc" | grep -E '^(\{|ERR)' | head -1)
if printf '%s' "$pgfunc" | grep -q '^ERR'; then
  emit FAIL "pg retrieval" "${pgfunc#ERR }"
else
  nulls=$("$PYBIN" -c "import json,sys;print(json.loads(sys.argv[1])['nulls'])" "$pgfunc" 2>/dev/null)
  total=$("$PYBIN" -c "import json,sys;print(json.loads(sys.argv[1])['total'])" "$pgfunc" 2>/dev/null)
  vec_hits=$("$PYBIN" -c "import json,sys;print(json.loads(sys.argv[1])['vec_hits'])" "$pgfunc" 2>/dev/null)
  hyb=$("$PYBIN" -c "import json,sys;print(json.loads(sys.argv[1])['hyb'])" "$pgfunc" 2>/dev/null)
  hyberr=$("$PYBIN" -c "import json,sys;print(json.loads(sys.argv[1])['hyberr'])" "$pgfunc" 2>/dev/null)
  drift=$(( flat_ct > total ? flat_ct - total : total - flat_ct )); dpct=$(( flat_ct > 0 ? drift*100/flat_ct : 0 ))
  if   [ "${vec_hits:-0}" -lt 1 ];  then emit FAIL "pg retrieval" "vector search returned 0 results — retrieval broken (rows=$total)"
  elif [ "${hyb:-0}" -lt 0 ];       then emit FAIL "pg retrieval" "hybrid_search_memories() errors: ${hyberr:-unknown} — semantic search broken"
  elif [ "${nulls:-0}" -gt 0 ];     then emit WARN "pg retrieval" "$nulls/$total rows un-embedded (not vector-searchable) — run skmem_reconcile"
  elif [ "$dpct" -gt 15 ];          then emit WARN "pg retrieval" "$total rows vs flat $flat_ct (${dpct}% drift) — pg out of sync, run skmem_reconcile"
  else                                   emit PASS "pg retrieval" "vec=$vec_hits hybrid=$hyb; ${total} rows, 0 null-embeddings, ${dpct}% drift vs flat"
  fi
fi

# ── Summary + state persistence ──────────────────────────────────────────────
case "$WORST" in 0) V="🟢 ALL HEALTHY";; 1) V="🟡 DEGRADED";; 2) V="🔴 ATTENTION NEEDED";; esac
echo
echo "SUMMARY: $V — ${np} pass / ${nw} warn / ${nf} fail"

"$PYBIN" - "$STATE" "${PG_ROWS:-?}" "$NOW" "$WORST" <<'PY' 2>/dev/null
import json,sys
state,rows,now,worst=sys.argv[1:5]
try: rows=int(rows)
except: rows=None
json.dump({"pg_rows":rows,"last_run":int(now),"worst":int(worst)},open(state,"w"))
PY

# ── sk-alert on degradation (feeds Telegram via the skalert primitive) ────────
# Only alert on WARN/FAIL — the daily digest (Hermes synthesis) covers the healthy
# case. Deduped per severity with a 6h TTL so a persistent issue re-pings every 6h,
# not every run. GREEN clears the dedup so recovery is announced once. The alert
# lib is optional — absent it, the digest still prints and the script exits clean.
SKALERT_LIB="${SKALERT_LIB:-$HOME/.hermes/scripts/lib/skalert.sh}"
if [ -f "$SKALERT_LIB" ]; then
  # shellcheck disable=SC1090
  source "$SKALERT_LIB" 2>/dev/null || true
  if [ "$WORST" -ge 1 ] && command -v sk_alert >/dev/null 2>&1; then
    LINES=$(grep -E '^\[(WARN|FAIL)\]' "$REPORT" | head -5)
    LVL=$([ "$WORST" -ge 2 ] && echo crit || echo warn)
    sk_alert -l "$LVL" -k "skmem-health" -t 21600 \
      "🧠 skmemory health: $V  (${nw} warn / ${nf} fail)
$LINES

report: $OUTDIR/latest.md" || true
  fi
fi

# Let the tee subshell flush before we exit.
exec 1>&-; wait 2>/dev/null
exit 0
