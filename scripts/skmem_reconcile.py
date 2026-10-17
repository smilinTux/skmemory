#!/usr/bin/env python3
"""Full flat<->pg reconcile for skmem-pg (idempotent; safe to cron).

Source of truth = per-agent flat memory JSON files. Ensures every flat
memory is present+embedded in skmem-pg, and prunes pg rows whose flat file
is gone.

    skmem_reconcile.py [AGENT] [--force-stubs]
Env: EMBED_URL (default .100:11434/api/embed), EMBED_MODEL (mxbai-embed-large)

Version control note: this script lived unversioned at ~/skmem-build/ and
was invoked straight from cron on every node for months -- the one thing on
the estate that reconciles the fleet's memory store but that `git log`
couldn't see. It now lives in the skmemory repo
(scripts/skmem_reconcile.py); each node's crontab-invoked copy at
~/skmem-build/skmem_reconcile.py is a symlink into a checkout of this repo,
not an independent file. See
docs/postmortems/2026-10-16-sqlite-only-orphans.md.

Stub guard (card: skmemory sqlite-only-orphans, 2026-10-16): a flat file
that is itself a rescued SQLite-preview stub (content truncated to ~150
chars, metadata.recovered_from_sqlite_preview = True) must never be
backfilled into skmem-pg as if it were real content -- a reconcile run did
exactly that to 373 ids during the 2026-10-16 recovery, before this guard
existed. By default such ids are skipped (loudly) rather than inserted;
--force-stubs overrides for an operator who has verified this is correct.
"""

import csv
import glob
import io
import json
import os
import subprocess
import sys

import requests

LAYERS = ["short-term", "mid-term", "long-term"]
PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]

# Kept in sync by hand with skmemory/stub_markers.py in the main package (this
# script is a standalone cron entrypoint with no import path into that
# package). Any change to either must change both.
STUB_METADATA_KEY = "recovered_from_sqlite_preview"
STUB_CONTENT_MARKER = "[recovered from SQLite preview"


def is_stub_flat(obj: dict) -> bool:
    """True if a flat-file JSON payload carries the lossy-recovery marker.

    Args:
        obj: The parsed flat JSON memory payload.

    Returns:
        bool: True if this looks like a rescued SQLite-preview stub that
            must not be trusted as real content.
    """
    if not isinstance(obj, dict):
        return False
    meta = obj.get("metadata")
    if isinstance(meta, dict) and meta.get(STUB_METADATA_KEY):
        return True
    content = obj.get("content") or ""
    return STUB_CONTENT_MARKER in content


def parse_args(argv: list[str]) -> tuple[str, bool]:
    """Parse ``[AGENT] [--force-stubs]`` from argv (sys.argv[1:] shape).

    Args:
        argv: Command-line arguments, not including the program name.

    Returns:
        tuple[str, bool]: (agent, force_stubs).
    """
    force_stubs = "--force-stubs" in argv
    positional = [a for a in argv if not a.startswith("--")]
    agent = positional[0] if positional else os.environ.get("SKAGENT", "lumina")
    return agent, force_stubs


def psql(sql, want=False):
    args = PSQL + (["-tAF\t", "-c", sql] if want else ["-c", sql])
    return subprocess.run(args, capture_output=True, text=True).stdout


def psql_stdin(sql):
    return subprocess.run(PSQL + ["-f", "-"], input=sql, capture_output=True, text=True)


def embed(texts, embed_url, embed_model):
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
            return embed(texts[:m], embed_url, embed_model) + embed(
                texts[m:], embed_url, embed_model
            )
        texts = [texts[0][: max(200, len(texts[0]) // 2)]]
    raise RuntimeError("embed failed")


def vlit(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


def main(argv: list[str] | None = None) -> None:
    agent, force_stubs = parse_args(sys.argv[1:] if argv is None else argv)
    mem = os.path.expanduser(f"~/.skcapstone/agents/{agent}/memory")
    embed_url = os.environ.get("EMBED_URL", "http://192.168.0.100:11434/api/embed")
    embed_model = os.environ.get("EMBED_MODEL", "mxbai-embed-large")

    # flat truth (skip files whose stem != internal id is fine; stem is canonical key)
    flat = {}
    for layer in LAYERS:
        for fp in glob.glob(f"{mem}/{layer}/*.json"):
            stem = os.path.splitext(os.path.basename(fp))[0]
            if stem:
                flat[stem] = fp
    pg_ids = set(psql(f"select id from memories where agent='{agent}';", True).split())
    # memories_pkey is btree(id) ALONE -- not (id, agent) -- and the backfill below
    # inserts ON CONFLICT (id) DO NOTHING. So when two agents hold flat files with the
    # same id (the shared "Seed: emotion-*" records arrive in several agents' dirs via
    # Syncthing), whichever agent reconciles first claims the row and every other agent
    # can NEVER insert it. Counting those as "missing" made this job non-convergent:
    # opus reported missing=7 and re-embedded the same 7 records every single night,
    # forever, while the count never reached zero -- which also trains the operator to
    # ignore a nonzero missing, hiding real drift behind permanent noise.
    # Separate the two cases so `missing` means "absent from the table" and can hit 0.
    all_ids = set(psql("select id from memories;", True).split())
    shared = [i for i in flat if i not in pg_ids and i in all_ids]
    missing = [i for i in flat if i not in all_ids]
    print(
        f"[{agent}] flat={len(flat)} pg={len(pg_ids)} missing={len(missing)} shared={len(shared)}",
        flush=True,
    )
    if shared:
        shared_literals = ",".join("'" + i.replace("'", "''") + "'" for i in shared)
        owners = psql(
            f"select agent||':'||count(*) from memories where id in "
            f"({shared_literals}) group by agent;",
            True,
        ).split()
        print(
            f"[{agent}] shared ids already owned by -> {' '.join(owners)} "
            f"(single-column PK cannot represent per-agent ownership; not re-embedding)",
            flush=True,
        )

    # 1. backfill missing (embed + upsert) -- stub-flagged flat files are refused
    #    unless --force-stubs (card: skmemory sqlite-only-orphans, 2026-10-16).
    loaded = 0
    skipped_stubs: list[str] = []
    if missing:
        psql(
            "DROP TABLE IF EXISTS memories_bf; CREATE TABLE memories_bf "
            "(id text,layer text,role text,title text,content text,summary text,"
            "tags text,source text,created_at text,updated_at text,memory_json text,"
            "agent text,embedding text);"
        )
        batch = 24
        for i in range(0, len(missing), batch):
            pairs = []
            for mid in missing[i : i + batch]:
                try:
                    with open(flat[mid]) as fh:
                        o = json.load(fh)
                except Exception:
                    continue
                if not isinstance(o, dict) or not (o.get("content") or o.get("title")):
                    continue
                if is_stub_flat(o) and not force_stubs:
                    skipped_stubs.append(mid)
                    continue
                pairs.append((mid, o))
            if not pairs:
                continue
            embs = embed(
                [(o.get("content") or o.get("title") or " ")[:1100] for _, o in pairs],
                embed_url,
                embed_model,
            )
            buf = io.StringIO()
            w = csv.writer(buf)
            for (mid, o), e in zip(pairs, embs, strict=True):
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
            "INSERT INTO memories (id,layer,role,title,content,summary,tags,source,"
            "created_at,updated_at,memory_json,agent,embedding) SELECT "
            "id,layer,role,title,content,summary,tags::text[],source,"
            "created_at::timestamptz,COALESCE(NULLIF(updated_at,'')::timestamptz,"
            "created_at::timestamptz),memory_json::jsonb,agent,embedding::vector "
            "FROM memories_bf ON CONFLICT (id) DO NOTHING;"
        )
        psql("DROP TABLE IF EXISTS memories_bf;")

    if skipped_stubs:
        preview = ",".join(skipped_stubs[:20]) + ("..." if len(skipped_stubs) > 20 else "")
        print(
            f"[{agent}] REFUSED {len(skipped_stubs)} stub-flagged flat file(s) for pg "
            f"backfill (recovered_from_sqlite_preview marker present) -- these ids "
            f"were NOT inserted into skmem-pg. Pass --force-stubs only after verifying "
            f"this is genuinely the content to keep: {preview}",
            flush=True,
        )

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
        f"(SELECT 1 FROM flat_ids f WHERE f.id=m.id) RETURNING 1) SELECT count(*) FROM d;",
        True,
    ).strip()
    psql("DROP TABLE IF EXISTS flat_ids;")

    # 3. embed any null-vector rows
    nulls = [
        r.split("\t", 1)
        for r in psql(
            f"select id, left(regexp_replace(coalesce(content,title,' '),"
            f"E'[\\n\\r\\t]',' ','g'),1100) from memories where embedding is null "
            f"and agent='{agent}';",
            True,
        ).splitlines()
        if "\t" in r
    ]
    for i in range(0, len(nulls), 12):
        chunk = nulls[i : i + 12]
        es = embed([(c[1] or " ") for c in chunk], embed_url, embed_model)
        vals = ",".join(
            f"('{c[0].replace(chr(39), chr(39) * 2)}','{vlit(e)}')"
            for c, e in zip(chunk, es, strict=True)
        )
        psql_stdin(
            f"UPDATE memories m SET embedding=v.e::vector FROM (VALUES {vals}) AS v(id,e) "
            f"WHERE m.id=v.id;"
        )

    emb_stat = psql(
        f"select count(*) filter (where embedding is not null)||'/'||count(*) from "
        f"memories where agent='{agent}';",
        True,
    ).strip()
    print(
        f"[{agent}] backfilled={loaded} skipped_stubs={len(skipped_stubs)} pruned={pruned} "
        f"null_embedded={len(nulls)} embedded={emb_stat}",
        flush=True,
    )


if __name__ == "__main__":
    main()
