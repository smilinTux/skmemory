#!/usr/bin/env python3
"""Full flat<->pg reconcile for skmem-pg (idempotent; safe to cron).

Source of truth = per-agent flat memory JSON files. Ensures every flat memory
is present + embedded in skmem-pg, and prunes pg rows whose flat file is gone.

Canonical model (prb-6f069c5e): skmem-pg is LOCAL, per-node, and rebuildable
from source. It is NOT streaming-replicated, NOT a central/shared system of
record, and NOT a SPOF. The `memories` table is a DERIVED cache (same class as
`index.db`): this reconcile rebuilds it, agent-scoped and idempotently, from the
Syncthing-synced flat JSON. Embeddings are a deterministic function of flat
content + mxbai on .100, so any node regenerates them locally. This module talks
to the LOCAL container via `docker exec skmem-pg psql` (no host param), so it can
only ever act on the box it runs on; it must be present and scheduled on every
node, for every agent whose flat files that node serves.

Vendored from `~/skmem-build/skmem_reconcile.py` (the previously out-of-repo
production engine) so the rebuild path is versioned and testable in-repo.

Usage:
    python -m skmemory.reconcile [AGENT]        # default: $SKAGENT or lumina

Env:
    EMBED_URL    (default http://192.168.0.100:11434/api/embed)
    EMBED_MODEL  (default mxbai-embed-large)
"""

from __future__ import annotations

import csv
import glob
import io
import json
import os
import subprocess
import sys

import requests

LAYERS = ["short-term", "mid-term", "long-term"]
DEFAULT_EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.0.100:11434/api/embed")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
# Node-LOCAL psql. No host param: acts only on the box it runs on.
DEFAULT_PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]


def default_agent() -> str:
    return os.environ.get("SKAGENT", "lumina")


def _mem_dir(agent: str) -> str:
    return os.path.expanduser(f"~/.skcapstone/agents/{agent}/memory")


def reconcile(
    agent: str | None = None,
    *,
    mem_dir: str | None = None,
    embed_url: str | None = None,
    embed_model: str | None = None,
    psql_cmd: list[str] | None = None,
    verbose: bool = True,
) -> dict:
    """Reconcile one agent's flat JSON memories into the node-local skmem-pg.

    Returns a stats dict:
        {agent, flat, pg, missing, backfilled, pruned, null_embedded,
         embedded, total}
    where ``flat`` is the count of flat memory files, ``pg`` the pre-run pg row
    count for this agent, ``embedded``/``total`` the post-run embedded/row
    counts. On a clean idempotent second run ``backfilled == 0`` and
    ``pruned == 0``.
    """
    agent = agent or default_agent()
    mem = mem_dir or _mem_dir(agent)
    embed_url = embed_url or DEFAULT_EMBED_URL
    embed_model = embed_model or DEFAULT_EMBED_MODEL
    PSQL = list(psql_cmd) if psql_cmd else list(DEFAULT_PSQL)

    def psql(sql, want=False):
        args = PSQL + (["-tAF\t", "-c", sql] if want else ["-c", sql])
        return subprocess.run(args, capture_output=True, text=True).stdout

    def psql_stdin(sql):
        return subprocess.run(PSQL + ["-f", "-"], input=sql, capture_output=True, text=True)

    def embed(texts):
        for _ in range(4):
            try:
                j = requests.post(
                    embed_url, json={"model": embed_model, "input": texts}, timeout=180
                ).json()
                if "embeddings" in j and len(j["embeddings"]) == len(texts):
                    return j["embeddings"]
            except Exception:
                pass
            if len(texts) > 1:
                m = len(texts) // 2
                return embed(texts[:m]) + embed(texts[m:])
            texts = [texts[0][: max(200, len(texts[0]) // 2)]]
        raise RuntimeError("embed failed")

    def vlit(e):
        return "[" + ",".join(f"{x:.6f}" for x in e) + "]"

    def log(msg):
        if verbose:
            print(msg, flush=True)

    # flat truth (stem is the canonical key)
    flat = {}
    for layer in LAYERS:
        for fp in glob.glob(f"{mem}/{layer}/*.json"):
            stem = os.path.splitext(os.path.basename(fp))[0]
            if stem:
                flat[stem] = fp
    pg_ids = set(psql(f"select id from memories where agent='{agent}';", True).split())
    missing = [i for i in flat if i not in pg_ids]
    log(f"[{agent}] flat={len(flat)} pg={len(pg_ids)} missing={len(missing)}")

    # 1. backfill missing (embed + upsert)
    loaded = 0
    if missing:
        psql(
            "DROP TABLE IF EXISTS memories_bf; CREATE TABLE memories_bf (id text,layer text,"
            "role text,title text,content text,summary text,tags text,source text,"
            "created_at text,updated_at text,memory_json text,agent text,embedding text);"
        )
        B = 24
        for i in range(0, len(missing), B):
            pairs = []
            for mid in missing[i : i + B]:
                try:
                    with open(flat[mid]) as fh:
                        o = json.load(fh)
                    if isinstance(o, dict) and (o.get("content") or o.get("title")):
                        pairs.append((mid, o))
                except Exception:
                    pass
            if not pairs:
                continue
            embs = embed([(o.get("content") or o.get("title") or " ")[:1100] for _, o in pairs])
            buf = io.StringIO()
            w = csv.writer(buf)
            for (mid, o), e in zip(pairs, embs, strict=False):
                tags = (
                    "{"
                    + ",".join(
                        '"' + str(t).replace('"', '\\"') + '"' for t in (o.get("tags") or [])
                    )
                    + "}"
                )
                cr = o.get("created_at") or "1970-01-01T00:00:00+00:00"
                w.writerow(
                    [
                        mid,
                        o.get("layer", ""),
                        o.get("role", "general"),
                        o.get("title", ""),
                        o.get("content", ""),
                        o.get("summary", ""),
                        tags,
                        o.get("source", ""),
                        cr,
                        o.get("updated_at") or cr,
                        json.dumps(o),
                        o.get("agent", agent),
                        vlit(e),
                    ]
                )
            subprocess.run(
                PSQL + ["-c", "COPY memories_bf FROM STDIN WITH (FORMAT csv);"],
                input=buf.getvalue(),
                capture_output=True,
                text=True,
            )
            loaded += len(pairs)
        psql_stdin(
            "INSERT INTO memories (id,layer,role,title,content,summary,tags,source,created_at,"
            "updated_at,memory_json,agent,embedding) SELECT id,layer,role,title,content,summary,"
            "tags::text[],source,created_at::timestamptz,"
            "COALESCE(NULLIF(updated_at,'')::timestamptz,created_at::timestamptz),"
            "memory_json::jsonb,agent,embedding::vector FROM memories_bf ON CONFLICT (id) DO NOTHING;"
        )
        psql("DROP TABLE IF EXISTS memories_bf;")

    # 2. prune orphans (pg rows for this agent with no flat file)
    psql("DROP TABLE IF EXISTS flat_ids; CREATE TABLE flat_ids (id text primary key);")
    subprocess.run(
        PSQL + ["-c", "COPY flat_ids FROM STDIN;"],
        input="\n".join(flat.keys()),
        capture_output=True,
        text=True,
    )
    pruned = psql(
        f"WITH d AS (DELETE FROM memories m WHERE m.agent='{agent}' AND NOT EXISTS "
        "(SELECT 1 FROM flat_ids f WHERE f.id=m.id) RETURNING 1) SELECT count(*) FROM d;",
        True,
    ).strip()
    psql("DROP TABLE IF EXISTS flat_ids;")

    # 3. embed any null-vector rows
    nulls = [
        r.split("\t", 1)
        for r in psql(
            "select id, left(regexp_replace(coalesce(content,title,' '),E'[\\n\\r\\t]',' ','g'),"
            f"1100) from memories where embedding is null and agent='{agent}';",
            True,
        ).splitlines()
        if "\t" in r
    ]
    for i in range(0, len(nulls), 12):
        ch = nulls[i : i + 12]
        es = embed([(c[1] or " ") for c in ch])
        vals = ",".join(
            "('{}','{}')".format(c[0].replace("'", "''"), vlit(e))
            for c, e in zip(ch, es, strict=False)
        )
        psql_stdin(
            f"UPDATE memories m SET embedding=v.e::vector FROM (VALUES {vals}) AS v(id,e) "
            "WHERE m.id=v.id;"
        )

    emb_stat = psql(
        "select count(*) filter (where embedding is not null)||'/'||count(*) from memories "
        f"where agent='{agent}';",
        True,
    ).strip()
    log(
        f"[{agent}] backfilled={loaded} pruned={pruned} "
        f"null_embedded={len(nulls)} embedded={emb_stat}"
    )

    try:
        embedded_str, total_str = emb_stat.split("/")
        embedded, total = int(embedded_str), int(total_str)
    except Exception:
        embedded, total = None, None
    try:
        pruned_n = int(pruned)
    except Exception:
        pruned_n = None

    return {
        "agent": agent,
        "flat": len(flat),
        "pg": len(pg_ids),
        "missing": len(missing),
        "backfilled": loaded,
        "pruned": pruned_n,
        "null_embedded": len(nulls),
        "embedded": embedded,
        "total": total,
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    agent = argv[0] if argv else default_agent()
    reconcile(agent)


if __name__ == "__main__":
    main()
