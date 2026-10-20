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

Stub guard (card: skmemory sqlite-only-orphans, 2026-10-16): a flat file that is
itself a rescued SQLite-preview stub (content truncated to ~150 chars,
`metadata.recovered_from_sqlite_preview = True`, see `skmemory.stub_markers`)
must never be backfilled into skmem-pg as if it were real content -- a
reconcile run did exactly that to 373 ids during the 2026-10-16 recovery,
before this guard existed anywhere in the write path. By default such ids are
skipped (loudly) rather than inserted; `force_stubs=True` / `--force-stubs`
overrides for an operator who has verified this is correct.

Shared-id convergence fix: `memories_pkey` is a bare `id` primary key, not
`(id, agent)`. When two agents' flat stores both hold a file for the same id
(shared "Seed: emotion-*" records arrive in several agents' dirs via
Syncthing), whichever agent reconciles first claims the row and every other
agent's reconcile counted it "missing" forever -- re-embedding the same ids
every night with the count never reaching zero, training the operator to
ignore a nonzero `missing`. `missing` therefore means "absent from the table
for ANY agent"; an id present under another agent is reported separately as
`shared` and never retried.

This module was consolidated (card: skmem_reconcile.py was four independently
-evolved copies, see docs/postmortems/2026-10-16-sqlite-only-orphans.md section
3.12) from this engine plus three standalone cron scripts
(`~/skmem-build/skmem_reconcile.py`/`scripts/skmem_reconcile.py`/
`deploy/skmem-pg/skmem_reconcile.py`), each of which is now a thin shim that
execs this module.

Usage:
    python -m skmemory.reconcile [AGENT] [--force] [--dry-run] [--force-stubs]
                                                   # default: $SKAGENT or lumina
    python -m skmemory.reconcile --all [--force]     # every provisioned agent
    python -m skmemory.reconcile --agents a,b,c      # explicit list
    python -m skmemory.reconcile lumina --dry-run    # report only, no writes

Env:
    SKMEMORY_EMBED_URLS  comma-separated endpoints, tried in order
    SKMEMORY_EMBED_URL   single endpoint (used when _URLS is unset)
    EMBED_URL            legacy alias for the single endpoint
                         (default http://localhost:11434/api/embed)
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
import hashlib
import io
import json
import os
import re
import subprocess
import sys

import requests

from skmemory.invalid_records import payload_memory_id, quarantine_invalid_flat_file
from skmemory.stub_markers import is_stub_payload
from skmemory.tombstones import load_tombstones

LAYERS = ["short-term", "mid-term", "long-term"]


def _resolve_embed_urls() -> list[str]:
    """Embed endpoints for the reconcile path, in failover order.

    This module used to read a bare ``EMBED_URL`` and default to a hardcoded
    ``http://192.168.0.100:11434/api/embed``, which is BOTH a different knob
    from the one the rest of the package documents (``SKMEMORY_EMBED_URL`` /
    ``SKMEMORY_EMBED_URLS``) and, since that host went down, an endpoint that
    cannot answer. A node could configure the documented variables correctly,
    see them reflected in `skmemory health`, and still have every reconcile
    embed call go to a dead address.

    It also had no failover at all: one URL, four retries against the same
    URL, then give up. The read path has walked a list since the endpoint
    failover work; the write path silently did not.

    Precedence, most specific first. ``EMBED_URL`` is kept as a legacy alias so
    existing unit files and cron entries that set it keep working.
    """
    raw_list = (os.environ.get("SKMEMORY_EMBED_URLS") or "").strip()
    if raw_list:
        urls = [u.strip() for u in raw_list.split(",") if u.strip()]
        if urls:
            return list(dict.fromkeys(urls))
    for var in ("SKMEMORY_EMBED_URL", "EMBED_URL"):
        single = (os.environ.get(var) or "").strip()
        if single:
            return [single]
    return ["http://localhost:11434/api/embed"]


DEFAULT_EMBED_URLS = _resolve_embed_urls()
#: First endpoint, kept for callers and log lines that want a single name.
DEFAULT_EMBED_URL = DEFAULT_EMBED_URLS[0]
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
# Node-LOCAL psql. No host param: acts only on the box it runs on.
DEFAULT_PSQL = ["docker", "exec", "-i", "skmem-pg", "psql", "-U", "postgres", "-d", "skmemory"]


def _scratch_table_name(agent: str) -> str:
    """Per-run name for the backfill scratch table.

    skmemory-sync@lumina/@jarvis/@opus are timer units that fire in the same
    second against the same skmem-pg database. A fixed `memories_bf` meant one
    run's closing DROP pulled the table out from under another run's COPY
    ("relation memories_bf does not exist"), failing two of every three
    scheduled syncs. Scope the name to the agent AND the process.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (agent or "").lower()).strip("_") or "agent"
    return f"memories_bf_{slug}_{os.getpid()}"[:63]


def pg_text_array(values) -> str:
    """Render a Python iterable as a Postgres text[] array LITERAL.

    Inside a double-quoted array element Postgres treats BOTH `"` and `\\` as
    escape-requiring. The old inline version escaped only the quote, so a tag
    containing a backslash produced a literal Postgres could not parse. A tag
    ENDING in a backslash was fatal: the trailing `\\` escaped the closing
    quote, the element never terminated, and the statement died with
    "malformed array literal ... DETAIL: Unexpected end of input."

    That is not a theoretical input. Session-digest tags embed a section
    heading verbatim (`section:<heading>`), and headings contain quotes and
    backslashes. Measured 2026-10-20: 200 of 24,429 staged lumina rows carried
    a backslash in their tags.

    The cost was the whole batch. The backfill COPYs every row into a scratch
    table and then does ONE `INSERT ... SELECT`, so a single unparseable tag
    among 24,429 rows aborted the entire statement and threw away 40 minutes of
    embedding. Escaping the backslash first is the fix; the all-or-nothing
    shape of the insert is a separate problem.

    Order matters: backslashes are doubled BEFORE quotes are escaped, or the
    backslash this function adds would itself be doubled.
    """
    parts = []
    for value in values or []:
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{text}"')
    return "{" + ",".join(parts) + "}"


def _scratch_pid(table: str) -> int | None:
    """The PID embedded in a scratch table name, if it has one."""
    match = re.fullmatch(r"memories_bf_[A-Za-z0-9_]+_(\d+)", table)
    return int(match.group(1)) if match else None


def _pid_alive(pid: int) -> bool:
    """Whether a PID is still running on THIS node.

    skmem-pg is node-local (per-node container, prb-6f069c5e), so the PIDs in
    scratch table names are this host's PIDs and /proc is the right authority.
    """
    return os.path.isdir(f"/proc/{pid}")


def sweep_stale_scratch_tables(psql_exec, log=lambda _m: None) -> int:
    """Drop backfill scratch tables left behind by runs that are gone.

    The scratch name is scoped to the agent AND the process to stop concurrent
    timer units dropping each other's table mid-COPY (see
    :func:`_scratch_table_name`). That fixed the collision and created a leak:
    the closing DROP only runs on the success path, so any run that is killed
    (SIGKILL, a stalled sync stopped by hand, OOM, a reboot) leaves its table
    behind FOREVER, named after a PID that will never come back.

    A ``finally`` cannot fix this on its own, because SIGKILL runs no Python.
    So each run sweeps the corpses of previous ones.

    Measured on one node 2026-10-20: 136 orphan tables holding 293 MB, 53 of
    them for a single agent, in a database of 2.4 GB. Dropping the 134 whose
    PID was dead reclaimed 263 MB.

    A PID that has been REUSED by an unrelated process just means its table
    survives another cycle. That is the safe direction to be wrong in.

    Returns the number of tables dropped.
    """
    try:
        listing = psql_exec(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='public' and c.relkind='r' "
            "and c.relname like 'memories\\_bf\\_%';",
            True,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"scratch sweep skipped (could not list): {exc}")
        return 0

    mine = os.getpid()
    dropped = 0
    for raw in (listing or "").split():
        table = raw.strip()
        pid = _scratch_pid(table)
        if pid is None or pid == mine or _pid_alive(pid):
            continue
        try:
            psql_exec(f'DROP TABLE IF EXISTS public."{table}";')
            dropped += 1
        except Exception as exc:  # noqa: BLE001
            log(f"could not drop stale scratch table {table}: {exc}")
    if dropped:
        log(f"swept {dropped} stale backfill scratch table(s) from dead runs")
    return dropped


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


#: psql prefixes the line that actually says what went wrong.
_PG_DIAGNOSTIC_PREFIXES = ("ERROR:", "FATAL:", "PANIC:", "DETAIL:", "HINT:")


def _stderr_tail(stderr: str) -> str:
    """The scrubbed, most INFORMATIVE stderr line, for compact messages.

    This used to return the last line, which for the most common failure is
    worse than useless. psql reports a syntax error as three lines:

        ERROR:  syntax error at or near "foo"
        LINE 1: select foo
                       ^

    so the last line is the caret, and the diagnostic that names the problem
    was thrown away. Twice on 2026-10-20 a reconcile failure surfaced to the
    log as the single character "^" with no other information anywhere, which
    is indistinguishable from a tool that failed for no reason.

    Prefers the first line carrying a Postgres diagnostic prefix. Failing
    that, the last line with actual content, skipping caret-pointer and
    LINE-echo noise. Failing that, the last line, so this can never return
    less than it used to.
    """
    lines = [ln.strip() for ln in _scrub_stderr(stderr.strip()).splitlines() if ln.strip()]
    if not lines:
        return ""
    for line in lines:
        if line.startswith(_PG_DIAGNOSTIC_PREFIXES):
            return line
    for line in reversed(lines):
        if set(line) <= {"^", " "} or line.startswith("LINE "):
            continue
        return line
    return lines[-1]


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


def default_agent() -> str | None:
    """Return the validated active memory profile without a broad default."""
    from .agents import get_active_agent

    return get_active_agent()


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
    """Discover valid registered human and service memory owners."""
    from .profile_registry import resolve_memory_profile

    base = agents_base or _agents_base_dir()
    if not os.path.isdir(base):
        return []
    root = os.path.dirname(base)
    out = []
    for name in sorted(os.listdir(base)):
        if name.endswith("-template"):
            continue
        profile = resolve_memory_profile(root, name, agents_base=base)
        if profile.healthy and os.path.isdir(os.path.join(base, name, "memory")):
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
    force_stubs: bool = False,
    refresh: bool = False,
    dry_run: bool = False,
) -> dict:
    """Reconcile one agent's flat JSON memories into the node-local skmem-pg.

    Args:
        force_stubs: backfill a flat file carrying the lossy
            recovered_from_sqlite_preview stub marker anyway. Default False
            (refuse, loudly) -- see the stub guard in the module docstring.
        refresh: also UPDATE pg rows whose flat file content has CHANGED
            since the row was written. Default False, but the divergence is
            DETECTED and reported either way (``stale``), because the backfill
            is `ON CONFLICT (id) DO NOTHING`: a flat file that is corrected
            after its id is already in pg stays stale in the derived cache
            forever, and nothing noticed. `drift_check` compares id SETS only,
            so every count reads clean while title/content/tags/source and the
            embedding are all wrong. Detection is free and always on; the
            overwrite is opt-in because a flat file is not unconditionally
            better than the row it would replace. Stub-flagged flat files are
            NEVER used to refresh, regardless of this flag, so a lossy
            recovery preview can never overwrite fuller content already in pg.
        dry_run: compute and report every count (flat/pg/missing/shared/
            stub_skipped/stale/would-prune/would-embed) WITHOUT issuing any
            mutating SQL and WITHOUT calling the embed endpoint. Safe to run
            against production data.

    Returns a stats dict:
        {agent, flat, pg, missing, shared, stub_skipped, backfilled, stale,
         stale_ids, refreshed, pruned, null_embedded, embedded, total, dry_run}
    where ``flat`` is the count of flat memory files, ``pg`` the pre-run pg row
    count for this agent, ``missing`` ids absent from the table for any agent,
    ``shared`` ids already owned by a different agent (never retried, see the
    shared-id fix in the module docstring), ``embedded``/``total`` the
    post-run embedded/row counts. On a clean idempotent second run
    ``backfilled == 0`` and ``pruned == 0``. When ``dry_run`` is True, every
    count reflects what WOULD happen; nothing is written.
    """
    from .agents import require_memory_profile

    agent = require_memory_profile(agent).profile_id
    mem = mem_dir or _mem_dir(agent)
    # An explicitly passed single URL wins and disables failover, so a caller
    # that names one endpoint gets exactly that endpoint.
    embed_urls = [embed_url] if embed_url else list(DEFAULT_EMBED_URLS)
    embed_url = embed_urls[0]
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
            # Walk every endpoint before shrinking the input. Retrying the same
            # dead address four times is not failover, which is what this did.
            for url in embed_urls:
                try:
                    j = requests.post(
                        url, json={"model": embed_model, "input": texts}, timeout=180
                    ).json()
                    vecs = _embeddings_from_response(j)
                    if vecs is not None and len(vecs) == len(texts):
                        return vecs
                except Exception:
                    continue
            if len(texts) > 1:
                m = len(texts) // 2
                return embed(texts[:m]) + embed(texts[m:])
            texts = [texts[0][: max(200, len(texts[0]) // 2)]]
        raise RuntimeError(f"embed failed against every endpoint: {', '.join(embed_urls)}")

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

    # Clean up after runs that were killed before they could drop their own
    # scratch table. Cheap, idempotent, and the only thing that ever will.
    if not dry_run:
        sweep_stale_scratch_tables(psql, log)

    pg_ids = set(psql(f"select id from memories where agent='{agent}';", True).split())

    # Shared-id convergence fix: memories_pkey is a bare `id`, not (id, agent).
    # An id owned by a DIFFERENT agent can never be claimed by this one, so it
    # must not inflate `missing` (which would otherwise never reach zero and
    # re-embed the same ids every run, see module docstring). `all_ids` is
    # read once and reused for both classifications below.
    all_ids = set(psql("select id from memories;", True).split())
    shared_ids = [i for i in flat if i not in pg_ids and i in all_ids]
    missing = [i for i in flat if i not in all_ids]
    log(
        f"[{agent}] flat={len(flat)} pg={len(pg_ids)} missing={len(missing)} "
        f"shared={len(shared_ids)} tombstoned={len(tombstoned)} "
        f"resurrection_blocked={len(resurrection_blocked)}"
    )
    if shared_ids:
        try:
            literals = ",".join("'" + i.replace("'", "''") + "'" for i in shared_ids)
            owners = psql(
                f"select agent||':'||count(*) from memories where id in ({literals}) "
                "group by agent;",
                True,
            ).split()
            log(
                f"[{agent}] shared ids already owned by -> {' '.join(owners)} "
                "(single-column PK cannot represent per-agent ownership; not re-embedding)"
            )
        except Exception:
            pass

    # 1. backfill missing (embed + upsert). Stub-flagged flat files are
    # refused unless force_stubs=True (see the stub guard in the module
    # docstring). dry_run computes the same pairs/counts but issues no
    # mutating SQL and never calls the embed endpoint.
    loaded = 0
    #: Rows actually INSERTed and committed, as opposed to merely staged.
    #: Before per-batch commits these were the same number right up until the
    #: final statement failed and `loaded` became a count of work thrown away.
    committed = 0
    stub_skipped_ids: list[str] = []
    if missing:
        if not dry_run:
            bf = _scratch_table_name(agent)
            psql(
                f"DROP TABLE IF EXISTS {bf}; CREATE TABLE {bf} (id text,layer text,"
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
                        if not force_stubs and is_stub_payload(
                            o.get("content"), o.get("metadata")
                        ):
                            stub_skipped_ids.append(mid)
                            continue
                        pairs.append((mid, o))
                except Exception:
                    pass
            if not pairs:
                continue
            if dry_run:
                loaded += len(pairs)
                continue
            embs = embed([(o.get("content") or o.get("title") or " ")[:1100] for _, o in pairs])
            buf = io.StringIO()
            w = csv.writer(buf)
            for (mid, o), e in zip(pairs, embs, strict=False):
                tags = pg_text_array(o.get("tags"))
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
                PSQL + ["-c", f"COPY {bf} FROM STDIN WITH (FORMAT csv);"],
                input_text=buf.getvalue(),
            )
            loaded += len(pairs)
            # COMMIT THIS BATCH NOW, then clear the staging table.
            #
            # This used to accumulate every row in the scratch table and do ONE
            # INSERT at the very end, so the whole run was all-or-nothing. On
            # 2026-10-20 a lumina backfill embedded 24,429 rows over 40 minutes
            # and then died on the final statement because ONE tag out of
            # 24,429 carried an unescaped backslash. Nothing landed. The rows
            # were only recoverable because the scratch table happened to
            # outlive the failure and could be re-inserted by hand.
            #
            # Per batch, the same failure costs one batch. Everything already
            # committed stays committed, and the next run sees a smaller
            # `missing` list, so the work resumes instead of restarting. That
            # matters most for exactly the case that is slowest to redo: the
            # embedding, which is the expensive part and is already spent by
            # the time the insert runs.
            psql_stdin(
                "INSERT INTO memories (id,layer,role,title,content,summary,tags,source,"
                "created_at,updated_at,memory_json,agent,embedding) SELECT id,layer,role,title,"
                "content,summary,tags::text[],source,created_at::timestamptz,"
                "COALESCE(NULLIF(updated_at,'')::timestamptz,created_at::timestamptz),"
                f"memory_json::jsonb,agent,embedding::vector FROM {bf} ON CONFLICT (id) DO NOTHING;"
            )
            psql(f"TRUNCATE {bf};")
            committed += len(pairs)
        if not dry_run:
            psql(f"DROP TABLE IF EXISTS {bf};")

    if stub_skipped_ids:
        preview = ",".join(stub_skipped_ids[:20])
        if len(stub_skipped_ids) > 20:
            preview += "..."
        log(
            f"[{agent}] STUB GUARD: refused {len(stub_skipped_ids)} stub-flagged flat "
            "file(s) for pg backfill (recovered_from_sqlite_preview marker present) -- "
            "not inserted. Pass force_stubs=True only after verifying this is genuinely "
            f"the content to keep: {preview}"
        )

    # 1b. DETECT (and optionally repair) content divergence.
    #
    # The backfill above is `ON CONFLICT (id) DO NOTHING`, so it only ever
    # inserts ids that are absent. An id present in BOTH flat and pg is never
    # revisited, which means a flat file corrected after the fact (repair.py
    # backfilling a title, an operator fixing content by hand, a Syncthing
    # update from another node) leaves pg permanently stale. `drift_check`
    # compares id SETS only, so sqlite_only/flat_only/pg_only all read zero
    # while title, content, summary, tags, source and the EMBEDDING are wrong.
    # The embedding is the worst of it: computed from the old text and not
    # NULL, so the null-embedding pass below never touches it either.
    #
    # Found 2026-10-20 after repairing 35 rows by hand: the repair fixed the
    # flat files, every drift count stayed 0, and pg kept serving the old
    # content. Detection is always on. The UPDATE is opt-in, because a flat
    # file is not unconditionally better than the row it would replace.
    present_ids = [i for i in flat if i in pg_ids]
    stale_ids: list[str] = []
    refreshed = 0
    if present_ids:
        pg_hashes: dict[str, str] = {}
        try:
            CH = 400
            for i in range(0, len(present_ids), CH):
                chunk = present_ids[i : i + CH]
                literals = ",".join("'" + c.replace("'", "''") + "'" for c in chunk)
                # TITLE as well as content, and a third column saying whether
                # the row is structurally broken.
                #
                # The first version hashed content ALONE, which missed the very
                # case that motivated this phase: `repair.py` backfills a
                # missing TITLE and leaves content untouched, so the hashes
                # matched and the row read as fresh. Found on a second node
                # where 10 ava rows had no title in pg, a title in the flat
                # file, and byte-identical content. The detector said STALE=0
                # while health said 10 unparseable.
                #
                # `broken` is belt to that brace: a row whose memory_json is
                # missing a required key is stale by definition, whatever the
                # hashes say, as long as its flat file loads.
                rows = psql(
                    "select id||' '||md5(coalesce(title,'')||chr(31)||coalesce(content,''))"
                    "||' '||(case when (memory_json ? 'title' and memory_json ? 'content') "
                    "then 'ok' else 'broken' end) from memories "
                    f"where agent='{agent}' and id in ({literals});",
                    True,
                ).split("\n")
                for row in rows:
                    parts = row.split()
                    if len(parts) == 3:
                        pg_hashes[parts[0]] = (parts[1], parts[2] == "broken")
        except Exception as e:
            # Undetectable, not "no divergence". Say so rather than report 0.
            log(f"[{agent}] stale-content check FAILED (not proof of none): {e}")
            pg_hashes = {}

        if pg_hashes:
            stub_blocked_refresh = 0
            for mid in present_ids:
                want = pg_hashes.get(mid)
                if want is None:
                    continue
                try:
                    with open(flat[mid]) as fh:
                        o = json.load(fh)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                want_hash, pg_broken = want
                payload = (o.get("title") or "") + "\x1f" + (o.get("content") or "")
                fresh = hashlib.md5(payload.encode("utf-8")).hexdigest() == want_hash
                # A structurally broken pg row is stale no matter what the
                # hashes say, provided the flat file is actually loadable.
                flat_loadable = bool(o.get("title")) and bool(o.get("content"))
                if fresh and not (pg_broken and flat_loadable):
                    continue
                # A lossy recovery preview must never overwrite a fuller row.
                if is_stub_payload(o.get("content"), o.get("metadata")):
                    stub_blocked_refresh += 1
                    continue
                stale_ids.append(mid)

            if stale_ids or stub_blocked_refresh:
                preview = ",".join(stale_ids[:10]) + ("..." if len(stale_ids) > 10 else "")
                log(
                    f"[{agent}] STALE pg rows (flat content changed since the row "
                    f"was written): {len(stale_ids)}"
                    + (f" [{preview}]" if preview else "")
                    + (
                        f"; {stub_blocked_refresh} further mismatch(es) refused as "
                        "stub-flagged (a recovery preview must not overwrite pg)"
                        if stub_blocked_refresh
                        else ""
                    )
                    + ("" if refresh else "; pass refresh=True to UPDATE them")
                )

    if stale_ids and refresh and not dry_run:
        rf = _scratch_table_name(agent) + "_rf"
        psql(
            f"DROP TABLE IF EXISTS {rf}; CREATE TABLE {rf} (id text,layer text,"
            "role text,title text,content text,summary text,tags text,source text,"
            "created_at text,updated_at text,memory_json text,agent text,embedding text);"
        )
        B = 24
        for i in range(0, len(stale_ids), B):
            pairs = []
            for mid in stale_ids[i : i + B]:
                try:
                    with open(flat[mid]) as fh:
                        o = json.load(fh)
                    if isinstance(o, dict) and (o.get("content") or o.get("title")):
                        pairs.append((mid, o))
                except Exception:
                    pass
            if not pairs:
                continue
            # Content changed, so the embedding MUST be recomputed. This is the
            # part a null-embedding backfill can never do: the old vector is
            # present and wrong.
            embs = embed([(o.get("content") or o.get("title") or " ")[:1100] for _, o in pairs])
            buf = io.StringIO()
            w = csv.writer(buf)
            for (mid, o), e in zip(pairs, embs, strict=False):
                tags = pg_text_array(o.get("tags"))
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
                PSQL + ["-c", f"COPY {rf} FROM STDIN WITH (FORMAT csv);"],
                input_text=buf.getvalue(),
            )
            refreshed += len(pairs)
        # DO UPDATE, not DO NOTHING. The whole point of this phase.
        psql_stdin(
            "INSERT INTO memories (id,layer,role,title,content,summary,tags,source,"
            "created_at,updated_at,memory_json,agent,embedding) SELECT id,layer,role,title,"
            "content,summary,tags::text[],source,created_at::timestamptz,"
            "COALESCE(NULLIF(updated_at,'')::timestamptz,created_at::timestamptz),"
            f"memory_json::jsonb,agent,embedding::vector FROM {rf} "
            "ON CONFLICT (id) DO UPDATE SET layer=EXCLUDED.layer, role=EXCLUDED.role, "
            "title=EXCLUDED.title, content=EXCLUDED.content, summary=EXCLUDED.summary, "
            "tags=EXCLUDED.tags, source=EXCLUDED.source, updated_at=EXCLUDED.updated_at, "
            "memory_json=EXCLUDED.memory_json, embedding=EXCLUDED.embedding;"
        )
        psql(f"DROP TABLE IF EXISTS {rf};")
        log(f"[{agent}] refreshed {refreshed} stale pg row(s) from flat")

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
    elif dry_run:
        log(f"[{agent}] DRY RUN: would prune ({reason})")
        pruned = str(would_prune)
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

    # 3. embed any null-vector rows (dry_run: report the count, update nothing)
    nulls = [
        r.split("\t", 1)
        for r in psql(
            "select id, left(regexp_replace(coalesce(content,title,' '),E'[\\n\\r\\t]',' ','g'),"
            f"1100) from memories where embedding is null and agent='{agent}';",
            True,
        ).splitlines()
        if "\t" in r
    ]
    if not dry_run:
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
        f"[{agent}] backfilled={loaded} stub_skipped={len(stub_skipped_ids)} pruned={pruned} "
        f"null_embedded={len(nulls)} embedded={emb_stat} dry_run={dry_run}"
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
        "shared": len(shared_ids),
        "stub_skipped": len(stub_skipped_ids),
        "backfilled": loaded,
        "committed": committed,
        "stale": len(stale_ids),
        "stale_ids": stale_ids[:50],
        "refreshed": refreshed,
        "pruned": pruned_n,
        "prune_skipped": prune_skipped,
        "prune_reason": prune_reason,
        "tombstoned": len(tombstoned),
        "resurrection_blocked": len(resurrection_blocked),
        "null_embedded": len(nulls),
        "embedded": embedded,
        "total": total,
        "dry_run": dry_run,
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report every count, issue no mutating SQL, call no embed endpoint",
    )
    parser.add_argument(
        "--force-stubs",
        action="store_true",
        help="backfill stub-flagged (recovered_from_sqlite_preview) content anyway",
    )
    args = parser.parse_args(argv)

    if args.all or args.agents:
        explicit = None
        if args.agents:
            explicit = [a.strip() for a in args.agents.split(",") if a.strip()]
        summary = reconcile_all(
            explicit,
            force_prune=args.force,
            dry_run=args.dry_run,
            force_stubs=args.force_stubs,
        )
        if not summary["ok"]:
            sys.exit(1)
        return summary

    return reconcile(
        args.agent or default_agent(),
        force_prune=args.force,
        dry_run=args.dry_run,
        force_stubs=args.force_stubs,
    )


if __name__ == "__main__":
    main()
