"""One-shot repair of SUPERSEDES edges that promotion and chunking wrongly wrote.

Before card 32e2b5fa, ``AGEGraphBackend.index_memory`` turned every
``parent_id`` into a ``SUPERSEDES`` edge whose ``valid_to`` closes the parent's
validity. A promotion copy therefore marked its original as no longer current,
and every chunk "superseded" its own document.

This rewrites such an edge into ``PROMOTED_FROM`` or ``CHUNK_OF`` (deleting the
SUPERSEDES edge is what reopens the parent). The classification comes from the
child's FLAT file via :func:`skmemory.models.parent_relation_of`, never from
the graph, so an edge is only touched when the flat store says the child is a
promotion copy or chunk. A child whose relation is explicitly ``supersedes``,
or whose flat file is missing or unreadable, is left alone.

Safety: dry-run is the default, the graph is probed first (a dead graph raises
rather than reading as "no edges"), and an apply writes a JSON backup of every
edge it removes before the first change.

Why a re-sync does not undo it: ``sync_all`` skips a memory whose flat bytes
hash to the stored ``sync_hash``, and this repair does not rewrite flat files.
When a file IS re-indexed, the fixed ``index_memory`` writes PROMOTED_FROM /
CHUNK_OF and never SUPERSEDES. The one hole is a node still running the old
code: it would re-create the edge for any changed file, so deploy the fix to
every node that syncs a graph before applying this.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .backends.age_backend import PARENT_LINEAGE_EDGES, AGEGraphBackend, GraphTransportError
from .models import RELATION_SUPERSEDES, parent_relation_of

logger = logging.getLogger(__name__)

_TIERS = ("short-term", "mid-term", "long-term")


def _flat_parents(mem_dir: str | Path) -> dict[str, tuple[str | None, dict]]:
    """Map memory id -> (parent_id, metadata) over the live flat tiers."""
    out: dict[str, tuple[str | None, dict]] = {}
    base = Path(mem_dir)
    for tier in _TIERS:
        tier_dir = base / tier
        if not tier_dir.is_dir():
            continue
        for path in tier_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            mid = data.get("id") or path.stem
            meta = data.get("metadata")
            out[str(mid)] = (data.get("parent_id"), meta if isinstance(meta, dict) else {})
    return out


def repair_lineage_edges(
    backend: AGEGraphBackend,
    mem_dir: str | Path,
    *,
    dry_run: bool = True,
    backup_dir: str | Path | None = None,
) -> dict:
    """Rewrite wrongly-typed SUPERSEDES edges. Dry-run unless told otherwise.

    Returns:
        dict: ``dry_run``, ``supersedes_total``, ``to_promoted_from``,
        ``to_chunk_of``, ``kept_explicit`` (explicit supersedes),
        ``skipped_unknown`` (child or matching parent not in the flat store),
        ``rewritten``, ``backup_path`` and ``candidates`` (child/parent/edge).

    Raises:
        GraphTransportError: the graph is unreachable before any rewrite.
        A failing ROW never aborts the run: it is retried once on a fresh
        connection, then counted in ``errors`` (first few in ``error_samples``)
        and the run continues. A failed row leaves its SUPERSEDES edge, plus at
        most an extra lineage edge (merge runs before delete), so a rerun
        converges: MERGE is idempotent and only remaining edges are candidates.
    """
    backend.probe_connection()
    flat = _flat_parents(mem_dir)
    edges = backend.supersedes_edges()

    candidates: list[dict] = []
    kept = skipped = 0
    for edge in edges:
        info = flat.get(edge["child"])
        if info is None or info[0] != edge["parent"]:
            skipped += 1
            continue
        relation = parent_relation_of(info[0], info[1])
        if relation == RELATION_SUPERSEDES:
            kept += 1
            continue
        candidates.append({**edge, "to": PARENT_LINEAGE_EDGES[relation]})

    stats = {
        "agent": backend.agent,
        "dry_run": dry_run,
        "supersedes_total": len(edges),
        "to_promoted_from": sum(1 for c in candidates if c["to"] == "PROMOTED_FROM"),
        "to_chunk_of": sum(1 for c in candidates if c["to"] == "CHUNK_OF"),
        "kept_explicit": kept,
        "skipped_unknown": skipped,
        "rewritten": 0,
        "errors": 0,
        "error_samples": [],
        "backup_path": None,
        "candidates": candidates,
    }
    if dry_run or not candidates:
        return stats

    target = Path(backup_dir) if backup_dir is not None else Path(mem_dir).parent / "backups"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target / f"lineage-repair-{backend.agent}-{stamp}.json"
    path.write_text(json.dumps(candidates, indent=2, sort_keys=True, default=str) + "\n")
    stats["backup_path"] = str(path)

    for cand in candidates:
        try:
            _convert_with_retry(backend, cand)
            stats["rewritten"] += 1
        except GraphTransportError as exc:
            stats["errors"] += 1
            logger.warning(
                "lineage repair: %s -> %s failed: %s", cand["child"], cand["parent"], exc
            )
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"child": cand["child"], "parent": cand["parent"], "error": str(exc)[:300]}
                )
    return stats


def _convert_with_retry(backend, cand: dict) -> None:
    """Convert one edge; on a transport error retry once on a fresh connection."""
    try:
        backend.convert_supersedes_to_lineage(cand["child"], cand["parent"], cand["to"])
    except GraphTransportError:
        reset = getattr(backend, "reset_connection", None)
        if callable(reset):
            reset()
        backend.convert_supersedes_to_lineage(cand["child"], cand["parent"], cand["to"])
