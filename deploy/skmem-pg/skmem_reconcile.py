#!/usr/bin/env python3
"""Full flat<->pg reconcile for skmem-pg (idempotent; safe to cron).
Source of truth = per-agent flat memory JSON files. Ensures every flat memory
is present+embedded in skmem-pg, and prunes pg rows whose flat file is gone.

  skmem_reconcile.py [AGENT]        # default: lumina
Env: EMBED_URL (default .100:11434/api/embed), EMBED_MODEL (mxbai-embed-large)
"""
import os, sys, json, glob, time, subprocess, requests

AGENT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SKAGENT", "lumina")
MEM = os.path.expanduser(f"~/.skcapstone/agents/{AGENT}/memory")
LAYERS = ["short-term", "mid-term", "long-term"]
EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.0.100:11434/api/embed")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]

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

# 2. prune orphans (pg rows for this agent with no flat file)
psql("DROP TABLE IF EXISTS flat_ids; CREATE TABLE flat_ids (id text primary key);")
subprocess.run(PSQL + ["-c", "COPY flat_ids FROM STDIN;"], input="\n".join(flat.keys()), capture_output=True, text=True)
pruned = psql("WITH d AS (DELETE FROM memories m WHERE m.agent='%s' AND NOT EXISTS (SELECT 1 FROM flat_ids f WHERE f.id=m.id) RETURNING 1) SELECT count(*) FROM d;" % AGENT, True).strip()
psql("DROP TABLE IF EXISTS flat_ids;")

# 3. embed any null-vector rows
nulls = [r.split("\t",1) for r in psql("select id, left(regexp_replace(coalesce(content,title,' '),E'[\\n\\r\\t]',' ','g'),1100) from memories where embedding is null and agent='%s';" % AGENT, True).splitlines() if "\t" in r]
for i in range(0, len(nulls), 12):
    ch = nulls[i:i+12]; es = embed([(c[1] or " ") for c in ch])
    vals = ",".join("('%s','%s')" % (c[0].replace("'","''"), vlit(e)) for c, e in zip(ch, es))
    psql_stdin("UPDATE memories m SET embedding=v.e::vector FROM (VALUES %s) AS v(id,e) WHERE m.id=v.id;" % vals)

emb_stat = psql("select count(*) filter (where embedding is not null)||'/'||count(*) from memories where agent='%s';" % AGENT, True).strip()
print(f"[{AGENT}] backfilled={loaded} pruned={pruned} null_embedded={len(nulls)} embedded={emb_stat}", flush=True)
