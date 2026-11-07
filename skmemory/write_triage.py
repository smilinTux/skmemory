"""Write-time conflict verdict for ``MemoryStore.snapshot`` (card SKMEM-P3-02).

For a new fact, rule or preference, compare it with the strongest of the top-5
similar OPEN memories of the SAME kind the writer may see, and decide:

``unchanged``  dedupe-safe restatement: no write, the existing memory is returned.
``extends``    related and still true: write, with an EXTENDS edge to the target.
``changes``    write with ``supersedes_id`` and a SUPERSEDES edge, then close the
               old record's ``valid_to``. Only from an explicit ``supersedes_id``
               passed by the caller, or an LLM verdict that survives every guard
               (see :func:`skmemory.triage.judge_relation`). Rules never do it.
anything else  a plain write. A doubtful one is logged in
               ``metadata.possible_conflict_with`` for a human or the sweep.

Safety: a false ``changes`` closes a true fact, so every doubt resolves to
``extends``; a triage failure never loses the write; a reader never has a memory
it cannot see compared, closed or returned.

Closing the old record rewrites its flat file. ``integrity_hash`` covers id,
title, content and the emotional signature only, so ``valid_to`` does not touch
it and the record is not re-sealed; ``FileBackend.save`` re-signs a detached
signature sidecar when one is configured. The edit is recorded in
``metadata.superseded_by`` and the WAL. The new record is written FIRST and the
old one closed second, so a crash between them leaves the old record open with a
new record pointing at it: :func:`heal_supersessions` (run from the promotion
sweep) closes it. It also adopts a closure found only in a Syncthing conflict
copy, so a stale open copy cannot resurrect a closed fact.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import sealing
from .audience import resolve_reader
from .backends.file_backend import CONFLICT_MARK
from .triage import RelationVerdict, _lexical_similarity, judge_relation

if TYPE_CHECKING:
    from collections.abc import Callable

    from .models import Memory
    from .store import MemoryStore

logger = logging.getLogger(__name__)

#: Kinds triaged by default. Episodes happened and never conflict; procedures are out of scope.
TRIAGE_KINDS = frozenset({"fact", "rule", "preference"})
TOP_K = 5
_OFF = {"0", "off", "false", "no"}


@dataclass(frozen=True)
class WriteDecision:
    verdict: RelationVerdict
    target: Memory
    tier: str
    status: str
    possible_conflict: bool = False


def enabled() -> bool:
    """``SKMEMORY_WRITE_TRIAGE=off`` is the kill switch (default on)."""
    return os.environ.get("SKMEMORY_WRITE_TRIAGE", "on").strip().casefold() not in _OFF


def _open_head(store: MemoryStore, start: Memory, scope) -> Memory | None:
    """Follow ``superseded_by`` from a closed record to the open head of its chain."""
    cur = start
    for _ in range(16):
        if cur.valid_to is None:
            return cur
        nxt_id = (cur.metadata.get("superseded_by") or {}).get("id")
        nxt = store.primary.load(nxt_id) if nxt_id else None
        if nxt is None or not scope.allows(nxt.context_tag, nxt.tags):
            return cur
        cur = nxt
    return cur


def explicit(store: MemoryStore, supersedes_id: str, reader: str | None) -> WriteDecision | None:
    """Honour a caller-supplied ``supersedes_id`` (after the reader check)."""
    try:
        scope = resolve_reader(reader)
        target = store.primary.load(supersedes_id)
        if target is None or not scope.allows(target.context_tag, target.tags):
            logger.warning("explicit supersedes_id ignored: not found or not visible")
            return None
        return WriteDecision(
            "changes", _open_head(store, target, scope) or target, "explicit", "ok"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("explicit supersedes_id ignored: %s", type(exc).__name__)
        return None


def decide(
    store: MemoryStore,
    memory: Memory,
    *,
    reader: str | None = None,
    llm_call: Callable[[str], str] | None = None,
    use_llm: bool | None = None,
) -> WriteDecision | None:
    """Return the decision for ``memory``, or None for a plain write. Never raises."""
    if memory.kind not in TRIAGE_KINDS or not enabled():
        return None
    try:
        scope = resolve_reader(reader)
        query = f"{memory.title} {memory.content}"[:300]
        hits = store.search(query, limit=TOP_K * 3, reader=reader)
        pool = [
            m
            for m in hits
            if m.id != memory.id
            and m.kind == memory.kind
            and m.valid_to is None
            and CONFLICT_MARK not in m.id
            and scope.allows(m.context_tag, m.tags)
        ]
        # A record some pool member already supersedes is stale (crash window): skip it.
        stale = {m.supersedes_id for m in pool if m.supersedes_id}
        pool = [m for m in pool if m.id not in stale][:TOP_K]
        if not pool:
            return None
        target = max(pool, key=lambda m: _lexical_similarity(memory.content, m.content))
        result = judge_relation(memory.content, target.content, use_llm=use_llm, llm_call=llm_call)
        if result.verdict == "unrelated":
            return None
        return WriteDecision(
            result.verdict, target, result.tier, result.status, result.possible_conflict
        )
    except Exception as exc:  # noqa: BLE001 - triage must never lose the write
        logger.warning("write triage skipped: %s", type(exc).__name__)
        return None


def can_close(store: MemoryStore, old: Memory) -> bool:
    """False when ``old`` has a detached-signature sidecar this store cannot re-sign.

    Closing rewrites the flat file; without a fresh signature the sidecar goes
    stale (``signature_ok`` False) and a strict-verify load would drop the
    record. In that case the record is left valid, not closed.
    """
    base = getattr(store.primary, "base_path", None)
    if base is None:
        return True
    path = Path(base) / old.layer.value / f"{old.id}.json"
    if not os.path.exists(sealing.sidecar_path_for(path)):
        return True
    if not hasattr(store.primary, "_seal_config"):
        return False  # this backend's save() never re-signs
    try:
        return bool(sealing.get_sealer(store.primary._seal_config).sign(old))
    except Exception:  # noqa: BLE001
        return False


def _close(
    store: MemoryStore,
    old_id: str,
    valid_to: str,
    by_id: str,
    via: str,
    not_edited_after: str | None = None,
) -> bool:
    old = store.primary.load(old_id)
    if old is None or old.valid_to is not None:
        return False
    if not_edited_after and (old.updated_at or "") > not_edited_after:
        return False  # edited (reopened) after the superseding record: leave it valid
    if not can_close(store, old):
        logger.warning("not closing a sealed record: this store cannot re-seal its sidecar")
        return False
    sealed_ok = old.verify_integrity()
    now = datetime.now(timezone.utc).isoformat()
    old.valid_to = valid_to
    old.updated_at = now
    old.metadata = {**old.metadata, "superseded_by": {"id": by_id, "at": now, "via": via}}
    store._wal.log_pending(
        "supersede_close", old.id, old.title, old.layer.value, {"superseded_by": by_id}
    )
    store.primary.save(old)
    store._wal.log_done("supersede_close", old.id)
    if sealed_ok and not old.verify_integrity():  # pragma: no cover - hash ignores valid_to
        logger.error("closing %s broke its integrity hash", old.id)
    for backend, method in ((store.vector, "save"), (store.graph, "index_memory")):
        if backend is None:
            continue
        try:
            getattr(backend, method)(old)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reindex of closed %s failed: %s", old.id, type(exc).__name__)
    return True


def close_superseded(store: MemoryStore, old_id: str, new: Memory) -> bool:
    """Close ``old_id``'s validity at ``new.valid_from``. Idempotent, best effort."""
    return _close(store, old_id, new.valid_from or new.created_at, new.id, "write_time_triage")


def _superseding_records(store: MemoryStore) -> list[Memory]:
    """Live records carrying a ``supersedes_id``. SQLite primaries answer from the indexed
    column and load only those rows; other backends fall back to listing everything
    (O(store) per sweep, parsed one file at a time by the backend)."""
    get_conn = getattr(store.primary, "_get_conn", None)
    if callable(get_conn):
        try:
            rows = (
                get_conn()
                .execute(
                    "SELECT id FROM memories WHERE supersedes_id IS NOT NULL AND supersedes_id != ''"
                )
                .fetchall()
            )
            return [m for m in (store.primary.load(r[0]) for r in rows) if m is not None]
        except Exception as exc:  # noqa: BLE001
            logger.warning("heal: index query failed, scanning: %s", type(exc).__name__)
    return [m for m in store.primary.list_memories(limit=10**7) if m.supersedes_id]


def heal_supersessions(store: MemoryStore) -> dict:
    """Close every open record that a live record's ``supersedes_id`` points at.

    Self-heals the crash window between the new write and the old close, and adopts
    a closure that exists only in a Syncthing conflict copy, unless the canonical
    record was edited (reopened) after that copy. Oldest supersessor first. Never raises.
    """
    report = {"closed": 0, "adopted_from_conflict": 0, "errors": 0}
    try:
        for m in sorted(_superseding_records(store), key=lambda x: x.valid_from or ""):
            try:
                if _close(
                    store,
                    m.supersedes_id,
                    m.valid_from or m.created_at,
                    m.id,
                    "heal_supersessions",
                    not_edited_after=m.updated_at or m.created_at,
                ):
                    report["closed"] += 1
            except Exception as exc:  # noqa: BLE001
                report["errors"] += 1
                logger.warning("heal: close failed: %s", type(exc).__name__)
        base = getattr(store.primary, "base_path", None)
        if base is not None:
            for path in sorted(Path(base).glob(f"*/*{CONFLICT_MARK}*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    cid, vto = data.get("id"), data.get("valid_to")
                    if not (cid and vto):
                        continue
                    canonical = store.primary.load(cid)
                    if canonical is None or canonical.valid_to is not None:
                        continue
                    if (data.get("updated_at") or "") < (canonical.updated_at or ""):
                        continue  # the canonical record was edited after this copy
                    by = (data.get("metadata") or {}).get("superseded_by") or {}
                    if _close(store, cid, vto, by.get("id", "conflict-copy"), "conflict_copy"):
                        report["adopted_from_conflict"] += 1
                except Exception:  # noqa: BLE001
                    report["errors"] += 1
    except Exception as exc:  # noqa: BLE001
        report["errors"] += 1
        logger.warning("heal_supersessions failed: %s", type(exc).__name__)
    return report
