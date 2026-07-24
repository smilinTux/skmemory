"""Rebuild-from-source invariant for the node-local skmem-pg `memories` cache.

This is the linchpin of the local-per-node model (prb-6f069c5e): skmem-pg is
LOCAL, per-node, and rebuildable from source. It is NOT streaming-replicated,
NOT a central/shared system of record, and NOT a SPOF. The `memories` table is a
DERIVED cache (same class as `index.db`) rebuilt from the Syncthing-synced flat
JSON by ``skmemory/reconcile.py`` (idempotent, agent-scoped); embeddings are a
deterministic function of flat content + mxbai on .100, so any node regenerates
them locally.

This test exercises the REAL vendored reconcile engine against the LIVE local
skmem-pg (``localhost:5432`` via ``docker exec skmem-pg``) and the real mxbai
embed endpoint. It asserts, from an EMPTY-for-this-agent pg + flat fixtures:
  (a) reconcile backfills every flat memory with a non-null embedding,
  (b) it prunes pg rows whose flat file is gone,
  (c) flat_count == pg_count per agent,
  (d) it is idempotent (2nd run = 0 backfilled / 0 pruned).

SAFETY: every row written/read/deleted here is scoped to a throwaway ``agent``
value keyed to this process's PID (``__reconcile_test_<pid>__``). reconcile is
agent-scoped at the SQL level (`WHERE agent='<agent>'`), and teardown deletes
only that agent's rows, so this test structurally cannot touch real agents'
memories (lumina, opus, jarvis, ...) even on failure paths.

Skipped automatically (module-level) when the local skmem-pg, the `docker exec`
path it reconciles through, or the mxbai embed endpoint is unreachable, so this
stays green in CI / offline runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from skmemory import reconcile as reconcile_mod

# Node-LOCAL writable DSN. Default localhost:5432 (fleet-wide uniform port); the
# retired :5433 was the abandoned standby port. Per-node override SKMEMORY_PG_DSN.
PG_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory"
)
EMBED_URL = os.environ.get(
    "EMBED_URL", os.environ.get("SKMEMORY_EMBED_URL", reconcile_mod.DEFAULT_EMBED_URL)
)
EMBED_MODEL = os.environ.get("EMBED_MODEL", reconcile_mod.DEFAULT_EMBED_MODEL)
PSQL_CMD = list(reconcile_mod.DEFAULT_PSQL)

THROWAWAY_AGENT = f"__reconcile_test_{os.getpid()}__"


def _dsn_is_local_writable() -> bool:
    """The reconcile invariant only ever runs against a LOCAL, writable pg.

    Asserts the resolved DSN targets localhost (never a remote host / .158) and a
    live primary (``pg_is_in_recovery()`` false, never the retired :5433 standby).
    """
    try:
        import psycopg

        conn = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
        try:
            host = (conn.info.host or "").lower()
            if host not in ("localhost", "127.0.0.1", "::1", "", "/var/run/postgresql"):
                return False
            with conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery();")
                in_recovery = cur.fetchone()[0]
            return not in_recovery
        finally:
            conn.close()
    except Exception:
        return False


def _docker_reconcile_path_ok() -> bool:
    try:
        r = subprocess.run(
            PSQL_CMD + ["-tAc", "select 1;"], capture_output=True, text=True, timeout=15
        )
        return r.returncode == 0 and r.stdout.strip() == "1"
    except Exception:
        return False


def _embed_reachable() -> bool:
    try:
        import requests

        j = requests.post(
            EMBED_URL, json={"model": EMBED_MODEL, "input": "reachability probe"}, timeout=10
        ).json()
        return bool(j.get("embeddings"))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_dsn_is_local_writable() and _docker_reconcile_path_ok() and _embed_reachable()),
    reason="local skmem-pg (localhost:5432), its docker-exec reconcile path, or the mxbai "
    "embed endpoint is unreachable -- skipping the reconcile rebuild-from-source invariant.",
)


def _pg_conn():
    import psycopg

    return psycopg.connect(PG_DSN, autocommit=True)


def _agent_rows():
    """Return {id: embedding_is_not_null} for the throwaway agent."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, embedding IS NOT NULL FROM memories WHERE agent = %s",
                (THROWAWAY_AGENT,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def _delete_agent_rows():
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE agent = %s", (THROWAWAY_AGENT,))
    finally:
        conn.close()


def _write_flat(mem_dir, layer, mem_id, content):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    obj = {
        "id": mem_id,
        "agent": THROWAWAY_AGENT,
        "layer": layer,
        "role": "general",
        "title": f"reconcile fixture {mem_id[:8]}",
        "content": content,
        "summary": "",
        "tags": ["reconcile-test"],
        "source": "test_reconcile_invariant",
        "created_at": "2026-07-12T00:00:00+00:00",
    }
    (d / f"{mem_id}.json").write_text(json.dumps(obj))
    return mem_id


def _run(mem_dir):
    return reconcile_mod.reconcile(
        THROWAWAY_AGENT,
        mem_dir=str(mem_dir),
        embed_url=EMBED_URL,
        embed_model=EMBED_MODEL,
        psql_cmd=PSQL_CMD,
        verbose=False,
    )


@pytest.fixture
def clean_agent():
    """Guarantee the throwaway agent starts empty and is wiped afterward."""
    _delete_agent_rows()
    yield THROWAWAY_AGENT
    _delete_agent_rows()


def test_rebuild_from_empty_backfills_embeds_prunes_and_is_idempotent(clean_agent, tmp_path):
    mem = tmp_path / "memory"

    # --- Fixtures: 3 flat memories spread across the three layers ---
    ids = [
        _write_flat(mem, "short-term", str(uuid.uuid4()), "the quantum team ships next-actions"),
        _write_flat(mem, "mid-term", str(uuid.uuid4()), "microgreens warehouse automation plan"),
        _write_flat(mem, "long-term", str(uuid.uuid4()), "sovereign infra protects the innocent"),
    ]

    # Precondition: pg has NO rows for this agent (rebuild-from-empty).
    assert _agent_rows() == {}

    # --- (a) backfill: every flat memory lands in pg with a non-null embedding ---
    stats = _run(mem)
    assert stats["flat"] == 3
    assert stats["backfilled"] == 3
    rows = _agent_rows()
    assert set(rows.keys()) == set(ids), "every flat memory id must be present in pg"
    assert all(rows.values()), "every backfilled row must have a non-null embedding"

    # --- (c) flat_count == pg_count per agent ---
    assert stats["total"] == 3
    assert len(rows) == stats["flat"] == 3

    # --- (d) idempotent: a second run with no changes backfills/prunes nothing ---
    stats2 = _run(mem)
    assert stats2["backfilled"] == 0
    assert stats2["pruned"] == 0
    rows2 = _agent_rows()
    assert set(rows2.keys()) == set(ids)
    assert all(rows2.values())

    # --- (b) prune: remove one flat file, reconcile drops exactly that pg row ---
    gone = ids[0]
    # find and unlink the file for `gone` (it lives in short-term)
    (mem / "short-term" / f"{gone}.json").unlink()
    stats3 = _run(mem)
    assert stats3["backfilled"] == 0
    assert stats3["pruned"] == 1
    rows3 = _agent_rows()
    assert gone not in rows3, "reconcile must prune the pg row whose flat file is gone"
    assert set(rows3.keys()) == set(ids[1:])
    # flat_count == pg_count still holds after prune
    assert stats3["flat"] == 2
    assert len(rows3) == 2

    # --- idempotent again after the prune ---
    stats4 = _run(mem)
    assert stats4["backfilled"] == 0
    assert stats4["pruned"] == 0
