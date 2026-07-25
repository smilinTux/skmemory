"""Durable deletion markers (tombstones) for forgotten memories (card 7d3e9fcc).

Flat JSON files are skmemory's source of truth; ``reconcile.py`` rebuilds the
derived skmem-pg index (and other stores) from them, backfilling any flat memory
that is missing from pg. That backfill is a *resurrection* hazard: when a memory
is deliberately forgotten (``SKMemoryStore.forget`` deletes it from the flat
store + pgvector + AGE), a stale flat copy that reappears later -- Syncthing
re-delivering the file from a node that has not yet seen the delete, a second
source path, or an ingest re-import -- looks to reconcile like a brand-new
"missing" memory and gets re-inserted. The deliberately forgotten memory comes
back from the dead.

A tombstone is the durable record that a given memory id was forgotten and must
stay gone. It is written next to the flat memories (``<mem_dir>/tombstones/<id>.json``)
so it rides the same Syncthing sync as the memories themselves: once a memory is
forgotten on any node, the tombstone propagates and every node's reconcile
honours it. This mirrors the flat-files-are-truth model (one small append-only
file per id, no shared mutable index, Syncthing-merge-friendly).

The reconcile resurrection guard (see ``reconcile.reconcile``) loads these ids
and refuses to backfill any tombstoned id, even when a stale flat copy is
present; a tombstoned id that is somehow still in pg is treated as an orphan and
pruned out (through the existing guarded prune) so "forgotten" means gone from
the derived index too.

Tombstones are intentionally cheap and forgiving: a missing/unwritable
tombstone dir never blocks a forget (the delete still happens), and a malformed
tombstone file is skipped rather than aborting a reconcile. They are keyed by id
only, so they cost O(forgotten) files and never leak memory content.
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

TOMBSTONE_DIRNAME = "tombstones"


def tombstone_dir(mem_dir: str | os.PathLike[str]) -> Path:
    """Return the tombstones directory for a given flat-memory dir.

    ``mem_dir`` is the per-agent memory directory that holds the
    ``short-term`` / ``mid-term`` / ``long-term`` layer subdirs (the same path
    :func:`reconcile.reconcile` scans and a :class:`FileBackend` uses as its
    ``base_path``). Tombstones live in a sibling ``tombstones/`` subdir.
    """
    return Path(mem_dir) / TOMBSTONE_DIRNAME


def _tombstone_path(mem_dir: str | os.PathLike[str], memory_id: str) -> Path:
    return tombstone_dir(mem_dir) / f"{memory_id}.json"


def write_tombstone(
    mem_dir: str | os.PathLike[str],
    memory_id: str,
    *,
    agent: str | None = None,
    reason: str = "forget",
) -> Path | None:
    """Record a durable tombstone marking ``memory_id`` as deliberately forgotten.

    Best effort: returns the tombstone path on success, or ``None`` if it could
    not be written (a forget must never fail because a marker could not be
    persisted; the delete itself has already happened). Re-tombstoning an id is
    idempotent (the marker is simply rewritten).
    """
    if not memory_id:
        return None
    try:
        d = tombstone_dir(mem_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{memory_id}.json"
        payload = {
            "id": memory_id,
            "forgotten_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or "",
            "reason": reason,
        }
        if agent:
            payload["agent"] = agent
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def load_tombstones(mem_dir: str | os.PathLike[str]) -> set[str]:
    """Return the set of tombstoned (deliberately forgotten) memory ids.

    Reads ``<mem_dir>/tombstones/*.json``; the id is taken from each file's
    stem (canonical, matching how :func:`reconcile.reconcile` keys flat files),
    so an unreadable/malformed file still contributes its id and is honoured.
    Returns an empty set when no tombstone dir exists.
    """
    d = tombstone_dir(mem_dir)
    if not d.is_dir():
        return set()
    out: set[str] = set()
    for fp in glob.glob(str(d / "*.json")):
        stem = os.path.splitext(os.path.basename(fp))[0]
        if stem:
            out.add(stem)
    return out


def is_tombstoned(mem_dir: str | os.PathLike[str], memory_id: str) -> bool:
    """True if ``memory_id`` has a tombstone under ``mem_dir``."""
    return bool(memory_id) and _tombstone_path(mem_dir, memory_id).exists()
