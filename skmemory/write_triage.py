"""Write-time conflict verdict for ``MemoryStore.snapshot`` (card SKMEM-P3-02).

For a new fact, rule or preference, compare it with the top-5 similar memories of
the SAME kind that the writer is allowed to see, and decide:

``unchanged``  an agreed restatement: no write, the existing memory is returned.
``extends``    related and still true: write, with an EXTENDS edge to the target.
``changes``    the new fact makes the old one false: write with ``supersedes_id``
               and a SUPERSEDES edge, then close the old record's ``valid_to``.
anything else  a plain write.

Safety rules, in priority order:

* A false ``changes`` closes a true fact, so every doubt resolves to ``extends``
  or a plain write (see :func:`skmemory.triage.judge_relation`).
* A write must never be lost: any failure here returns "no decision" and the
  caller writes normally.
* The reader is respected: candidates come from ``store.search(reader=...)`` and
  are re-checked against the reader scope, so a memory the reader cannot see is
  never compared, never closed, and never returned.
* Episodes, procedures and unkinded writes are never triaged.

Closing the old record rewrites its flat file. ``integrity_hash`` covers id,
title, content and the emotional signature only, so ``valid_to`` does not touch
it and the record is NOT re-sealed; a detached signature sidecar, when one is
configured, is re-signed by ``FileBackend.save``. The edit is recorded in the
old record's ``metadata["superseded_by"]`` and in the WAL. Like reweave and
reinforcement it is a whole-file rewrite, so the value written is deterministic
(``new.valid_from``) and the close is skipped when ``valid_to`` is already set.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .audience import resolve_reader
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


def enabled() -> bool:
    """``SKMEMORY_WRITE_TRIAGE=off`` is the kill switch (default on)."""
    return os.environ.get("SKMEMORY_WRITE_TRIAGE", "on").strip().casefold() not in _OFF


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
            and scope.allows(m.context_tag, m.tags)
        ][:TOP_K]
        if not pool:
            return None
        # The strongest topical match is the only one judged: one LLM call at most.
        target = max(pool, key=lambda m: _lexical_similarity(memory.content, m.content))
        result = judge_relation(memory.content, target.content, use_llm=use_llm, llm_call=llm_call)
        if result.verdict == "unrelated":
            return None
        return WriteDecision(result.verdict, target, result.tier, result.status)
    except Exception as exc:  # noqa: BLE001 - triage must never lose the write
        logger.warning("write triage skipped: %s", type(exc).__name__)
        return None


def close_superseded(store: MemoryStore, old_id: str, new: Memory) -> bool:
    """Close ``old_id``'s validity at ``new.valid_from``. Idempotent, best effort."""
    old = store.primary.load(old_id)
    if old is None or old.valid_to is not None:
        return False
    sealed_ok = old.verify_integrity()
    now = datetime.now(timezone.utc).isoformat()
    old.valid_to = new.valid_from or new.created_at
    old.updated_at = now
    old.metadata = {
        **old.metadata,
        "superseded_by": {"id": new.id, "at": now, "via": "write_time_triage"},
    }
    store._wal.log_pending(
        "supersede_close", old.id, old.title, old.layer.value, {"superseded_by": new.id}
    )
    store.primary.save(old)
    store._wal.log_done("supersede_close", old.id)
    if sealed_ok and not old.verify_integrity():  # pragma: no cover - guard, hash ignores valid_to
        logger.error("closing %s broke its integrity hash", old.id)
    for backend, method in ((store.vector, "save"), (store.graph, "index_memory")):
        if backend is None:
            continue
        try:
            getattr(backend, method)(old)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reindex of closed %s failed: %s", old.id, type(exc).__name__)
    return True
