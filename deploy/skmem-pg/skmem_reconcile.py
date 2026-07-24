#!/usr/bin/env python3
"""Full flat<->pg reconcile for skmem-pg (idempotent; safe to cron).
Source of truth = per-agent flat memory JSON files. Ensures every flat memory
is present+embedded in skmem-pg, and prunes pg rows whose flat file is gone.

  skmem_reconcile.py [AGENT] [--force]   # default: lumina
Env: EMBED_URL (default .100:11434/api/embed), EMBED_MODEL (mxbai-embed-large)

Prune guardrail (card 6b8b3ced): on a freshly wiped / mid-Syncthing-sync box the
flat store can be empty or nearly so before it is restored. The prune step would
then delete every derived pg row for the agent and report success. This script
REFUSES a destructive prune when the flat source is empty/below a floor or when
the prune would remove more than a capped fraction of the current pg rows, unless
forced (--force or SKMEMORY_RECONCILE_FORCE=1). Refusals log loudly + sk-alert.
  SKMEMORY_RECONCILE_PRUNE_FLOOR         (default 1)
  SKMEMORY_RECONCILE_MAX_PRUNE_FRACTION  (default 0.20)
  SKMEMORY_RECONCILE_PRUNE_ALERT_ROWS    (default 50)
"""
import os, sys, json, glob, time, subprocess, requests

_ARGS = [a for a in sys.argv[1:] if a not in ("--force", "-f")]
FORCE = any(a in ("--force", "-f") for a in sys.argv[1:]) or \
    os.environ.get("SKMEMORY_RECONCILE_FORCE", "").lower() in ("1", "true", "yes")
AGENT = _ARGS[0] if _ARGS else os.environ.get("SKAGENT", "lumina")
MEM = os.path.expanduser(f"~/.skcapstone/agents/{AGENT}/memory")
LAYERS = ["short-term", "mid-term", "long-term"]
EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.0.100:11434/api/embed")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]
PRUNE_FLOOR = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_FLOOR", "1"))
MAX_PRUNE_FRACTION = float(os.environ.get("SKMEMORY_RECONCILE_MAX_PRUNE_FRACTION", "0.20"))
PRUNE_MIN_SAMPLE = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_MIN_SAMPLE", "20"))
PRUNE_ALERT_ROWS = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_ALERT_ROWS", "50"))

def prune_guard(flat_count, pg_count, would_prune, floor=PRUNE_FLOOR,
                max_fraction=MAX_PRUNE_FRACTION, min_sample=PRUNE_MIN_SAMPLE, force=FORCE):
    """Return (allowed, reason). Refuse destructive prune on empty/partial flat store.

    The fraction cap only applies once pg holds >= min_sample rows; on tiny stores
    a single legitimate delete trivially exceeds any fraction. The floor check
    guards the true cold-boot wipe (large pg + empty flat) at any size.
    """
    if would_prune <= 0:
        return True, "noop (nothing to prune)"
    if force:
        return True, f"force override (would prune {would_prune}/{pg_count})"
    if flat_count < floor:
        return False, (f"flat source count {flat_count} < floor {floor}: empty/unrestored "
                       f"flat store, refusing prune of {would_prune}/{pg_count} pg rows "
                       "(cold-boot guard; --force / SKMEMORY_RECONCILE_FORCE=1 to override)")
    if pg_count >= min_sample and would_prune / pg_count > max_fraction:
        return False, (f"prune would remove {would_prune}/{pg_count} rows "
                       f"({would_prune/pg_count:.1%}) > cap {max_fraction:.1%}: refusing "
                       "(suspected partial/mid-sync flat store; --force to override)")
    return True, f"ok (prune {would_prune}/{pg_count})"

def alert(msg, level="warn", key=None):
    """Best-effort sk-alert; never fatal."""
    cmd = ["sk-alert", "-l", level] + (["-k", key] if key else []) + [msg]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        pass

def psql(sql, want=False):
    args = PSQL + (["-tAF\t", "-c", sql] if want else ["-c", sql])
    return subprocess.run(args, capture_output=True, text=True).stdout
def psql_stdin(sql):
    return subprocess.run(PSQL + ["-f", "-"], input=sql, capture_output=True, text=True)
def embed(texts):
    for _ in range(4):
        try:
            j = requests.post(EMBED_URL, json={"model": EMBED_MODEL, "input": texts}, timeout=180).json()
            if "embeddings" in j and len(j["embeddings"]) == len(texts): return j["embeddings"]
        except Exception: pass
        if len(texts) > 1:
            m = len(texts)//2; return embed(texts[:m]) + embed(texts[m:])
        texts = [texts[0][:max(200, len(texts[0])//2)]]
    raise RuntimeError("embed failed")
def vlit(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"

# flat truth (skip files whose stem != internal id is fine; stem is canonical key)
flat = {}
for L in LAYERS:
    for fp in glob.glob(f"{MEM}/{L}/*.json"):
        stem = os.path.splitext(os.path.basename(fp))[0]
        if stem: flat[stem] = fp
pg_ids = set(psql("select id from memories where agent='%s';" % AGENT, True).split())
missing = [i for i in flat if i not in pg_ids]
print(f"[{AGENT}] flat={len(flat)} pg={len(pg_ids)} missing={len(missing)}", flush=True)

# 1. backfill missing (embed + upsert)
loaded = 0
if missing:
    psql("DROP TABLE IF EXISTS memories_bf; CREATE TABLE memories_bf (id text,layer text,role text,title text,content text,summary text,tags text,source text,created_at text,updated_at text,memory_json text,agent text,embedding text);")
    import csv, io
    B = 24
    for i in range(0, len(missing), B):
        pairs = []
        for mid in missing[i:i+B]:
            try:
                o = json.load(open(flat[mid]))
                if isinstance(o, dict) and (o.get("content") or o.get("title")): pairs.append((mid, o))
            except Exception: pass
        if not pairs: continue
        embs = embed([(o.get("content") or o.get("title") or " ")[:1100] for _, o in pairs])
        buf = io.StringIO(); w = csv.writer(buf)
        for (mid, o), e in zip(pairs, embs):
            tags = "{" + ",".join('"'+str(t).replace('"','\\"')+'"' for t in (o.get("tags") or [])) + "}"
            cr = o.get("created_at") or "1970-01-01T00:00:00+00:00"
            w.writerow([mid, o.get("layer",""), o.get("role","general"), o.get("title",""),
                        o.get("content",""), o.get("summary",""), tags, o.get("source",""),
                        cr, o.get("updated_at") or cr, json.dumps(o), o.get("agent", AGENT), vlit(e)])
        subprocess.run(PSQL + ["-c", "COPY memories_bf FROM STDIN WITH (FORMAT csv);"], input=buf.getvalue(), capture_output=True, text=True)
        loaded += len(pairs)
    psql_stdin("INSERT INTO memories (id,layer,role,title,content,summary,tags,source,created_at,updated_at,memory_json,agent,embedding) SELECT id,layer,role,title,content,summary,tags::text[],source,created_at::timestamptz,COALESCE(NULLIF(updated_at,'')::timestamptz,created_at::timestamptz),memory_json::jsonb,agent,embedding::vector FROM memories_bf ON CONFLICT (id) DO NOTHING;")
    psql("DROP TABLE IF EXISTS memories_bf;")

# 2. prune orphans (pg rows for this agent with no flat file) -- GUARDED
would_prune = len(pg_ids - set(flat.keys()))
allowed, reason = prune_guard(len(flat), len(pg_ids), would_prune)
if not allowed:
    print(f"[{AGENT}] PRUNE REFUSED: {reason}", flush=True)
    alert(f"🚨 skmem-pg reconcile [{AGENT}] REFUSED prune of {would_prune}/{len(pg_ids)} rows: {reason}",
          level="crit", key=f"skmem-reconcile-prune-refused-{AGENT}")
    pruned = "0"
else:
    psql("DROP TABLE IF EXISTS flat_ids; CREATE TABLE flat_ids (id text primary key);")
    subprocess.run(PSQL + ["-c", "COPY flat_ids FROM STDIN;"], input="\n".join(flat.keys()), capture_output=True, text=True)
    pruned = psql("WITH d AS (DELETE FROM memories m WHERE m.agent='%s' AND NOT EXISTS (SELECT 1 FROM flat_ids f WHERE f.id=m.id) RETURNING 1) SELECT count(*) FROM d;" % AGENT, True).strip()
    psql("DROP TABLE IF EXISTS flat_ids;")
    try:
        if int(pruned) >= PRUNE_ALERT_ROWS:
            alert(f"⚠️ skmem-pg reconcile [{AGENT}] pruned {pruned} orphan rows (flat={len(flat)}, pg={len(pg_ids)})",
                  level="warn", key=f"skmem-reconcile-prune-large-{AGENT}")
    except (TypeError, ValueError):
        pass

# 3. embed any null-vector rows
nulls = [r.split("\t",1) for r in psql("select id, left(regexp_replace(coalesce(content,title,' '),E'[\\n\\r\\t]',' ','g'),1100) from memories where embedding is null and agent='%s';" % AGENT, True).splitlines() if "\t" in r]
for i in range(0, len(nulls), 12):
    ch = nulls[i:i+12]; es = embed([(c[1] or " ") for c in ch])
    vals = ",".join("('%s','%s')" % (c[0].replace("'","''"), vlit(e)) for c, e in zip(ch, es))
    psql_stdin("UPDATE memories m SET embedding=v.e::vector FROM (VALUES %s) AS v(id,e) WHERE m.id=v.id;" % vals)

emb_stat = psql("select count(*) filter (where embedding is not null)||'/'||count(*) from memories where agent='%s';" % AGENT, True).strip()
print(f"[{AGENT}] backfilled={loaded} pruned={pruned} null_embedded={len(nulls)} embedded={emb_stat}", flush=True)
