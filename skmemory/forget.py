"""Forget / redaction cascade report types (CR-8.3).

A forget request must delete a memory from EVERY store skmemory keeps and hand
back a verification report of what was purged where. The stores:

  * ``flat``     - the flat JSON files under ``memory/{short,mid,long}-term/``
                   (the Syncthing-synced source of truth).
  * ``index_db`` - the local ``index.db`` SQLite index (a rebuilt cache).
  * ``chroma``   - the derived ChromaDB vector store (when wired).
  * ``skmem_pg`` - the derived Postgres (pgvector) cache. This leg is expressed
                   as a delete PLAN (SQL + params) so it is verifiable without a
                   live database, and only best-effort-executed when a live
                   pgvector backend is wired (see :mod:`skmemory.store`).
  * ``graph``    - the AGE knowledge graph (when wired).

:class:`ForgetReport` is the structured result: one :class:`StorePurge` per
store, each recording whether the store was attached, how many rows/files were
found and removed, any error, and (for plan-based stores) the SQL that would
run. Derived-store failures are recorded, never raised, so one dead backend
cannot mask the rest of the cascade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def resolve_agent() -> str:
    """Resolve the active agent for scoping the skmem-pg delete plan.

    Mirrors :class:`skmemory.backends.pgvector_backend.PGVectorBackend`'s own
    precedence (``SKMEMORY_AGENT`` -> ``SKAGENT`` -> ``SKCAPSTONE_AGENT``,
    default ``lumina``) so a plan built without a wired backend still targets
    the same agent-scoped rows the live backend would.
    """
    return (
        os.environ.get("SKMEMORY_AGENT")
        or os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or "lumina"
    )


@dataclass
class StorePurge:
    """Per-store outcome of a forget cascade.

    Args:
        store: Store key (``flat`` | ``index_db`` | ``chroma`` | ``skmem_pg`` |
            ``graph`` | ``vector`` | ``skvector``).
        attached: Whether the store was wired/reachable for this cascade. A
            plan-only skmem-pg leg (no live pgvector wired) reports ``False``.
        found: Rows/files found for the id before deletion (best-effort; some
            derived stores can only report 0/1).
        removed: Rows/files actually removed.
        error: The failure string when the store's delete raised, else ``None``.
        plan: For plan-based stores (skmem-pg), the ordered SQL statements the
            delete comprises. ``None`` for stores that delete in place.
    """

    store: str
    attached: bool
    found: int = 0
    removed: int = 0
    error: str | None = None
    plan: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "store": self.store,
            "attached": self.attached,
            "found": self.found,
            "removed": self.removed,
            "error": self.error,
            "plan": self.plan,
        }


@dataclass
class ForgetReport:
    """Aggregate verification report for a forget cascade.

    Attributes:
        memory_id: The id that was forgotten.
        stores: One :class:`StorePurge` per store the cascade touched.
    """

    memory_id: str
    stores: list[StorePurge] = field(default_factory=list)

    @property
    def deleted(self) -> bool:
        """True when the source-of-truth (flat) store removed the memory.

        This preserves the legacy ``forget() -> bool`` contract: deletion is
        authoritative iff the flat file was present and removed.
        """
        flat = self.get("flat")
        return bool(flat and flat.removed > 0)

    @property
    def ok(self) -> bool:
        """True when no attached store recorded an error."""
        return all(s.error is None for s in self.stores if s.attached)

    def get(self, store: str) -> StorePurge | None:
        """Return the purge entry for ``store``, or ``None`` if absent."""
        for s in self.stores:
            if s.store == store:
                return s
        return None

    def __bool__(self) -> bool:
        return self.deleted

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "deleted": self.deleted,
            "ok": self.ok,
            "stores": [s.to_dict() for s in self.stores],
        }
