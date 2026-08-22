"""Guarded AGE orphan reconciliation and flat/graph parity (card c25e2513).

Incident chg-a76c0aee proved the AGE ``sync_all`` backfills with MERGE but
never removes stale Memory nodes: after authoritative convergence, hundreds
of stale graph Memory nodes lingered on each node while missing
authoritative nodes went to zero. This module adds the missing other half:
a dry-run parity report, a backup-aware guarded prune, and aux-node
cleanup, mirroring the pgvector reconcile (``skmemory.reconcile``) safety
vocabulary exactly.

Safety model (same semantics as the pgvector reconcile):

* Transport first: the graph connection is probed before any report or
  mutation. A dead graph raises :class:`GraphTransportError` and is never
  misread as "zero graph nodes" (mass backfill) or "zero flat files"
  (mass prune).
* Dry-run is the default for every prune path.
* The prune guard is the pgvector ``prune_guard`` itself: absolute flat
  floor, max fraction of graph Memory nodes, minimum sample size, and an
  explicit force override (``SKMEMORY_GRAPH_RECONCILE_FORCE`` / ``--force``).
* Before any delete, a JSON backup of every doomed node (properties plus
  incident edge inventory) is written under the agent's backups dir.
* Memory nodes are removed one id at a time through the existing
  ``remove_memory`` DETACH DELETE path; no ad-hoc production Cypher.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .backends.age_backend import AUX_NODE_LABELS, AGEGraphBackend, GraphTransportError
from .invalid_records import payload_memory_id
from .reconcile import _alert, prune_guard
from .tombstones import load_tombstones

logger = logging.getLogger(__name__)

_TIERS = ("short-term", "mid-term", "long-term")

#: Guardrail defaults, mirroring the pgvector reconcile knobs.
DEFAULT_GRAPH_PRUNE_FLOOR = int(os.environ.get("SKMEMORY_GRAPH_PRUNE_FLOOR", "1"))
DEFAULT_GRAPH_MAX_PRUNE_FRACTION = float(
    os.environ.get("SKMEMORY_GRAPH_MAX_PRUNE_FRACTION", "0.20")
)
DEFAULT_GRAPH_PRUNE_MIN_SAMPLE = int(os.environ.get("SKMEMORY_GRAPH_PRUNE_MIN_SAMPLE", "20"))

#: Force override env var (same style as SKMEMORY_RECONCILE_FORCE).
GRAPH_FORCE_ENV = "SKMEMORY_GRAPH_RECONCILE_FORCE"


def flat_memory_ids(mem_dir: str | Path) -> set[str]:
    """Authoritative flat memory ids with sync_all's tier-race tolerance.

    Snapshots by file name across the three memory tiers, exactly like
    ``AGEGraphBackend.sync_all``. A file that vanishes between snapshot and
    read (Syncthing move, promotion) STAYS in the authoritative set: a
    transient race must never read as proof that the graph node is stale.
    Files whose id fails validation are excluded (sync_all quarantines
    them; they never project). Tombstoned (deliberately forgotten) ids are
    excluded so a forgotten memory's graph node reads as stale and gets
    pruned, mirroring the pgvector reconcile.

    Args:
        mem_dir: The agent's memory directory (holds the tier dirs).

    Returns:
        set[str]: The authoritative memory ids.
    """
    base = Path(mem_dir)
    snapshot: dict[str, Path] = {}
    for tier in _TIERS:
        tier_dir = base / tier
        if not tier_dir.is_dir():
            continue
        for json_file in tier_dir.glob("*.json"):
            if json_file.stem:
                snapshot[json_file.name] = json_file
    ids: set[str] = set()
    for name, original in snapshot.items():
        candidates = [original]
        candidates.extend(base / tier / name for tier in _TIERS)
        payload = None
        for candidate in dict.fromkeys(candidates):
            try:
                payload = candidate.read_text(encoding="utf-8")
                break
            except OSError:
                continue
        stem = Path(name).stem
        if payload is None:
            # Vanished from every tier mid-run: still authoritative. Pruning
            # on the strength of a race is exactly the failure this prevents.
            ids.add(stem)
            continue
        try:
            data = json.loads(payload)
            ids.add(payload_memory_id(data, stem))
        except (ValueError, json.JSONDecodeError):
            continue
    return ids - set(load_tombstones(base))


def graph_parity(backend: AGEGraphBackend, mem_dir: str | Path) -> dict:
    """Strict flat/graph parity report for one agent.

    Args:
        backend: The AGE backend (graph already named for the agent).
        mem_dir: The agent's memory directory.

    Returns:
        dict: ``flat``, ``graph``, ``matched``, ``stale_candidates``,
        ``missing`` counts plus the explicit ``stale_ids`` / ``missing_ids``
        lists.

    Raises:
        GraphTransportError: The graph could not be read. Never returns
        counts derived from a failed query.
    """
    graph_ids = backend.graph_memory_ids()
    flat_ids = flat_memory_ids(mem_dir)
    stale = sorted(graph_ids - flat_ids)
    missing = sorted(flat_ids - graph_ids)
    return {
        "flat": len(flat_ids),
        "graph": len(graph_ids),
        "matched": len(graph_ids & flat_ids),
        "stale_candidates": len(stale),
        "missing": len(missing),
        "stale_ids": stale,
        "missing_ids": missing,
    }


def _force_enabled(force: bool | None) -> bool:
    """Explicit flag wins; otherwise the env override decides."""
    if force is not None:
        return force
    return os.environ.get(GRAPH_FORCE_ENV, "").lower() in ("1", "true", "yes")


def _write_prune_backup(
    backend: AGEGraphBackend,
    stale_ids: list[str],
    backup_dir: Path,
) -> tuple[Path, int]:
    """Write the JSON backup of every doomed node plus its incident edges.

    Args:
        backend: The AGE backend (strict reads).
        stale_ids: Memory ids about to be pruned, in prune order.
        backup_dir: Directory to receive the timestamped backup file.

    Returns:
        (backup path, total incident edges recorded).

    Raises:
        GraphTransportError: A node or edge read failed mid-backup; the
        caller aborts before any delete.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nodes = []
    edges_removed = 0
    for mid in stale_ids:
        node = backend.memory_node_strict(mid)
        edges = backend.memory_edge_inventory(mid)
        nodes.append({"id": mid, "properties": node or {}, "edges": edges})
        edges_removed += len(edges)
    payload = {
        "agent": backend.agent,
        "graph": backend.graph,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
    }
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"graph-prune-{backend.agent}-{stamp}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return path, edges_removed


def reconcile_graph(
    backend: AGEGraphBackend,
    mem_dir: str | Path,
    *,
    dry_run: bool = True,
    force: bool | None = None,
    backup_dir: str | Path | None = None,
    floor: int | None = None,
    max_fraction: float | None = None,
    min_sample: int | None = None,
) -> dict:
    """Parity report plus guarded prune of stale graph Memory nodes.

    The graph connection is probed first; a dead graph raises before any
    count or delete. Dry-run (the default) reports parity and current aux
    orphan counts and changes nothing. A live prune runs the pgvector
    ``prune_guard`` (flat floor, graph fraction cap, minimum sample, force
    override), writes the JSON backup, deletes stale Memory nodes one at a
    time through ``remove_memory``, then removes zero-edge aux nodes.

    Args:
        backend: The AGE backend for the agent's graph.
        mem_dir: The agent's memory directory (authoritative flat store).
        dry_run: Report only; never delete. Default True.
        force: Force override past the guardrail. None defers to
            ``SKMEMORY_GRAPH_RECONCILE_FORCE``.
        backup_dir: Backup destination (default ``<agent home>/backups``).
        floor: Flat-count floor required before any prune.
        max_fraction: Max prunable fraction of graph Memory nodes.
        min_sample: Graph size at which the fraction cap engages.

    Returns:
        dict: Explicit counts: parity counts, ``guard_allowed``,
        ``guard_reason``, ``prune_skipped``, ``pruned``, ``edges_removed``,
        ``aux_orphans`` (dry-run preview), ``aux_removed``, ``backup_path``,
        ``stale_ids`` / ``pruned_ids``.

    Raises:
        GraphTransportError: The graph was unreachable, or a delete failed
        mid-prune (remaining deletes are aborted).
    """
    backend.probe_connection()
    report = graph_parity(backend, mem_dir)
    floor_value = DEFAULT_GRAPH_PRUNE_FLOOR if floor is None else floor
    max_frac = DEFAULT_GRAPH_MAX_PRUNE_FRACTION if max_fraction is None else max_fraction
    sample = DEFAULT_GRAPH_PRUNE_MIN_SAMPLE if min_sample is None else min_sample
    allowed, reason = prune_guard(
        report["flat"],
        report["graph"],
        report["stale_candidates"],
        floor=floor_value,
        max_fraction=max_frac,
        min_sample=sample,
        force=_force_enabled(force),
    )

    stats = {
        "agent": backend.agent,
        "graph_name": backend.graph,
        "dry_run": dry_run,
        "flat": report["flat"],
        "graph": report["graph"],
        "matched": report["matched"],
        "stale_candidates": report["stale_candidates"],
        "missing": report["missing"],
        "stale_ids": report["stale_ids"],
        "missing_ids": report["missing_ids"],
        "guard_allowed": allowed,
        "guard_reason": reason,
        "prune_skipped": not allowed,
        "pruned": 0,
        "pruned_ids": [],
        "edges_removed": 0,
        "aux_orphans": backend.count_orphaned_aux_nodes(AUX_NODE_LABELS),
        "aux_removed": {},
        "backup_path": None,
    }

    if not allowed:
        logger.warning("graph reconcile [%s] PRUNE REFUSED: %s", backend.agent, reason)
        _alert(
            f"🚨 AGE graph reconcile [{backend.agent}] REFUSED prune of "
            f"{report['stale_candidates']}/{report['graph']} stale Memory nodes: {reason}",
            level="crit",
            key=f"skmem-graph-prune-refused-{backend.agent}",
        )
        return stats
    if dry_run or not report["stale_candidates"]:
        return stats

    target_dir = Path(backup_dir) if backup_dir is not None else Path(mem_dir).parent / "backups"
    backup_path, edges_removed = _write_prune_backup(backend, report["stale_ids"], target_dir)

    pruned = 0
    for mid in report["stale_ids"]:
        if not backend.remove_memory(mid):
            raise GraphTransportError(
                f"graph prune delete failed for {mid} after {pruned} delete(s); aborting"
            )
        pruned += 1

    stats.update(
        {
            "pruned": pruned,
            "pruned_ids": report["stale_ids"],
            "edges_removed": edges_removed,
            "aux_removed": backend.delete_orphaned_aux_nodes(AUX_NODE_LABELS),
            "backup_path": str(backup_path),
        }
    )
    return stats
