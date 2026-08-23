#!/usr/bin/env python3
"""Full flat<->pg reconcile for skmem-pg (idempotent; safe to cron).

Source of truth = per-agent flat memory JSON files. Ensures every flat memory
is present + embedded in skmem-pg, and prunes pg rows whose flat file is gone.

Canonical model (prb-6f069c5e): skmem-pg is LOCAL, per-node, and rebuildable
from source. It is NOT streaming-replicated, NOT a central/shared system of
record, and NOT a SPOF. The `memories` table is a DERIVED cache (same class as
`index.db`): this reconcile rebuilds it, agent-scoped and idempotently, from the
Syncthing-synced flat JSON. Embeddings are a deterministic function of flat
content + mxbai on .100, so any node regenerates them locally. This module uses
the node-local ``SKMEMORY_PG_DSN`` when configured. The DSN is read from the
environment and never placed in process arguments or logs. A legacy Docker
transport remains available for nodes without a DSN and explicit test transports.

Vendored from `~/skmem-build/skmem_reconcile.py` (the previously out-of-repo
production engine) so the rebuild path is versioned and testable in-repo.

Prune guardrail (cold-boot safety, card 6b8b3ced): the prune step deletes pg
rows whose flat file is gone. On a freshly wiped / mid-Syncthing-sync machine
the flat store can be empty or nearly so *before* it is restored; a naive prune
then deletes every derived pg row for that agent and reports success. reconcile
therefore REFUSES a destructive prune when the flat source is empty/below a floor
or when the prune would remove more than a capped fraction of the current pg rows,
unless an explicit force override is given. Refusals log loudly and (best effort)
alert to sk-alert.

Resurrection guard (card 7d3e9fcc): the backfill step re-inserts any flat memory
missing from pg. That is a resurrection hazard for a *deliberately forgotten*
memory: `SKMemoryStore.forget` deletes it from the flat store + pgvector + AGE,
but a stale flat copy that reappears later (Syncthing re-delivering the file from
a node that has not seen the delete, a second source path, or an ingest
re-import) looks like a brand-new "missing" memory and would be re-inserted.
forget() therefore records a durable tombstone next to the flat memories (see
`skmemory.tombstones`); reconcile loads those ids and REFUSES to backfill any
tombstoned memory, even with a stale flat copy present, and treats a tombstoned
row still in pg as an orphan to be pruned. Forgotten memories stay gone.

Usage:
    python -m skmemory.reconcile [AGENT] [--force]   # default: $SKAGENT or lumina
    python -m skmemory.reconcile --all [--force]     # every provisioned agent
    python -m skmemory.reconcile --agents a,b,c      # explicit list

Env:
    EMBED_URL    (default http://192.168.0.100:11434/api/embed)
    EMBED_MODEL  (default mxbai-embed-large)
    SKMEMORY_RECONCILE_PRUNE_FLOOR         (default 1)   flat count must be >= this to prune
    SKMEMORY_RECONCILE_MAX_PRUNE_FRACTION  (default 0.20) refuse if prune > this fraction of pg
    SKMEMORY_RECONCILE_PRUNE_MIN_SAMPLE    (default 20)  fraction cap only applies at pg >= this
    SKMEMORY_RECONCILE_PRUNE_ALERT_ROWS    (default 50)  alert when a prune removes >= this many
    SKMEMORY_RECONCILE_FORCE               (1 to force prune past the guardrail)
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import glob
import io
import json
import os
import re
import subprocess
import sys

import requests

from skmemory.invalid_records import payload_memory_id, quarantine_invalid_flat_file
from skmemory.tombstones import load_tombstones

LAYERS = ["short-term", "mid-term", "long-term"]
DEFAULT_EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.0.100:11434/api/embed")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
# Node-LOCAL psql. No host param: acts only on the box it runs on.
DEFAULT_PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]


def default_psql_cmd() -> list[str]:
    """Select the protected DSN transport before the legacy Docker fallback."""
    if os.environ.get("SKMEMORY_PG_DSN", "").strip():
        return [sys.executable, "-m", "skmemory.dsn_psql"]
    return list(DEFAULT_PSQL)


class ReconcileTransportError(RuntimeError):
    """The psql transport to skmem-pg is unavailable; reconcile fails closed.

    Raised when the transport itself cannot carry SQL (docker socket
    permission denied, missing docker/psql binary, unreachable DSN), as
    opposed to a query the server rejected. The distinction matters because
    a transport failure must never be misread as an empty result set.
    """


#: stderr fragments that mark a transport-level failure rather than a SQL
#: error from a healthy server.
_TRANSPORT_MARKERS = (
    "permission denied",
    "cannot connect to the docker daemon",
    "no such container",
    "connection refused",
    "connection failed",
    "could not connect",
    "transport failed",
    "no such file or directory",
)


def _transport_label(psql_cmd: list[str]) -> str:
    """Human name for a psql transport; never carries credentials or a DSN."""
    if "skmemory.dsn_psql" in psql_cmd:
        return "dsn (SKMEMORY_PG_DSN via skmemory.dsn_psql)"
    if "docker" in psql_cmd and "skmem-pg" in psql_cmd:
        return "docker exec skmem-pg"
    return os.path.basename(psql_cmd[0]) if psql_cmd else "unknown"


def _scrub_stderr(text: str) -> str:
    """Redact credential-shaped fragments from transport stderr."""
    text = re.sub(r"(?i)password=\S+", "password=***", text)
    return re.sub(r"://[^:/\s]+:[^@\s]+@", "://***:***@", text)


def _stderr_tail(stderr: str) -> str:
    """The scrubbed last stderr line, for compact failure messages."""
    lines = _scrub_stderr(stderr.strip()).splitlines()
    return lines[-1] if lines else ""


def _is_transport_failure(stderr: str) -> bool:
    """Whether stderr reads as a dead transport rather than a rejected query."""
    low = stderr.lower()
    return any(marker in low for marker in _TRANSPORT_MARKERS)


def probe_transport(psql_cmd: list[str]) -> None:
    """Fail closed unless the chosen psql transport answers a trivial query.

    Runs ``select 1;`` through the transport before reconcile issues any
    counting query or mutation. A docker-socket permission denial, a missing
    binary, or an unreachable DSN raises here, naming the transport, instead
    of surfacing downstream as a phantom empty result.

    Args:
        psql_cmd: The psql command vector (docker exec form or DSN shim).

    Raises:
        ReconcileTransportError: The transport cannot carry SQL.
    """
    label = _transport_label(psql_cmd)
    try:
        result = subprocess.run(
            list(psql_cmd) + ["-tA", "-c", "select 1;"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReconcileTransportError(
            f"skmem-pg transport unavailable ({label}): {_scrub_stderr(str(exc))}"
        ) from exc
    if result.returncode != 0:
        raise ReconcileTransportError(
            f"skmem-pg transport unavailable ({label}): {_stderr_tail(result.stderr or '')}"
        )


# --- prune guardrail defaults (cold-boot / empty-source safety) --------------
# Minimum flat-source count required before ANY destructive prune is allowed.
# The default of 1 means an empty flat store (0 files) NEVER prunes -- that is
# the cold-boot / unrestored-machine case where the flat tree has not synced yet.
DEFAULT_PRUNE_FLOOR = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_FLOOR", "1"))
# Refuse (absent force) if the prune would remove more than this fraction of the
# current pg rows for the agent. Guards a partial/mid-sync flat store that is
# non-empty but has lost most of its files.
DEFAULT_MAX_PRUNE_FRACTION = float(os.environ.get("SKMEMORY_RECONCILE_MAX_PRUNE_FRACTION", "0.20"))
# The fraction cap only applies once pg holds at least this many rows. Below it a
# single legitimate delete can trivially exceed any fraction (1 of 3 = 33%), so the
# fraction heuristic is meaningless on tiny stores; the empty/floor guard still
# fully protects the true cold-boot wipe (large pg, empty/near-empty flat) at any
# size, since that is caught by the floor check regardless of sample size.
DEFAULT_PRUNE_MIN_SAMPLE = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_MIN_SAMPLE", "20"))
# Alert (best effort) when an allowed prune removes at least this many rows.
DEFAULT_PRUNE_ALERT_ROWS = int(os.environ.get("SKMEMORY_RECONCILE_PRUNE_ALERT_ROWS", "50"))


def default_agent() -> str:
    return os.environ.get("SKAGENT", "lumina")


def _agents_base_dir() -> str:
    """Base directory holding per-agent homes.

    Mirrors ``skmemory.agents`` resolution so discovery honours the same
    overrides (used by tests to point at a throwaway tree):
      1. ``SKMEMORY_HOME``   -> the agent base dir directly
      2. ``SKCAPSTONE_HOME`` -> ``<that>/agents``
      3. default             -> ``~/.skcapstone/agents``
    """
    home = os.environ.get("SKMEMORY_HOME")
    if home:
        return home
    skcap = os.environ.get("SKCAPSTONE_HOME")
    if skcap:
        return os.path.join(skcap, "agents")
    return os.path.expanduser("~/.skcapstone/agents")


def prune_guard(
    flat_count: int,
    pg_count: int,
    would_prune: int,
    *,
    floor: int = DEFAULT_PRUNE_FLOOR,
    max_fraction: float = DEFAULT_MAX_PRUNE_FRACTION,
    min_sample: int = DEFAULT_PRUNE_MIN_SAMPLE,
    force: bool = False,
) -> tuple[bool, str]:
    """Decide whether a destructive prune is safe. Pure; no I/O.

    Returns ``(allowed, reason)``. A prune is REFUSED (``allowed=False``) when,
    absent ``force``, the flat source looks empty/unrestored (``flat_count <
    floor``) or -- once pg holds at least ``min_sample`` rows -- the prune would
    remove more than ``max_fraction`` of the current pg rows. This is the
    cold-boot / mid-Syncthing-sync guardrail: a wiped or half-synced flat tree
    must never wipe the derived pg index. The fraction cap is skipped on tiny
    stores (``pg_count < min_sample``) where a single legitimate delete would
    trivially exceed any fraction; the floor check still guards the true
    cold-boot wipe (large pg + empty flat) at any size.
    """
    if would_prune <= 0:
        return True, "noop (nothing to prune)"
    if force:
        return True, f"force override (would prune {would_prune}/{pg_count})"
    if flat_count < floor:
        return False, (
            f"flat source count {flat_count} < floor {floor}: empty/unrestored flat "
            f"store, refusing destructive prune of {would_prune}/{pg_count} pg rows "
            "(cold-boot guard; set SKMEMORY_RECONCILE_FORCE=1 or pass --force to override)"
        )
    if pg_count >= min_sample:
        frac = would_prune / pg_count
        if frac > max_fraction:
            return False, (
                f"prune would remove {would_prune}/{pg_count} rows ({frac:.1%}) > cap "
                f"{max_fraction:.1%}: refusing (suspected partial/mid-sync flat store; "
                "set SKMEMORY_RECONCILE_FORCE=1 or pass --force to override)"
            )
    return True, f"ok (prune {would_prune}/{pg_count})"


def _alert(message: str, *, level: str = "warn", key: str | None = None) -> None:
    """Best-effort sk-alert. Never raises; a missing/failed alerter is non-fatal."""
    cmd = ["sk-alert", "-l", level]
    if key:
        cmd += ["-k", key]
    cmd.append(message)
    with contextlib.suppress(Exception):
        subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def _mem_dir(agent: str) -> str:
    return os.path.join(_agents_base_dir(), agent, "memory")


def discover_agents(agents_base: str | None = None) -> list[str]:
    """Discover every provisioned agent that has a memory dir.

    Scans the agent base dir for subdirectories that contain a ``memory/``
    directory, excluding ``*-template`` scaffolds. This is the "all agents"
    source for :func:`reconcile_all`; the acceptance contract is "every agent
    with a memory dir", so we key on the memory dir (not on the presence of a
    ``config/skmemory.yaml``, which non-lumina MCP-only agents may lack).

    Returns a sorted list of agent names (empty if the base dir is absent).
    """
    base = agents_base or _agents_base_dir()
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base)):
        if name.endswith("-template"):
            continue
        if os.path.isdir(os.path.join(base, name, "memory")):
            out.append(name)
    return out



def _embeddings_from_response(payload) -> list | None:
    """Pull the embedding vectors out of an embedding-endpoint response.

    Two wire shapes are in use across the fleet and both must work, because
    ``embed_url`` points at whichever one a node happens to serve:

    * Ollama native ``/api/embed``   -> ``{"embeddings": [[...], ...]}``
    * OpenAI-compatible ``/v1/embeddings`` -> ``{"data": [{"embedding": [...],
      "index": 0}, ...]}`` (llama.cpp, vLLM, Ollama's ``/v1`` shim)

    Args:
        payload: The decoded JSON body of the embedding response.

    Returns:
        The list of vectors in request order, or ``None`` if the payload is
        not a recognised embedding response (which the caller treats as a
        failed attempt and retries).
    """
    if not isinstance(payload, dict):
        return None
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list):
        return embeddings
    data = payload.get("data")
    if isinstance(data, list):
        rows = []
        for i, row in enumerate(data):
            if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
                return None
            idx = row.get("index")
            rows.append((idx if isinstance(idx, int) else i, row["embedding"]))
        return [vec for _, vec in sorted(rows, key=lambda r: r[0])]
    return None

def reconcile(
    agent: str | None = None,
    *,
    mem_dir: str | None = None,
    embed_url: str | None = None,
    embed_model: str | None = None,
    psql_cmd: list[str] | None = None,
    verbose: bool = True,
    force_prune: bool | None = None,
    prune_floor: int | None = None,
    max_prune_fraction: float | None = None,
    prune_min_sample: int | None = None,
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
    PSQL = list(psql_cmd) if psql_cmd else default_psql_cmd()
    floor = DEFAULT_PRUNE_FLOOR if prune_floor is None else prune_floor
    max_frac = DEFAULT_MAX_PRUNE_FRACTION if max_prune_fraction is None else max_prune_fraction
    min_sample = DEFAULT_PRUNE_MIN_SAMPLE if prune_min_sample is None else prune_min_sample
    force = (
        os.environ.get("SKMEMORY_RECONCILE_FORCE", "").lower() in ("1", "true", "yes")
        if force_prune is None
        else force_prune
    )

    label = _transport_label(PSQL)

    def run_psql(args, *, input_text=None):
        try:
            result = subprocess.run(args, input=input_text, capture_output=True, text=True)
        except OSError as exc:
            raise ReconcileTransportError(
                f"skmem-pg transport unavailable ({label}): {_scrub_stderr(str(exc))}"
            ) from exc
        if result.returncode != 0:
            tail = _stderr_tail(result.stderr or "")
            if _is_transport_failure(result.stderr or ""):
                raise ReconcileTransportError(f"skmem-pg transport unavailable ({label}): {tail}")
            raise RuntimeError(f"skmem-pg query failed ({label}): {tail}")
        return result

    def psql(sql, want=False):
        args = PSQL + (["-tAF\t", "-c", sql] if want else ["-c", sql])
        return run_psql(args).stdout

    def psql_stdin(sql):
        return run_psql(PSQL + ["-f", "-"], input_text=sql)

    def embed(texts):
        for _ in range(4):
            try:
                j = requests.post(
                    embed_url, json={"model": embed_model, "input": texts}, timeout=180
                ).json()
                vecs = _embeddings_from_response(j)
                if vecs is not None and len(vecs) == len(texts):
                    return vecs
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
        candidates = glob.glob(f"{mem}/{layer}/*.json")
        dot_json = os.path.join(mem, layer, ".json")
        if os.path.isfile(dot_json):
            candidates.append(dot_json)
        for fp in candidates:
            stem = os.path.splitext(os.path.basename(fp))[0]
            try:
                with open(fp, encoding="utf-8") as handle:
                    payload = json.load(handle)
                payload_memory_id(payload, stem)
            except ValueError as exc:
                quarantine_invalid_flat_file(mem, fp, reason=str(exc))
                continue
            except (OSError, json.JSONDecodeError):
                # Existing malformed-file behavior is unchanged; the backfill
                # loader skips unreadable payloads without inventing an ID.
                pass
            if stem:
                flat[stem] = fp

    # Resurrection guard (card 7d3e9fcc): a deliberately forgotten memory must
    # never be re-created by reconcile. forget() records a durable tombstone
    # (see skmemory.tombstones) that rides the same Syncthing sync as the flat
    # memories. Any tombstoned id is dropped from the flat truth here, so it is
    # never backfilled -- even when a stale flat copy has reappeared (Syncthing
    # re-deliver, a second source path, or an ingest re-import) -- and, if it is
    # somehow still in pg, it is left as an orphan and pruned out (through the
    # guarded prune below) so "forgotten" stays gone from the derived index too.
    tombstoned = load_tombstones(mem)
    resurrection_blocked = sorted(i for i in flat if i in tombstoned)
    for i in resurrection_blocked:
        del flat[i]
    if resurrection_blocked:
        log(
            f"[{agent}] resurrection-guard: refused to resurrect "
            f"{len(resurrection_blocked)} tombstoned memory(ies) with a stale flat "
            "copy present (forgotten memories stay gone)"
        )

    # Transport probe (card 9157c2c5): a cheap select 1 through the chosen
    # transport BEFORE the first counting query and long before any mutation.
    # A dead transport (docker socket permission denied, missing binary, DSN
    # unreachable) raises ReconcileTransportError here, so a failed query can
    # never be misread as "0 rows" and start an unnecessary full backfill.
    probe_transport(PSQL)

    pg_ids = set(psql(f"select id from memories where agent='{agent}';", True).split())
    missing = [i for i in flat if i not in pg_ids]
    log(
        f"[{agent}] flat={len(flat)} pg={len(pg_ids)} missing={len(missing)} "
        f"tombstoned={len(tombstoned)} resurrection_blocked={len(resurrection_blocked)}"
    )

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
            run_psql(
                PSQL + ["-c", "COPY memories_bf FROM STDIN WITH (FORMAT csv);"],
                input_text=buf.getvalue(),
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

    # 2. prune orphans (pg rows for this agent with no flat file) -- GUARDED.
    # would_prune is computed in Python from the already-agent-scoped pg_ids and
    # flat keys (same set semantics as the SQL DELETE below), so the guardrail can
    # veto the destructive DELETE before it runs.
    orphan_ids = pg_ids - set(flat.keys())
    would_prune = len(orphan_ids)
    allowed, reason = prune_guard(
        len(flat),
        len(pg_ids),
        would_prune,
        floor=floor,
        max_fraction=max_frac,
        min_sample=min_sample,
        force=force,
    )
    prune_skipped = not allowed
    prune_reason = reason
    if not allowed:
        log(f"[{agent}] PRUNE REFUSED: {reason}")
        _alert(
            f"🚨 skmem-pg reconcile [{agent}] REFUSED prune of {would_prune}/{len(pg_ids)} "
            f"rows: {reason}",
            level="crit",
            key=f"skmem-reconcile-prune-refused-{agent}",
        )
        pruned = "0"
    else:
        psql("DROP TABLE IF EXISTS flat_ids; CREATE TABLE flat_ids (id text primary key);")
        run_psql(
            PSQL + ["-c", "COPY flat_ids FROM STDIN;"],
            input_text="\n".join(flat.keys()),
        )
        pruned = psql(
            f"WITH d AS (DELETE FROM memories m WHERE m.agent='{agent}' AND NOT EXISTS "
            "(SELECT 1 FROM flat_ids f WHERE f.id=m.id) RETURNING 1) SELECT count(*) FROM d;",
            True,
        ).strip()
        psql("DROP TABLE IF EXISTS flat_ids;")
        try:
            if int(pruned) >= DEFAULT_PRUNE_ALERT_ROWS:
                _alert(
                    f"⚠️ skmem-pg reconcile [{agent}] pruned {pruned} orphan rows "
                    f"(flat={len(flat)}, pg={len(pg_ids)})",
                    level="warn",
                    key=f"skmem-reconcile-prune-large-{agent}",
                )
        except (TypeError, ValueError):
            pass

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
        "prune_skipped": prune_skipped,
        "prune_reason": prune_reason,
        "tombstoned": len(tombstoned),
        "resurrection_blocked": len(resurrection_blocked),
        "null_embedded": len(nulls),
        "embedded": embedded,
        "total": total,
    }


def reconcile_all(
    agents: list[str] | None = None,
    *,
    agents_base: str | None = None,
    verbose: bool = True,
    **kwargs,
) -> dict:
    """Reconcile every provisioned agent (or an explicit ``agents`` list).

    Each agent is reconciled independently via :func:`reconcile`. A failure for
    one agent is captured and does NOT abort the others (isolation of failure);
    it is reflected in the summary so the CLI can exit non-zero.

    Args:
        agents: explicit agent names; if None, discovered via
            :func:`discover_agents`.
        agents_base: override the agent base dir for discovery (tests).
        verbose: print per-agent + rollup lines.
        **kwargs: forwarded to :func:`reconcile` (embed_url, embed_model,
            psql_cmd, ...); ``mem_dir`` is intentionally left per-agent.

    Returns:
        dict: {
            "ok": bool,                # True iff every agent succeeded
            "succeeded": int,
            "failed": int,
            "agents": [                # one entry per agent, in run order
                {..reconcile stats.., "ok": True} |
                {"agent": name, "ok": False, "error": "<msg>"}
            ],
        }
    """
    names = agents if agents is not None else discover_agents(agents_base)
    results: list[dict] = []
    failed = 0
    for name in names:
        try:
            stats = reconcile(name, verbose=verbose, **kwargs)
            stats["ok"] = True
            results.append(stats)
        except Exception as exc:  # isolation: one agent's failure never aborts the run
            failed += 1
            if verbose:
                print(f"[{name}] FAILED: {exc}", flush=True)
            results.append({"agent": name, "ok": False, "error": str(exc)})
    summary = {
        "ok": failed == 0,
        "succeeded": len(results) - failed,
        "failed": failed,
        "agents": results,
    }
    if verbose:
        print(
            f"[reconcile-all] agents={len(results)} "
            f"succeeded={summary['succeeded']} failed={failed}",
            flush=True,
        )
    return summary


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="skmemory.reconcile",
        description="Reconcile flat JSON memories into the node-local skmem-pg.",
    )
    parser.add_argument(
        "agent",
        nargs="?",
        default=None,
        help="single agent to reconcile (default: $SKAGENT or lumina)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="reconcile every provisioned agent (those with a memory dir)",
    )
    parser.add_argument(
        "--agents",
        default=None,
        help="comma-separated explicit agent list to reconcile",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="force prune past the cold-boot/mid-sync guardrail",
    )
    args = parser.parse_args(argv)

    if args.all or args.agents:
        explicit = None
        if args.agents:
            explicit = [a.strip() for a in args.agents.split(",") if a.strip()]
        summary = reconcile_all(explicit, force_prune=args.force)
        if not summary["ok"]:
            sys.exit(1)
        return summary

    return reconcile(args.agent or default_agent(), force_prune=args.force)


if __name__ == "__main__":
    main()
