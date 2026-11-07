"""Re-embed one agent's rows whose embed-text formula changed (SKMEM-P1-07).

``reconcile`` embedded ``(content or title)[:1100]`` while ``PGVectorBackend.save``
embedded ``title + content + summary`` cut at 1400, so the rows in skmem-pg are
a mix of two formulas. :func:`skmemory.embed_text.embed_text` is now the single
formula. New and changed rows get it automatically; this tool brings the OLD
rows over, for ONE agent, on the DSN in the environment.

Safety, in order of importance:

* DRY RUN BY DEFAULT. Without ``--apply`` nothing is written and the embed
  endpoint is never called; it only counts. ``reembed(apply=False)`` has no
  write path at all.
* Writes ONLY the ``embedding`` column, and only for rows of the given agent.
* Each write is guarded by a digest of the text that was embedded, so a row that
  was saved again after it was read (and so already carries a fresher vector)
  is skipped and counted as ``stale``, never overwritten with an older vector.
* Resumable: keyset pagination on ``id`` with the last committed id saved to a
  state file after every batch. Re-running continues; ``--restart`` starts over.
* Batched and rate-limited (``--batch-size``, ``--rows-per-sec``).

Which rows: every row whose NEW text differs from the text the OLD reconcile
formula would have embedded. A row that ``save`` already embedded with the new
formula cannot be told apart from one reconcile embedded (the row does not
record its formula), so it is re-embedded to the same vector: wasted work, never
wrong. Rows whose two texts are identical are skipped.

The tool never prints row text or the DSN; it reports counts and ids only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .embed_text import embed_text, legacy_reconcile_text

DSN_ENV = "SKMEMORY_PG_DSN"
DEFAULT_BATCH = 64
DEFAULT_RATE = 20.0
STATE_VERSION = 1


class ReembedRefused(RuntimeError):
    """The run was refused (unsafe or contradictory arguments)."""


def text_digest(title, content, summary) -> str:
    """Digest of the raw fields the embedding was computed from."""
    raw = f"{title or ''}\n{content or ''}\n{summary or ''}"
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()  # noqa: S324


def needs_reembed(title, content, summary) -> bool:
    """True when the old reconcile text and the shared formula disagree."""
    return legacy_reconcile_text(content, title) != embed_text(title, content, summary)


def default_state_path(agent: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "skmemory" / f"reembed-{agent}.json"


def _load_state(path: Path, agent: str) -> dict:
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if state.get("agent") != agent or state.get("version") != STATE_VERSION:
        raise ReembedRefused(
            f"state file {path} belongs to agent {state.get('agent')!r}, not {agent!r}; "
            "pass --restart or a different --state"
        )
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def reembed(
    conn,
    agent: str,
    embed: Callable[[str], list[float]] | None,
    *,
    apply: bool = False,
    state_path: Path | None = None,
    batch_size: int = DEFAULT_BATCH,
    rows_per_sec: float = DEFAULT_RATE,
    limit: int | None = None,
    restart: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Walk the agent's rows in id order and (with ``apply``) re-embed stale ones.

    ``conn`` is an open psycopg connection (autocommit) with the vector type
    registered. ``embed`` maps text to a vector and is only called under
    ``apply``. Returns a count report.
    """
    if not agent:
        raise ReembedRefused("an explicit agent is required (there is no default)")
    if batch_size < 1:
        raise ReembedRefused("batch size must be at least 1")
    if apply and embed is None:
        raise ReembedRefused("--apply needs an embedder")
    state: dict = {}
    if state_path is not None and not restart:
        state = _load_state(state_path, agent)
    last_id = state.get("last_id") or ""
    counts = {
        "scanned": int(state.get("scanned", 0)),
        "stale_formula": int(state.get("stale_formula", 0)),
        "unchanged_formula": int(state.get("unchanged_formula", 0)),
        "updated": int(state.get("updated", 0)),
        "skipped_changed_since_read": int(state.get("skipped_changed_since_read", 0)),
        "failed": int(state.get("failed", 0)),
    }
    failed_ids: list[str] = list(state.get("failed_ids", []))
    this_run = 0

    def process(mid, title, content, summary) -> int:
        """Embed and write one row. Returns 1 if a vector was written."""
        try:
            vec = embed(embed_text(title, content, summary) or " ")
            with conn.cursor() as cur:
                # Only the embedding column, only this agent, only if the text
                # we embedded is still the row's text.
                cur.execute(
                    "UPDATE memories SET embedding = %s::vector "
                    "WHERE id = %s AND agent = %s AND md5("
                    "coalesce(title,'') || E'\\n' || coalesce(content,'') || E'\\n' "
                    "|| coalesce(summary,'')) = %s",
                    (vec, mid, agent, text_digest(title, content, summary)),
                )
                if cur.rowcount:
                    counts["updated"] += 1
                    wrote = 1
                else:
                    counts["skipped_changed_since_read"] += 1
                    wrote = 0
            if mid in failed_ids:
                failed_ids.remove(mid)
            return wrote
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
            if mid not in failed_ids:
                failed_ids.append(mid)
            print(f"reembed: {mid} failed: {type(exc).__name__}", file=sys.stderr)
            return 0

    def checkpoint() -> None:
        counts["failed"] = len(failed_ids)
        if state_path is not None and apply:
            _save_state(
                state_path,
                {
                    "version": STATE_VERSION,
                    "agent": agent,
                    "last_id": last_id,
                    "failed_ids": failed_ids,
                    **counts,
                },
            )

    # Rows that failed in an earlier run are retried FIRST: the keyset cursor has
    # already moved past them, so without this a transient embed error would skip
    # a row permanently.
    if apply and failed_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, content, summary FROM memories "
                "WHERE agent = %s AND id = ANY(%s) ORDER BY id",
                (agent, list(failed_ids)),
            )
            retry = cur.fetchall()
        found = {r[0] for r in retry}
        for gone in [i for i in failed_ids if i not in found]:
            failed_ids.remove(gone)  # row deleted since: nothing left to retry
        for mid, title, content, summary in retry:
            if needs_reembed(title, content, summary):
                process(mid, title, content, summary)
            else:
                failed_ids.remove(mid)
        checkpoint()

    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, content, summary FROM memories "
                "WHERE agent = %s AND id > %s ORDER BY id LIMIT %s",
                (agent, last_id, batch_size),
            )
            rows = cur.fetchall()
        if not rows:
            break
        started = time.monotonic()
        wrote = 0
        for mid, title, content, summary in rows:
            if limit is not None and this_run >= limit:
                break
            this_run += 1
            counts["scanned"] += 1
            last_id = mid
            if not needs_reembed(title, content, summary):
                counts["unchanged_formula"] += 1
                continue
            counts["stale_formula"] += 1
            if not apply:
                continue
            wrote += process(mid, title, content, summary)
        checkpoint()
        if limit is not None and this_run >= limit:
            break
        if apply and wrote and rows_per_sec > 0:
            budget = wrote / rows_per_sec
            spent = time.monotonic() - started
            if budget > spent:
                sleep(budget - spent)
    counts["failed"] = len(failed_ids)
    return {
        "agent": agent,
        "applied": apply,
        "last_id": last_id or None,
        "failed_ids": list(failed_ids),
        **counts,
    }


def build_embedder(agent: str) -> Callable[[str], list[float]]:
    """The production embedder: endpoint failover, dimension and model pinning."""
    from .backends.pgvector_backend import PGVectorBackend

    backend = PGVectorBackend(dsn=os.environ[DSN_ENV], agent=agent)
    return lambda text: backend._embed(text, required=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Re-embed one agent's rows whose embed-text formula changed. "
        "Dry run unless --apply."
    )
    ap.add_argument("--agent", required=True, help="memory owner (no default, by design)")
    ap.add_argument("--apply", action="store_true", help="actually write embeddings")
    ap.add_argument("--state", type=Path, help="resume file (default under XDG state dir)")
    ap.add_argument("--restart", action="store_true", help="ignore the resume file")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--rows-per-sec", type=float, default=DEFAULT_RATE, help="0 disables")
    ap.add_argument("--limit", type=int, help="stop after this many rows this run")
    args = ap.parse_args(argv)

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        print(f"refused: {DSN_ENV} is not set", file=sys.stderr)
        return 2
    try:
        import psycopg
        from pgvector.psycopg import register_vector
    except ImportError:
        print("refused: psycopg and pgvector are required", file=sys.stderr)
        return 2
    state_path = args.state or default_state_path(args.agent)
    embed = None
    if args.apply:
        from .agents import require_memory_profile

        require_memory_profile(args.agent)  # a typo'd agent must not reach the DB
        embed = build_embedder(args.agent)
    else:
        print("DRY RUN: nothing is written and no embed call is made. Pass --apply to write.")
    with psycopg.connect(dsn, autocommit=True) as conn:
        register_vector(conn)
        try:
            report = reembed(
                conn,
                args.agent,
                embed,
                apply=args.apply,
                state_path=state_path,
                batch_size=args.batch_size,
                rows_per_sec=args.rows_per_sec,
                limit=args.limit,
                restart=args.restart,
            )
        except ReembedRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
