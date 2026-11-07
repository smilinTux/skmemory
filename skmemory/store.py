"""
MemoryStore - the main interface for storing and recalling memories.

This is the "camera" -- you point it at a moment, click, and it stores
a polaroid with full emotional context. Later, you recall by feeling
or by search, and the polaroid comes back with everything intact.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import decay as _decay
from . import retrieval_log, write_triage
from .agents import get_agent_paths
from .audience import ReaderScope, resolve_reader
from .backends.base import BaseBackend
from .backends.file_backend import FileBackend
from .backends.sqlite_backend import CONTENT_PREVIEW_LENGTH, SQLiteBackend
from .decompose import CHUNK_OVERLAP, CHUNK_TARGET, decompose_content
from .forget import ForgetReport, StorePurge, resolve_agent
from .fusion import (
    DEFAULT_ARM_CAP,
    ArmGuard,
    FusionResult,
    arm_executor,
    fuse,
    fusion_strategy,
    parse_time_window,
    route_query,
    run_arms_concurrent,
    search_deadline_s,
)
from .models import (
    PARENT_RELATION_KEY,
    RELATION_CHUNK_OF,
    RELATION_EXTENDS,
    RELATION_SUPERSEDES,
    EmotionalSnapshot,
    Memory,
    MemoryKind,
    MemoryLayer,
    MemoryOutcome,
    MemoryRole,
    SeedMemory,
    SourceTier,
)
from .procedures import order_procedures
from .query_sanitizer import sanitize_query
from .rerank import NoopReranker, RareTermReranker
from .retrieval import authority_weight, novelty_score, prepare_metadata, summarize_authorities
from .skseed_validation import annotate_truth_score, resolve_auto_validate
from .tiers import apply_tier_cap, tier_for_source
from .tombstones import write_tombstone
from .validation import (
    PreWriteHook,
    default_pre_write_hooks,
    run_pre_write_hooks,
)
from .validity import normalize_as_of, valid_at
from .wal import WriteAheadLog

# A search hit on the same memory counts once per this many seconds.
SEARCH_HIT_DEBOUNCE_S = 3600.0

logger = logging.getLogger("skmemory.store")

#: ``MemoryStore.last_search_degraded()`` value: an arm missed the deadline.
DEGRADED_ARMS_TIMED_OUT = "arms timed out"

MAX_CONTENT_LENGTH = 10000
CONTENT_OVERFLOW_STRATEGY = "split"  # "truncate" or "split"
DECOMPOSE_MIN_LENGTH = 1200
TASK_PACK_TAG = "task-pack"

# --- Reweave (backward pass) defaults -------------------------------------- #
# When a NEW memory is stored, the forward pass wires only the new memory's
# outgoing links. The reweave backward pass closes the loop: it finds the
# top-K most-related OLDER memories and adds a back-reference to the new
# memory on each, keeping the association graph coherent (bidirectional).
# Bounded by design — never re-weaves the whole corpus on a write.
REWEAVE_TOP_K = 5
# Tags that mark a memory as a decomposition/split fragment; these are skipped
# as reweave targets to avoid amplifying chunk noise.
REWEAVE_SKIP_TAGS = frozenset({"content-chunk"})


def _unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _memory_signal_set(memory: Memory) -> set[str]:
    signals: set[str] = set()
    for value in memory.tags:
        signals.add(value.casefold())
    decomposition = memory.metadata.get("decomposition", {})
    for key in ("entities", "citations", "claims", "section_titles"):
        for value in decomposition.get(key, []):
            if value:
                signals.add(str(value).casefold())
    if decomposition.get("section_title"):
        signals.add(str(decomposition["section_title"]).casefold())
    return signals


def _extract_query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9§.\-]{2,}", query.casefold())
        if len(term) > 2
    ]


def _extract_inline_citations(text: str) -> list[str]:
    patterns = [
        r"\b\d+\s+ILCS\s+[\dA-Za-z./()-]+",
        r"\b\d+\s+U\.?S\.?C\.?\s*[§]?\s*[\dA-Za-z./()-]+",
        r"\bUCC\s*[§]?\s*[\dA-Za-z./()-]+",
        r"\b\d+\s+CFR\s+[§]?\s*[\dA-Za-z./()-]+",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(match.strip() for match in re.findall(pattern, text, flags=re.IGNORECASE))
    return _unique_list(found)


def _accepts(fn, name: str) -> bool:
    """Whether ``fn`` takes a ``name`` keyword (capability check, made before calling)."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _passes_filters(mem, layer, tags, context_tag, source, as_of=None) -> bool:
    """Whether a hydrated memory satisfies the search filters (AND logic on tags)."""
    if as_of is not None and not valid_at(mem, as_of):
        return False
    if layer is not None:
        want = str(getattr(layer, "value", layer))
        have = getattr(mem, "layer", None)
        if str(getattr(have, "value", have)) != want:
            return False
    if tags and not set(tags) <= set(getattr(mem, "tags", None) or []):
        return False
    if context_tag is not None:
        allowed = [context_tag] if isinstance(context_tag, str) else list(context_tag)
        if getattr(mem, "context_tag", None) not in allowed:
            return False
    return not (source and getattr(mem, "source", None) != source)


MAX_CHAIN = 32


def _demote_list(memories: list[Memory]) -> list[Memory]:
    """``_demote_superseded`` over a ranked list. Runs LAST (after decay) so no
    later reorder can put a closed fact above its current head."""
    by_id = {m.id: m for m in memories}
    return [by_id[i] for i in _demote_superseded([m.id for m in memories], by_id)]


def _demote_superseded(ids: list[str], found: dict[str, Memory]) -> list[str]:
    """Rank a closed record directly below its open head, never above it.

    A record is superseded when it is closed (valid_to set) and a pooled record
    names it in ``supersedes_id`` or it carries ``metadata.superseded_by``. Only
    reorders: nothing is dropped, and a record whose head is not in the pool
    keeps its place. Stable: untouched records keep their relative order.
    """
    succ: dict[str, str] = {}
    for i in ids:
        m = found.get(i)
        if m is not None and getattr(m, "supersedes_id", None):
            succ[m.supersedes_id] = i

    def closed(i: str) -> bool:
        return getattr(found.get(i), "valid_to", None) is not None

    for i in ids:
        m = found.get(i)
        if closed(i) and i not in succ:
            nxt = ((getattr(m, "metadata", None) or {}).get("superseded_by") or {}).get("id")
            if nxt:
                succ[i] = nxt
    pooled = set(ids)

    def head(i: str) -> str | None:
        seen = {i}
        while i in succ and succ[i] in pooled and succ[i] not in seen:
            i = succ[i]
            seen.add(i)
        return i if len(seen) > 1 else None

    pos = {i: n for n, i in enumerate(ids)}
    # Only records ranked ABOVE their head move (a record already below it stays,
    # so this never promotes anything).
    moving = [i for i in ids if closed(i) and (h := head(i)) is not None and pos[i] < pos[h]]
    out = [i for i in ids if i not in set(moving)]
    for i in moving:
        out.insert(out.index(head(i)) + 1, i)
    return out


class MemoryStore:
    """Main entry point for all memory operations.

    Delegates to one or more backends. The primary backend handles
    all CRUD. A vector backend (optional) handles semantic search.
    A graph backend (optional) indexes relationships for traversal.

    Args:
        primary: The primary storage backend (default: FileBackend).
        vector: Optional vector search backend (PGVectorBackend).
        graph: Optional graph backend (AGEGraphBackend) for relationship indexing.
        max_content_length: Max chars before overflow strategy applies (default: 10000).
        content_overflow_strategy: "truncate" or "split" (default: "split").
    """

    def __init__(
        self,
        primary: BaseBackend | None = None,
        vector: BaseBackend | None = None,
        graph: Any | None = None,
        use_sqlite: bool = True,
        max_content_length: int = MAX_CONTENT_LENGTH,
        content_overflow_strategy: str = CONTENT_OVERFLOW_STRATEGY,
        decompose_min_length: int = DECOMPOSE_MIN_LENGTH,
        pre_write_hooks: list[PreWriteHook] | None = None,
        skseed_auto_validate: bool | None = None,
        reweave: bool = False,
        reweave_top_k: int = REWEAVE_TOP_K,
    ) -> None:
        if primary is not None:
            self.primary = primary
        elif use_sqlite:
            self.primary = SQLiteBackend()
        else:
            self.primary = FileBackend()
        self.vector = vector
        self.graph = graph
        # Search arms run on worker threads. The SQLite primary and the pg
        # vector backend hand each worker thread its OWN connection (one
        # sqlite3 connection used by two threads at once raises "API misuse").
        # AGE keeps one psycopg connection and wraps statement_timeout in a
        # transaction on it, so concurrent graph calls are serialised too.
        self._graph_lock = threading.Lock()
        # Caps in-flight calls per arm and cools a timed-out arm down, so one hung
        # backend cannot eat the shared worker pool (see fusion.ArmGuard).
        self._arm_guard = ArmGuard()
        self._search_tls = threading.local()
        # Backends that were CONFIGURED but failed to construct, by role
        # ("vector"/"graph") -> the real error text. health() reports these as
        # ok:false with the message; without this a configured-and-broken
        # backend and a never-configured one both read as `null`.
        self.backend_errors: dict[str, str] = {}
        self.max_content_length = max_content_length
        self.content_overflow_strategy = content_overflow_strategy
        self.decompose_min_length = decompose_min_length
        # Reweave backward pass — opt-in. When enabled, a simple snapshot()
        # write triggers a bounded backward pass that re-links the top-K most
        # related OLDER memories to the new one. Off by default so existing
        # callers see no behaviour change.
        self.reweave = reweave
        self.reweave_top_k = reweave_top_k

        # Pluggable pre-write validation hooks. Each is a callable
        # (Memory) -> None that raises to reject a malformed write before it
        # reaches any backend. Defaults to the canonical schema validator;
        # pass an explicit list (even []) to override, or use
        # register_pre_write_hook() to extend.
        self.pre_write_hooks: list[PreWriteHook] = (
            list(pre_write_hooks) if pre_write_hooks is not None else default_pre_write_hooks()
        )

        # Write-time SKSeed truth-check (card 9b72c6c2). Advisory + fail-open:
        # when enabled it annotates a memory with an advisory ``truth_score`` on
        # write; it never blocks, and no-ops when skseed is absent. Explicit
        # bool overrides; None resolves env > config > False.
        self.skseed_auto_validate: bool = (
            skseed_auto_validate if skseed_auto_validate is not None else resolve_auto_validate()
        )

        # Write-ahead log, resilient init so missing agent config doesn't block.
        #
        # The WAL belongs with the store it DESCRIBES. This used to come from
        # get_agent_paths() unconditionally, ignoring where the primary backend
        # was actually rooted, so a store explicitly pointed somewhere else
        # (every test that passes base_path=tmp_path) wrote its write-ahead log
        # straight into the LIVE agent directory. Same test-isolation shape as
        # the pytest rows that reached production pg, and `pytest_guard` does
        # not cover the WAL.
        #
        # For a real agent this resolves to the identical path it always did:
        # the primary's base_path IS <agent>/memory, so base_path/"wal" is
        # <agent>/memory/wal. Production behaviour is unchanged.
        from pathlib import Path as _Path

        primary_base = getattr(self.primary, "base_path", None)
        if primary_base is not None:
            self._wal = WriteAheadLog(_Path(primary_base) / "wal" / "write_log.jsonl")
            return
        try:
            agent_paths = get_agent_paths()
            wal_path = agent_paths["base"] / "memory" / "wal" / "write_log.jsonl"
        except Exception as e:
            logger.warning("store.py: %s", e)
            import tempfile

            wal_path = (
                __import__("pathlib").Path(tempfile.gettempdir())
                / "skmemory_wal"
                / "write_log.jsonl"
            )
        self._wal = WriteAheadLog(wal_path)

    def _probe_backend(self, role: str, backend: BaseBackend | None) -> dict:
        """Probe one derived backend and return an always-populated block.

        ``ok`` is True/False only when a probe actually ran or the backend is
        known to be configured-but-broken; ``None`` means NOT CONFIGURED and
        nothing else. A failed probe always carries the real error text.
        """
        if backend is None:
            err = self.backend_errors.get(role)
            if err:
                return {"ok": False, "configured": True, "error": err}
            return {"ok": None, "configured": False, "error": None}
        try:
            block = backend.health_check()
        except Exception as e:  # noqa: BLE001
            logger.warning("store.py: %s health probe failed: %s", role, e)
            return {"ok": False, "configured": True, "error": f"{type(e).__name__}: {e}"}
        if not isinstance(block, dict):
            return {
                "ok": False,
                "configured": True,
                "error": f"health_check returned {type(block).__name__}, expected dict",
            }
        block = dict(block)
        block.setdefault("configured", True)
        if block.get("ok") is False and not block.get("error"):
            block["error"] = "backend reported ok:false without detail"
        return block

    def _profile_info(self) -> dict:
        """Describe the corpus this store is rooted in."""
        from pathlib import Path as _Path

        base = getattr(self.primary, "base_path", None)
        try:
            from . import config as _config

            unresolved = _config.PROFILE_UNRESOLVED
            placeholder = _config.UNRESOLVED_HOME_NAME
        except Exception:  # noqa: BLE001
            unresolved, placeholder = None, ""
        # An explicit base_path elsewhere is the caller's own instruction.
        if unresolved and base is not None and _Path(base).parent.name != placeholder:
            unresolved = None
        info: dict = {"resolved": not unresolved, "is_template": False, "agent": None}
        if unresolved:
            info["error"] = unresolved
        if base is not None:
            home = _Path(base).parent.name
            info["agent"] = home
            info["is_template"] = home.endswith("-template")
        return info

    def _enrich_metadata(
        self,
        title: str,
        source: str,
        source_ref: str,
        tags: list[str] | None,
        metadata: dict | None,
    ) -> dict:
        return prepare_metadata(
            title=title,
            source=source,
            source_ref=source_ref,
            tags=tags,
            metadata=metadata,
        )

    def register_pre_write_hook(self, hook: PreWriteHook) -> None:
        """Register an additional pre-write validation hook.

        Hooks run in registration order right before a memory is persisted.
        Any hook that raises aborts the write and the exception propagates to
        the caller, so a memory that fails validation never reaches a backend.

        Args:
            hook: A callable ``(Memory) -> None`` that raises to reject.
        """
        self.pre_write_hooks.append(hook)

    def _run_pre_write_hooks(self, memory: Memory) -> None:
        """Run all registered pre-write hooks against *memory*.

        Raises:
            Exception: Whatever a hook raises (e.g. SchemaValidationError)
                when the memory is rejected.
        """
        run_pre_write_hooks(memory, self.pre_write_hooks)

    def snapshot(
        self,
        title: str,
        content: str,
        *,
        layer: MemoryLayer = MemoryLayer.SHORT,
        role: MemoryRole = MemoryRole.GENERAL,
        tags: list[str] | None = None,
        emotional: EmotionalSnapshot | None = None,
        source: str = "manual",
        source_ref: str = "",
        related_ids: list[str] | None = None,
        metadata: dict | None = None,
        decompose: bool = False,
        kind: MemoryKind | None = None,
        reader: str | None = None,
        supersedes_id: str | None = None,
        source_tier: SourceTier | None = None,
        outcome: MemoryOutcome | None = None,
        event_at: str | None = None,
    ) -> Memory:
        """Take a polaroid -- capture a moment as a memory.

        This is the primary way to create memories. It stores to
        the primary backend and optionally indexes in the vector backend.

        Args:
            title: Short label for this memory.
            content: The full memory content.
            layer: Persistence tier.
            role: Role-based partition.
            tags: Searchable tags.
            emotional: Emotional context snapshot.
            source: Where this memory came from.
            source_ref: Reference to the source.
            related_ids: IDs of related memories.
            metadata: Additional key-value data.
            kind: Schema v2 kind. None (the default) keeps today's behaviour
                exactly. fact, rule and preference are compared with similar
                same-kind memories at write time (see write_triage.py).
            reader: Who is writing. Dedup only matches memories this reader may
                see; a hash match with a hidden memory is ignored and a fresh
                memory is written, so the write cannot confirm hidden content
                or return its id. The write-time comparison also never compares,
                closes or returns a memory this reader cannot see.
            source_tier: Provenance tier. None derives it from ``source`` with
                :func:`skmemory.models.tier_for_source` (unknown labels are
                ``self-generated``, the capped, conservative choice).
            outcome: Schema v2 outcome (success, failure, lesson). None leaves it
                unset. Not carried through the decompose and split paths.
            event_at: When the event happened (ISO 8601). None keeps the
                default (created_at). Carried to every chunk and parent on the
                decompose and split paths.
            supersedes_id: Explicit supersession: always honoured (after the reader
                check), whatever the kind. The open head of that record's chain is
                closed at this memory's valid_from.

        Returns:
            Memory: The stored memory with its assigned ID.
        """
        metadata = self._enrich_metadata(title, source, source_ref, tags, metadata)

        # Dedup guard: check for existing memory with same content
        _content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        if isinstance(self.primary, SQLiteBackend):
            existing = self.primary.find_by_content_hash(_content_hash)
            if existing and not resolve_reader(reader).allows(existing.context_tag, existing.tags):
                existing = None
            if existing:
                logger.info(
                    "Duplicate content detected for title '%s' — returning existing memory %s",
                    title,
                    existing.id,
                )
                return existing

        if decompose or len(content) >= self.decompose_min_length:
            return self.ingest_document(
                title=title,
                content=content,
                layer=layer,
                role=role,
                tags=tags,
                emotional=emotional,
                source=source,
                source_tier=source_tier or tier_for_source(source),
                source_ref=source_ref,
                related_ids=related_ids,
                metadata=metadata,
                event_at=event_at,
            )

        # Handle content overflow
        if len(content) > self.max_content_length:
            if self.content_overflow_strategy == "split":
                return self._snapshot_split(
                    title=title,
                    content=content,
                    layer=layer,
                    role=role,
                    tags=tags,
                    emotional=emotional,
                    source=source,
                    source_tier=source_tier or tier_for_source(source),
                    source_ref=source_ref,
                    related_ids=related_ids,
                    metadata=metadata,
                    event_at=event_at,
                )
            else:
                logger.info(
                    "Content truncated from %d to %d chars for '%s'",
                    len(content),
                    self.max_content_length,
                    title,
                )
                content = content[: self.max_content_length]

        memory = Memory(
            title=title,
            content=content,
            layer=layer,
            role=role,
            tags=tags or [],
            emotional=emotional or EmotionalSnapshot(),
            source=source,
            source_tier=source_tier or tier_for_source(source),
            source_ref=source_ref,
            related_ids=related_ids or [],
            metadata=metadata,
            **({"kind": kind} if kind else {}),
            **({"outcome": outcome} if outcome else {}),
            **({"event_at": event_at} if event_at else {}),
        )

        # Write-time conflict verdict (card SKMEM-P3-02): fact/rule/preference only.
        if supersedes_id:
            decision = write_triage.explicit(self, supersedes_id, reader)
        else:
            decision = write_triage.decide(self, memory, reader=reader) if kind else None
        if (
            decision is not None
            and decision.verdict == "changes"
            and not write_triage.can_close(self, decision.target)
        ):
            logger.warning("supersession downgraded to extends: sealed record cannot be re-sealed")
            decision = write_triage.WriteDecision(
                "extends", decision.target, decision.tier, decision.status, True
            )
        if decision is not None:
            if decision.verdict == "unchanged":
                return decision.target
            if decision.possible_conflict and decision.verdict != "changes":
                memory.metadata["possible_conflict_with"] = [decision.target.id]
            memory.parent_id = decision.target.id
            if decision.verdict == "changes":
                memory.supersedes_id = decision.target.id
                memory.metadata[PARENT_RELATION_KEY] = RELATION_SUPERSEDES
            else:
                memory.metadata[PARENT_RELATION_KEY] = RELATION_EXTENDS
            memory.metadata["write_triage"] = {
                "verdict": decision.verdict,
                "target": decision.target.id,
                "tier": decision.tier,
                "status": decision.status,
            }

        # Infer valence from emotional labels if not explicitly set
        if memory.emotional.valence == 0.0 and memory.emotional.labels:
            POSITIVE = {
                "joy",
                "trust",
                "love",
                "anticipation",
                "hope",
                "gratitude",
                "excited",
                "happy",
            }
            NEGATIVE = {
                "fear",
                "anger",
                "disgust",
                "sadness",
                "grief",
                "anxiety",
                "frustrated",
                "disappointed",
            }
            labels_lower = {lbl.lower() for lbl in memory.emotional.labels}
            pos = len(labels_lower & POSITIVE)
            neg = len(labels_lower & NEGATIVE)
            if pos > neg:
                memory.emotional.valence = min(1.0, 0.3 + 0.2 * pos)
            elif neg > pos:
                memory.emotional.valence = max(-1.0, -0.3 - 0.2 * neg)

        # Write-time SKSeed truth-check (card 9b72c6c2). Advisory: annotates an
        # advisory ``truth_score`` into metadata and flags contradictions with
        # existing memories. Fail-open — never blocks the write, no-ops without
        # skseed. Runs before seal() so the annotation is captured, though the
        # integrity hash intentionally covers content/title/emotion only.
        if self.skseed_auto_validate:
            annotate_truth_score(memory, store=self)

        # Memory.intent auto-fill removed 2026-05-10 (no consumer; map keys
        # didn't match actual source distribution). Field still declared for
        # backward-compat — see skmemory/archived/predictive_2026-05-10/README.md
        memory.seal()

        # Pre-write validation: reject malformed memories before they hit
        # any backend. Raises (e.g. SchemaValidationError) on rejection.
        self._run_pre_write_hooks(memory)

        self._wal.log_pending("snapshot", memory.id, title, layer.value)
        try:
            self.primary.save(memory)
            self._wal.log_done("snapshot", memory.id)
        except Exception as exc:
            logger.warning("store.py: %s", exc)
            self._wal.log_failed("snapshot", memory.id, str(exc))
            raise

        if decision is not None and decision.verdict == "changes":
            try:
                write_triage.close_superseded(self, decision.target.id, memory)
            except Exception as exc:  # noqa: BLE001 - the new memory is already saved
                logger.warning("could not close superseded %s: %s", decision.target.id, exc)

        # Vector indexing (resilient)
        if self.vector:
            try:
                self.vector.save(memory)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.clear_sync_failure(memory.id, "vector")
                    self.primary.clear_sync_failure(memory.id, "skvector")  # legacy key
            except Exception as e:
                logger.warning("Vector save failed for %s: %s", memory.id, e)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.record_sync_failure(memory.id, "vector", str(e))

        # Graph indexing (resilient)
        if self.graph:
            try:
                self.graph.index_memory(memory)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.clear_sync_failure(memory.id, "graph")
                    self.primary.clear_sync_failure(memory.id, "skgraph")  # legacy key
            except Exception as e:
                logger.warning("Graph index failed for %s: %s", memory.id, e)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.record_sync_failure(memory.id, "graph", str(e))

        # Reweave backward pass (opt-in, bounded) — re-link older neighbours.
        if self.reweave:
            try:
                self._reweave_backward(memory)
            except Exception as exc:
                logger.warning("reweave backward pass failed for %s: %s", memory.id, exc)

        return memory

    def _reweave_backward(self, memory: Memory) -> list[str]:
        """Backward pass: re-link the top-K most-related OLDER memories.

        The forward write path only wires the *new* memory's outgoing edges.
        This closes the loop so the association graph stays coherent: for each
        of the top-K older memories most related to ``memory`` it

          * adds ``memory.id`` to the older memory's ``related_ids`` (back-ref),
          * bumps ``updated_at`` and re-seals its integrity hash,
          * re-persists it to the primary backend, and
          * best-effort re-indexes it in the vector + graph backends so their
            embeddings-links / weighted edges refresh too.

        The new memory is kept symmetric: any older neighbour discovered here
        (not already an explicit ``related_id``) is linked back on the new
        memory as well and persisted once.

        Bounded by design:

          * candidates = the new memory's explicit ``related_ids`` first, then
            up to ``2 * reweave_top_k`` topical search neighbours;
          * only genuinely older memories are touched (``created_at`` strictly
            before the new memory's);
          * decomposition/split fragments (``content-chunk``) are skipped;
          * at most ``reweave_top_k`` older memories are re-linked per write;
          * already-linked neighbours are skipped, so repeated writes are
            idempotent.

        Args:
            memory: The freshly stored memory driving the backward pass.

        Returns:
            list[str]: IDs of the older memories that were re-linked.
        """
        top_k = self.reweave_top_k
        if top_k <= 0:
            return []

        # --- 1. Gather candidate older memories (bounded) ---
        seen: set[str] = {memory.id}
        candidate_ids: list[str] = []

        # (a) Explicit outgoing links get first priority for a back-reference.
        for rid in memory.related_ids:
            if rid and rid not in seen:
                seen.add(rid)
                candidate_ids.append(rid)

        # (b) Topical neighbours via the existing search path (vector or text).
        query = " ".join([memory.title, *memory.tags]).strip() or memory.content[:200]
        if query:
            try:
                hits = self.search(query, limit=max(top_k * 2, top_k))
            except Exception as exc:
                logger.warning("reweave: neighbour search failed: %s", exc)
                hits = []
            for hit in hits:
                if hit.id not in seen:
                    seen.add(hit.id)
                    candidate_ids.append(hit.id)

        # --- 2. Re-link older memories, capped at top_k ---
        relinked: list[str] = []
        new_symmetric_links: list[str] = []
        for cid in candidate_ids:
            if len(relinked) >= top_k:
                break
            older = self.primary.load(cid)
            if older is None or older.id == memory.id:
                continue
            # Only reweave genuinely older memories.
            if older.created_at >= memory.created_at:
                continue
            if any(tag in REWEAVE_SKIP_TAGS for tag in older.tags):
                continue
            if memory.id in older.related_ids:
                continue  # already back-linked — idempotent

            older.related_ids = _unique_list([*older.related_ids, memory.id])
            older.updated_at = datetime.now(timezone.utc).isoformat()
            older.seal()
            try:
                self.primary.save(older)
            except Exception as exc:
                logger.warning("reweave: primary save failed for %s: %s", older.id, exc)
                continue
            relinked.append(older.id)
            if older.id not in memory.related_ids:
                new_symmetric_links.append(older.id)

            if self.vector:
                try:
                    self.vector.save(older)
                except Exception as exc:
                    logger.warning("reweave: vector reindex failed for %s: %s", older.id, exc)
            if self.graph:
                try:
                    self.graph.index_memory(older)
                except Exception as exc:
                    logger.warning("reweave: graph reindex failed for %s: %s", older.id, exc)

        # --- 3. Keep the new memory symmetric with discovered neighbours ---
        if new_symmetric_links:
            memory.related_ids = _unique_list([*memory.related_ids, *new_symmetric_links])
            memory.updated_at = datetime.now(timezone.utc).isoformat()
            memory.seal()
            try:
                self.primary.save(memory)
            except Exception as exc:
                logger.warning("reweave: symmetric persist failed for %s: %s", memory.id, exc)
            if self.vector:
                try:
                    self.vector.save(memory)
                except Exception as exc:
                    logger.warning("reweave: vector reindex (new) failed: %s", exc)
            if self.graph:
                try:
                    self.graph.index_memory(memory)
                except Exception as exc:
                    logger.warning("reweave: graph reindex (new) failed: %s", exc)

        if relinked:
            logger.info("reweave: linked %d older memories back to %s", len(relinked), memory.id)
        return relinked

    def snapshot_bulk(self, items: list[dict], progress_cb=None) -> list[Memory]:
        """Batch save multiple memories efficiently.

        Each item dict should have same kwargs as snapshot().
        Uses single SQLite transaction, then batch vector upsert.

        Args:
            items: List of dicts with title, content, and optional kwargs
            progress_cb: Optional callback(done, total) for progress reporting

        Returns:
            List of saved Memory objects (skips duplicates)
        """
        results = []
        total = len(items)

        for i, item in enumerate(items):
            try:
                mem = self.snapshot(**item)
                if mem:
                    results.append(mem)
            except Exception as e:
                logger.warning("snapshot_bulk item %d failed: %s", i, e)
            if progress_cb:
                progress_cb(i + 1, total)

        return results

    def ingest_document(
        self,
        title: str,
        content: str,
        *,
        layer: MemoryLayer = MemoryLayer.SHORT,
        role: MemoryRole = MemoryRole.GENERAL,
        tags: list[str] | None = None,
        emotional: EmotionalSnapshot | None = None,
        source: str = "document",
        source_ref: str = "",
        related_ids: list[str] | None = None,
        metadata: dict | None = None,
        chunk_target: int = CHUNK_TARGET,
        chunk_overlap: int = CHUNK_OVERLAP,
        source_tier: SourceTier | None = None,
        event_at: str | None = None,
    ) -> Memory:
        """Store a long-form document with decomposition-aware child chunks."""
        metadata = self._enrich_metadata(title, source, source_ref, tags, metadata)

        decomposition = decompose_content(
            content,
            chunk_target=chunk_target,
            chunk_overlap=chunk_overlap,
        )
        base_tags = list(tags or [])
        all_related = list(related_ids or [])
        child_ids: list[str] = []
        prepared_metadata = prepare_metadata(
            title=title,
            source=source,
            source_ref=source_ref,
            tags=base_tags,
            metadata=metadata,
        )

        for chunk in decomposition.chunks:
            chunk_memory = Memory(
                title=(
                    f"{title} [chunk {chunk.chunk_index + 1}/{chunk.total_chunks}]"
                    if chunk.total_chunks > 1
                    else title
                ),
                content=chunk.text,
                layer=layer,
                role=role,
                tags=_unique_list(
                    base_tags
                    + ["decomposed", "content-chunk"]
                    + [f"section:{chunk.section_title}"] * (1 if chunk.section_title else 0)
                ),
                emotional=emotional or EmotionalSnapshot(),
                source=source,
                source_tier=source_tier or tier_for_source(source),
                source_ref=source_ref,
                related_ids=[],
                **({"event_at": event_at} if event_at else {}),
                metadata={
                    **prepared_metadata,
                    "decomposition": {
                        "chunk_id": chunk.chunk_id,
                        "chunk_index": chunk.chunk_index,
                        "total_chunks": chunk.total_chunks,
                        "section_title": chunk.section_title,
                        "citations": chunk.citations,
                        "entities": chunk.entities,
                        "claims": chunk.claims,
                    },
                },
            )
            chunk_memory.seal()
            self._run_pre_write_hooks(chunk_memory)
            self.primary.save(chunk_memory)
            child_ids.append(chunk_memory.id)

        all_related.extend(child_ids)

        parent = Memory(
            title=title,
            content=content
            if len(content) <= self.max_content_length
            else (content[:200] + "..."),
            summary=content[:200] + ("..." if len(content) > 200 else ""),
            layer=layer,
            role=role,
            tags=_unique_list(base_tags + ["decomposed", "document-parent"]),
            emotional=emotional or EmotionalSnapshot(),
            source=source,
            source_tier=source_tier or tier_for_source(source),
            source_ref=source_ref,
            related_ids=all_related,
            **({"event_at": event_at} if event_at else {}),
            metadata={
                **prepared_metadata,
                "decomposition": decomposition.model_dump(exclude={"chunks"}),
                "chunk_memory_ids": child_ids,
                "original_length": len(content),
            },
        )
        parent.seal()
        self._run_pre_write_hooks(parent)
        self.primary.save(parent)

        for idx, child_id in enumerate(child_ids):
            child = self.primary.load(child_id)
            if child is None:
                continue
            child.parent_id = parent.id
            child.metadata[PARENT_RELATION_KEY] = RELATION_CHUNK_OF
            neighbours: list[str] = [parent.id]
            if idx > 0:
                neighbours.append(child_ids[idx - 1])
            if idx + 1 < len(child_ids):
                neighbours.append(child_ids[idx + 1])
            child.related_ids = _unique_list(neighbours)
            child.metadata["decomposition"]["parent_id"] = parent.id
            child.seal()
            self._run_pre_write_hooks(child)
            self.primary.save(child)
            if self.vector:
                try:
                    self.vector.save(child)
                except Exception as exc:
                    logger.warning("Vector indexing failed for chunk %s: %s", child.id, exc)
            if self.graph:
                try:
                    self.graph.index_memory(child)
                except Exception as exc:
                    logger.warning("Graph indexing failed for chunk %s: %s", child.id, exc)

        if self.vector:
            try:
                self.vector.save(parent)
            except Exception as exc:
                logger.warning("Vector indexing failed for document %s: %s", parent.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(parent)
            except Exception as exc:
                logger.warning("Graph indexing failed for document %s: %s", parent.id, exc)

        return parent

    def _snapshot_split(
        self,
        title: str,
        content: str,
        *,
        layer: MemoryLayer = MemoryLayer.SHORT,
        role: MemoryRole = MemoryRole.GENERAL,
        tags: list[str] | None = None,
        emotional: EmotionalSnapshot | None = None,
        source: str = "manual",
        source_ref: str = "",
        related_ids: list[str] | None = None,
        metadata: dict | None = None,
        source_tier: SourceTier | None = None,
        event_at: str | None = None,
    ) -> Memory:
        """Split oversized content into parent (summary) + child (chunk) memories.

        The parent memory contains a summary (first 200 chars) and links to
        child memories via related_ids. Each child holds one chunk.

        Returns:
            Memory: The parent memory.
        """
        chunk_size = self.max_content_length
        chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]

        logger.info(
            "Splitting '%s' (%d chars) into %d chunks",
            title,
            len(content),
            len(chunks),
        )

        # Create child memories first
        child_ids: list[str] = []
        for i, chunk in enumerate(chunks):
            child = Memory(
                title=f"{title} [part {i + 1}/{len(chunks)}]",
                content=chunk,
                layer=layer,
                role=role,
                tags=(tags or []) + ["content-chunk"],
                emotional=emotional or EmotionalSnapshot(),
                source=source,
                source_tier=source_tier or tier_for_source(source),
                source_ref=source_ref,
                **({"event_at": event_at} if event_at else {}),
                metadata={
                    **(metadata or {}),
                    "chunk_index": i,
                    "chunk_total": len(chunks),
                },
            )
            child.seal()
            self._run_pre_write_hooks(child)
            self.primary.save(child)
            child_ids.append(child.id)

        # Create parent with summary
        summary = content[:200] + ("..." if len(content) > 200 else "")
        all_related = (related_ids or []) + child_ids

        parent = Memory(
            title=title,
            content=summary,
            summary=summary,
            layer=layer,
            role=role,
            tags=(tags or []) + ["content-split-parent"],
            emotional=emotional or EmotionalSnapshot(),
            source=source,
            source_tier=source_tier or tier_for_source(source),
            source_ref=source_ref,
            related_ids=all_related,
            **({"event_at": event_at} if event_at else {}),
            metadata={
                **(metadata or {}),
                "split_children": child_ids,
                "original_length": len(content),
            },
        )
        parent.seal()
        self._run_pre_write_hooks(parent)
        self.primary.save(parent)

        if self.vector:
            try:
                self.vector.save(parent)
            except Exception as exc:
                logger.warning("Vector indexing failed for split parent %s: %s", parent.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(parent)
            except Exception as exc:
                logger.warning("Graph indexing failed for split parent %s: %s", parent.id, exc)

        return parent

    def recall(self, memory_id: str, reader: str | None = None) -> Memory | None:
        """Retrieve a specific memory by ID with integrity verification.

        A declared reader (or a ``SKMEMORY_READER`` process ceiling) that may
        not see the memory gets ``None``, exactly as for an id that does not
        exist: not-found, never forbidden, so existence is not an oracle.

        Automatically checks the integrity hash on recall. If the
        memory has been tampered with, a warning is logged and the
        memory's metadata is flagged with 'integrity_warning'.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Optional[Memory]: The memory if found.
        """
        memory = self.primary.load(memory_id)
        if memory is None:
            return None
        scope = resolve_reader(reader)
        if not scope.allows(memory.context_tag, memory.tags):
            return None

        if memory.integrity_hash and not memory.verify_integrity():
            logger.warning(
                "TAMPER ALERT: Memory %s failed integrity check! "
                "Content may have been modified since storage.",
                memory_id,
            )
            memory.metadata["integrity_warning"] = (
                f"Integrity check failed at {datetime.now(timezone.utc).isoformat()}. "
                "This memory may have been tampered with."
            )

        # Reinforce: a recall is a hit. Same batched path search() uses.
        self._reinforce([memory_id])

        return memory

    def _reinforce(self, memory_ids: Sequence[str], *, debounce_s: float | None = None) -> None:
        """Bump access_count / last_accessed for memories returned to a caller.

        One batched UPDATE on the SQLite index, best effort: a primary without
        usage counters is skipped, and a failure is logged and swallowed so it
        can never fail the read that triggered it.
        """
        bump = getattr(self.primary, "bump_access", None)
        if bump is None or not memory_ids:
            return
        try:
            if debounce_s is None:
                bump(list(memory_ids))
            else:
                bump(list(memory_ids), debounce_s=debounce_s)
        except Exception as exc:
            logger.debug("access bump skipped: %s", exc)

    def backfill_importance(self, batch_size: int = 500) -> dict | None:
        """Recompute legacy importance in the SQLite index; None if unsupported."""
        fn = getattr(self.primary, "backfill_importance", None)
        return fn(batch_size) if fn else None

    def usage_counts(self, memory_ids: Sequence[str]) -> dict[str, dict] | None:
        """Usage counters (``access_count``, ``last_accessed``) for ids.

        Returns ``None`` when the primary keeps no such counters, so a caller
        can tell "no counters here" from "counted zero".
        """
        get_usage = getattr(self.primary, "get_usage", None)
        if get_usage is None:
            return None
        return get_usage(list(memory_ids))

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        tags: list[str] | None = None,
        layer: str | None = None,
        source: str | None = None,
        context_tag: str | list[str] | None = None,
        record_access: bool = False,
        reader: str | None = None,
        as_of: str | datetime | None = None,
    ) -> list[Memory]:
        """Search memories by text.

        Uses vector backend if available, falls back to text search.

        Args:
            query: Search query string.
            limit: Maximum results.
            tags: Optional tag filter (AND logic).
            layer: Optional layer filter ("short-term", "mid-term", "long-term").
            source: Optional source filter.
            context_tag: Optional audience tag filter, exact or a list (any-of).
                Honoured by the fused semantic and graph arms and the primary
                text path; NOT an access-control check by itself, see
                ``audience.py`` for the resolver that decides what a reader
                may see.
            record_access: Count the returned memories as read by a person or
                agent (bumps ``access_count``, debounced to once per hour per
                memory). Only explicit caller-facing retrieval passes True.
                Background searches (auto-context, novelty, briefs, hooks)
                must leave it False so search popularity never drives
                promotion.
            as_of: ISO-8601 instant (naive or bare date reads as UTC). Keeps only
                memories valid then: ``coalesce(valid_from, created_at) <= as_of``
                and (``valid_to`` null or ``valid_to > as_of``), pushed into every
                arm and re-checked on the hydrated rows. ``None`` (the default)
                applies NO temporal filter: a closed or superseded memory is
                still returned, exactly as before this parameter existed. Raises
                ValueError when unparseable.
            reader: Who is asking, see ``audience.resolve_reader`` for the
                grammar (``@public``, ``@work:x``, ``channel:<id>``, ...).
                ``None`` is the owner, which keeps local owner sessions
                unchanged, UNLESS ``SKMEMORY_READER`` declares the process a
                non-owner (that env is a ceiling a per-call reader can only
                narrow). A declared reader is enforced in every arm and fails
                closed: an unparseable reader is ``@public``.

        Returns:
            list[Memory]: Matching memories ranked by relevance.
        """
        as_of = normalize_as_of(as_of)
        scope = resolve_reader(reader)
        # Over-fetch a pool so the tier cap has higher-tier hits to promote, then
        # cap ONCE here: every path (fused, degraded, fallback, legacy) and every
        # reader filter has already run, so the cap sees exactly what is returned.
        pool = max(limit, limit * 3)
        if not scope.unrestricted:
            rows = self._search_as_reader(
                query,
                scope,
                pool,
                tags=tags,
                layer=layer,
                source=source,
                context_tag=context_tag,
                record_access=False,
                as_of=as_of,
            )
        else:
            rows = self._search_unscoped(
                query,
                pool,
                tags=tags,
                layer=layer,
                source=source,
                context_tag=context_tag,
                record_access=False,
                as_of=as_of,
            )
        by_id = {m.id: m for m in rows}
        kept = [by_id[i] for i in apply_tier_cap([m.id for m in rows], by_id, limit)]
        # Last step, on reader-filtered and tier-capped hits: a failure procedure
        # never outranks a successful one for a how-to query.
        kept = order_procedures(kept, query)
        if record_access:
            self._reinforce([m.id for m in kept], debounce_s=SEARCH_HIT_DEBOUNCE_S)
        return kept

    def _search_unscoped(
        self, query, limit, *, tags, layer, source, context_tag, record_access=False, as_of=None
    ) -> list[Memory]:
        query = sanitize_query(query)
        self._search_tls.degraded = None

        if self._fusion_enabled():
            fused, report = self.search_fused(
                query,
                limit=limit,
                tags=tags,
                layer=layer,
                source=source,
                context_tag=context_tag,
                record_access=record_access,
                as_of=as_of,
                return_report=True,
            )
            if report.timed_out_arms:
                self._search_tls.degraded = DEGRADED_ARMS_TIMED_OUT
            if fused is not None:
                return _demote_list(self._apply_decay(fused))
            if report.timed_out_arms:
                # Nothing answered in time. The legacy path below would call the
                # same hung backends SYNCHRONOUSLY with no bound, so it is never
                # taken here: only the local SQLite lexical index, within what
                # is left of the deadline. An empty result is flagged degraded,
                # it is not a miss.
                results = self._local_only_search(
                    query,
                    limit,
                    tags=tags,
                    layer=layer,
                    source=source,
                    context_tag=context_tag,
                    remaining_s=report.deadline_s - report.elapsed_s,
                    as_of=as_of,
                )
                if record_access:
                    self._reinforce([m.id for m in results], debounce_s=SEARCH_HIT_DEBOUNCE_S)
                # Same tail as every other path: decay, then supersession demotion last.
                return _demote_list(self._apply_decay(results))

        # The legacy path cannot push context_tag down, so overfetch and filter.
        fetch = limit * 3 if (context_tag is not None or as_of) else limit

        def _ctx(rows):
            if context_tag is None and not as_of:
                return rows
            return [m for m in rows if _passes_filters(m, None, None, context_tag, None, as_of)][
                :limit
            ]

        if self.vector:
            try:
                results = _ctx(
                    self.vector.search_text(
                        query, limit=fetch, layer=layer, tags=tags, source=source
                    )
                )
                if results:
                    if record_access:
                        self._reinforce([m.id for m in results], debounce_s=SEARCH_HIT_DEBOUNCE_S)
                    return _demote_list(self._apply_decay(results))
            except Exception as exc:
                logger.warning("Vector search failed, falling back to text search: %s", exc)

        results = []
        local = getattr(self.primary, "search_lexical_local", None)
        if local is not None:
            try:
                results = local(
                    query,
                    limit=limit,
                    layer=layer,
                    tags=tags,
                    source=source,
                    context_tag=context_tag,
                    **({"as_of": as_of} if as_of and _accepts(local, "as_of") else {}),
                )
            except Exception as exc:
                logger.warning("Local FTS search failed, falling back to LIKE: %s", exc)
        if not results:
            results = _ctx(self.primary.search_text(query, limit=fetch))
        if record_access:
            self._reinforce([m.id for m in results], debounce_s=SEARCH_HIT_DEBOUNCE_S)
        return _demote_list(self._apply_decay(results))

    def last_search_degraded(self) -> str | None:
        """Why the last ``search()`` on THIS thread was degraded, else ``None``.

        ``DEGRADED_ARMS_TIMED_OUT`` means at least one arm missed the deadline,
        so the result may be missing hits: an empty list then is not a true miss.
        The per-arm detail is on the ``FusionResult`` from ``search_fused``.
        """
        return getattr(self._search_tls, "degraded", None)

    def _local_only_search(
        self, query, limit, *, tags, layer, source, context_tag, remaining_s, as_of=None
    ) -> list[Memory]:
        """Local SQLite lexical search, bounded by ``remaining_s`` (floor 0.1s).

        ``as_of`` is pushed to the FTS query when it takes it and always
        re-checked on the rows, like every arm.
        """
        local = getattr(self.primary, "search_lexical_local", None)
        fetch = limit * 3 if (context_tag is not None or as_of) else limit

        def run() -> list[Memory]:
            if local is not None:
                rows = local(
                    query,
                    limit=limit,
                    layer=layer,
                    tags=tags,
                    source=source,
                    context_tag=context_tag,
                    **({"as_of": as_of} if as_of and _accepts(local, "as_of") else {}),
                )
                return [m for m in rows if _passes_filters(m, None, None, None, None, as_of)]
            rows = self.primary.search_text(query, limit=fetch)
            return [
                m for m in rows if _passes_filters(m, layer, tags, context_tag, source, as_of)
            ][:limit]

        fut = arm_executor().submit(run)
        try:
            return fut.result(timeout=max(remaining_s, 0.1))
        except Exception as exc:  # noqa: BLE001 - timeout or a local failure: degraded, empty
            logger.warning("local-only search after arm timeouts gave nothing: %s", exc)
            return []

    def _apply_decay(self, results: list[Memory]) -> list[Memory]:
        """Bend a relevance-ordered result list by the SKMEMORY_DECAY channel.

        Only reorders what ``search`` already selected (never adds or drops a
        hit), skips when decay is off or the primary keeps no usage counters,
        and never fails the search.
        """
        channel = _decay.configured_channel()
        if channel is None or len(results) < 2:
            return results
        try:
            usage = self.usage_counts([m.id for m in results])
            return _decay.rerank(results, usage, channel)
        except Exception as exc:
            logger.debug("decay rerank skipped: %s", exc)
            return results

    def _search_as_reader(
        self,
        query,
        scope,
        limit,
        *,
        tags,
        layer,
        source,
        context_tag,
        record_access=False,
        as_of=None,
    ) -> list[Memory]:
        """Search for a declared (non-owner) reader.

        The allowed ``context_tag`` set is pushed into every arm (intersected
        with any explicit ``context_tag``), and the reader's never-share tag
        exclusions are applied to the hydrated rows, with an overfetch so the
        exclusion does not silently shorten the page.
        """
        allowed = scope.allowed_tags
        if allowed is not None:
            if context_tag is not None:
                wanted = {context_tag} if isinstance(context_tag, str) else set(context_tag)
                allowed = allowed & wanted
            if not allowed:
                return []
            ctx: str | list[str] | None = sorted(allowed)
        else:
            ctx = context_tag
        fetch = limit * 3 if scope.exclusions else limit
        rows = self._search_unscoped(
            query, fetch, tags=tags, layer=layer, source=source, context_tag=ctx, as_of=as_of
        )
        # Last gate: re-check every hydrated row against the full scope, so a
        # backend that ignored the pushed filter still cannot leak.
        kept = [m for m in rows if scope.allows(m.context_tag, m.tags)][:limit]
        if record_access:
            # Count only what the reader was actually given.
            self._reinforce([m.id for m in kept], debounce_s=SEARCH_HIT_DEBOUNCE_S)
        return kept

    def _fusion_enabled(self) -> bool:
        """Whether multi-arm fusion should handle ``search()``.

        ``SKMEMORY_SEARCH_FUSION`` takes ``on`` / ``off`` / ``auto`` (default
        ``auto``: fuse whenever a second arm is actually available, since with
        only the primary backend attached fusion and the legacy path are the
        same single query).
        """
        mode = os.environ.get("SKMEMORY_SEARCH_FUSION", "auto").strip().casefold()
        if mode in ("off", "0", "false", "no"):
            return False
        if mode in ("on", "1", "true", "yes"):
            return True
        return self.vector is not None or (self.graph is not None and self.graph_arm_enabled())

    GRAPH_ARM_ENV = "SKMEMORY_GRAPH_ARM"

    @classmethod
    def graph_arm_enabled(cls) -> bool:
        """Whether the graph arm takes part in ``search()``. OFF by default.

        ``SKMEMORY_GRAPH_ARM=on`` (or ``1``/``true``/``yes``) opts in. The arm was
        measured at recall@5 0.005 on self-queries and cost 0.3 to 2.3 s a call
        (a ``DISTINCT`` over every Memory node forces a sequential scan and
        returns arbitrary unranked rows), 67 percent of total arm time. This
        switches off ONLY the search arm: the graph backend, lineage, related and
        cluster tools, and graph indexing on write are untouched.
        """
        mode = os.environ.get(cls.GRAPH_ARM_ENV, "off").strip().casefold()
        return mode in ("on", "1", "true", "yes")

    def _retrieval_log_dir(self):
        """``<agent>/logs`` next to the primary's memory dir, or None when unknown."""
        base = getattr(self.primary, "base_path", None)
        return None if base is None else Path(base).parent / "logs"

    def _log_retrieval(self, query: str, report) -> None:
        """Queue one retrieval record. Best effort: never raises, never blocks."""
        try:
            retrieval_log.record(
                self._retrieval_log_dir(), retrieval_log.build_record(query, report)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("retrieval log skipped: %s", exc)

    def arm_liveness(self, status: dict) -> dict:
        """Arm liveness from the last 100 logged searches (shared across processes)."""
        backend_ok = {
            role: (status.get(role) or {}).get("ok")
            if isinstance(status.get(role), dict)
            else None
            for role in ("primary", "vector", "graph")
        }
        records = retrieval_log.read_window(self._retrieval_log_dir())
        return {
            "observed": self._fusion_enabled(),
            "note": None
            if self._fusion_enabled()
            else "legacy search path in use: searches are not logged, so liveness is unobserved",
            "window": len(records),
            "arms": retrieval_log.arm_liveness(records, self.search_arm_states(), backend_ok),
        }

    def search_arm_states(self) -> dict[str, dict]:
        """Per search arm: ``enabled`` plus ``state`` and, when off, the reason."""
        graph_on = self.graph_arm_enabled()
        if self.graph is None:
            graph_state = {"enabled": False, "state": "unavailable", "reason": "no graph backend"}
        elif graph_on:
            graph_state = {"enabled": True, "state": "enabled", "reason": None}
        else:
            graph_state = {
                "enabled": False,
                "state": "disabled",
                "reason": f"disabled by config ({self.GRAPH_ARM_ENV} is off, the default)",
            }
        return {"graph": graph_state}

    def _reranker(self):
        """The reranker for the fused path.

        ``SKMEMORY_SEARCH_RERANK`` takes ``on`` / ``off`` (default ``on``), and
        ``SKMEMORY_SEARCH_RERANK_DIVISOR`` overrides the rank divisor.

        On by default because reranking is what makes fusion safe to turn on at
        all: R1 alone widened recall while dropping MRR on the real backend
        (0.298 to 0.266), since interleaving hands every arm's champion a head
        slot and some champions are topical near-misses. The reranker abstains
        unless the query carries an identifier-shaped term, so on a query it has
        no opinion about it returns the identity order and costs nothing.
        """
        mode = os.environ.get("SKMEMORY_SEARCH_RERANK", "on").strip().casefold()
        if mode in ("off", "0", "false", "no"):
            return NoopReranker()
        raw = os.environ.get("SKMEMORY_SEARCH_RERANK_DIVISOR", "").strip()
        try:
            divisor = float(raw) if raw else None
        except ValueError:
            logger.warning("ignoring non-numeric SKMEMORY_SEARCH_RERANK_DIVISOR=%r", raw)
            divisor = None
        return RareTermReranker(**({"rank_divisor": divisor} if divisor else {}))

    def search_fused(
        self,
        query: str,
        limit: int = 10,
        *,
        tags: list[str] | None = None,
        layer: str | None = None,
        source: str | None = None,
        context_tag: str | list[str] | None = None,
        return_report: bool = False,
        record_access: bool = False,
        as_of: str | None = None,
    ) -> list[Memory] | tuple[list[Memory], FusionResult] | None:
        """Search by fusing every retrieval arm this store actually has.

        This is the UNSCOPED primitive: it enforces ``context_tag`` but knows
        nothing about readers. Reader enforcement lives in :meth:`search`; a
        caller serving a non-owner must go through that, not here.

        Arms run concurrently under one deadline and are merged by pinning the
        lexical arm's top 3 then RRF fill (``SKMEMORY_FUSION_STRATEGY=interleave``
        restores the previous rank-depth interleaving, see :mod:`skmemory.fusion`):

        ``semantic``  dense cosine over the vector backend.
        ``lexical``   the primary backend's text search (SQLite LIKE, or
                      Postgres BM25 when skmem-pg is the primary).
        ``graph``     one-hop entity expansion over the graph backend. OFF by
                      default (``SKMEMORY_GRAPH_ARM=on`` opts in).
        ``temporal``  a date-bounded listing, which only runs when the query
                      actually names a time window.

        Every filter (``layer``, ``tags``, ``source``) is pushed into each
        arm's own query rather than applied to the merged output. That ordering
        is postmortem 2026-10-16 §3.11's lesson: a predicate that runs after a
        limit silently returns fewer rows than asked for, and the caller cannot
        distinguish a filtered-empty from a genuinely-empty.

        Returns ``None`` when no arm produced anything, so the caller can take
        its legacy path with that fact visible rather than inferred. With
        ``return_report=True`` returns ``(memories, FusionResult)`` so a caller
        can see which arm produced which hit.
        """
        started_at = time.monotonic()
        found: dict[str, Memory] = {}
        # Arms run on worker threads: `found` is shared, so guard its writes.
        found_lock = threading.Lock()

        def asof_kw(fn) -> dict:
            # Sent only when set AND the arm's method takes it: a backend or fake
            # that predates as_of must not be handed an unknown kwarg (that fails
            # the whole arm). Such an arm is still held to as_of by _collect and
            # the graph re-check, so it is only less efficient, never wrong.
            return {"as_of": as_of} if as_of and _accepts(fn, "as_of") else {}

        def _collect(memories: Sequence[Memory], *, check_ctx: bool = False) -> list[str]:
            ids = []
            for mem in memories:
                mem_id = getattr(mem, "id", None)
                if not mem_id:
                    continue
                # The lexical and temporal backends cannot filter on
                # context_tag in their own query yet, so it is enforced here
                # on the overfetched arm (arm_limit is 3x limit). Fewer hits
                # than asked is possible; a leaked private hit is not.
                if (
                    check_ctx
                    and context_tag is not None
                    and not _passes_filters(mem, None, None, context_tag, None)
                ):
                    continue
                # as_of is re-checked on every arm: the pushed-down predicate is
                # the fast path, this is the guarantee for a backend that ignored it.
                if as_of and not valid_at(mem, as_of):
                    continue
                with found_lock:
                    found.setdefault(str(mem_id), mem)
                ids.append(str(mem_id))
            return ids

        # Over-fetch per arm so the merge has something to choose between: a
        # top-`limit` slice from each arm leaves fusion nothing to reorder.
        arm_limit = max(limit * 3, DEFAULT_ARM_CAP)

        def _semantic() -> list[str]:
            backend = self.vector
            search_semantic = getattr(backend, "search_semantic", None)
            if search_semantic is None:
                return []
            return _collect(
                search_semantic(
                    query,
                    limit=arm_limit,
                    layer=layer,
                    tags=tags,
                    context_tag=context_tag,
                    source=source,
                    **asof_kw(search_semantic),
                )
            )

        def _lexical() -> list[str]:
            # Prefer the strongest lexical engine the attached backends offer.
            # skmem-pg is attached as the VECTOR backend with SQLite as the
            # primary index, so taking the lexical arm from `self.primary`
            # means a deployment with ParadeDB BM25 available was still being
            # served SQLite LIKE. Measured on the real backend, that cost the
            # whole rare-identifier query class: BM25 puts the answer at rank
            # 7, LIKE does not surface it.
            #
            # This is a capability choice resolved once from what the backend
            # exposes, not a result-dependent fallback: it does not re-query
            # because it disliked the rows it got back.
            search_lexical = getattr(self.vector, "search_lexical", None)
            if search_lexical is not None:
                return _collect(
                    search_lexical(
                        query,
                        limit=arm_limit,
                        layer=layer,
                        tags=tags,
                        source=source,
                        **asof_kw(search_lexical),
                    ),
                    check_ctx=True,
                )
            return _collect(self.primary.search_text(query, limit=arm_limit), check_ctx=True)

        def _lexical_local() -> list[str]:
            # Always-on local tier: bm25 over the full text in index.db. With
            # skmem-pg down it is the only real lexical engine; with pg up it
            # is an extra voter and fusion handles the overlap.
            local = getattr(self.primary, "search_lexical_local", None)
            if local is None:
                return []
            return _collect(
                local(
                    query,
                    limit=arm_limit,
                    layer=layer,
                    tags=tags,
                    source=source,
                    context_tag=context_tag,
                    **asof_kw(local),
                )
            )

        def _graph() -> list[str]:
            backend = self.graph
            # Prefer token-level entity lookup when the backend offers it.
            # Whole-query substring matching only fires when the query is
            # itself close to a bare entity name: measured against lumina's
            # real graph, it fired on 0 of 10 realistic multi-word agent
            # queries, while per-token lookup fired on 10 of 10. An arm that is
            # attached but never fires is an unmeasured feature, not a working
            # one. Resolved once from what the backend exposes, not re-queried
            # because the first attempt returned little.
            search_by_entity = getattr(backend, "search_by_entity_tokens", None) or getattr(
                backend, "search_by_entity", None
            )
            if search_by_entity is None:
                return []
            ids = []
            graph_filters = {
                "layer": layer,
                "tags": tags,
                "context_tag": context_tag,
                "source": source,
                **asof_kw(search_by_entity),
            }
            with self._graph_lock:
                graph_rows = search_by_entity(query, limit=arm_limit, **graph_filters) or []
            for row in graph_rows:
                row_id = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
                if not row_id:
                    continue
                row_id = str(row_id)
                with found_lock:
                    known = row_id in found
                if not known:
                    # load, not recall: a graph candidate is not a hit
                    loaded = self.primary.load(row_id)
                    if loaded is None:
                        # The graph is a derived projection and can name a row
                        # the source of truth no longer has. Skip it rather
                        # than emit an id the caller cannot hydrate.
                        continue
                    # The graph is derived and can be stale (a layer or tag
                    # changed since indexing), so re-check the hydrated row
                    # against the source of truth. The Cypher WHERE already did
                    # the bulk of the work; this is the safety net.
                    if not _passes_filters(loaded, layer, tags, context_tag, source, as_of):
                        continue
                    with found_lock:
                        found.setdefault(row_id, loaded)
                ids.append(row_id)
            return ids

        def _temporal() -> list[str]:
            # strict: a date token inside a title ("... 2026-10-16 ...", a bare
            # year) is not time intent, so the arm abstains instead of listing
            # that month's memories into the pool.
            window = parse_time_window(query, strict=True)
            if window is None:
                return []  # no time intent in the query: this arm has no opinion
            after, before = window
            return _collect(
                self.primary.list_memories(
                    layer=layer,
                    tags=tags,
                    limit=arm_limit,
                    created_after=after,
                    created_before=before,
                    source=source,
                    **asof_kw(self.primary.list_memories),
                ),
                check_ctx=True,
            )

        graph_off = (
            None
            if self.graph is None or self.graph_arm_enabled()
            else f"disabled by config ({self.GRAPH_ARM_ENV} is off, the default)"
        )
        # All arms run at once under ONE total deadline, so the slowest cannot
        # dominate; a late arm is reported `timeout`, never as an empty result.
        arms = run_arms_concurrent(
            [
                ("semantic", _semantic if self.vector is not None else None, None),
                ("lexical", _lexical, None),
                (
                    "lexical_local",
                    _lexical_local
                    if getattr(self.primary, "search_lexical_local", None) is not None
                    else None,
                    None,
                ),
                ("graph", _graph if self.graph is not None else None, graph_off),
                ("temporal", _temporal, None),
            ],
            deadline_s=search_deadline_s(),
            executor=arm_executor(),
            guard=self._arm_guard,
        )
        # Fuse the WHOLE pool, then rerank, then truncate. Reranking a list
        # already cut to `limit` could never promote the candidate it exists to
        # promote: measured on the real backend, the correct answer for an
        # identifier query sits at fused rank 8.
        # Pin the strongest lexical engine attached: ParadeDB BM25 on the vector
        # backend when there is one, else the local FTS5 bm25. The plain
        # `lexical` arm is SQLite LIKE without a pg backend, which is not a
        # ranking worth pinning.
        has_pg_bm25 = getattr(self.vector, "search_lexical", None) is not None
        df_fn = getattr(self.primary, "term_doc_freq", None)
        route = route_query(query, df_fn) if fusion_strategy() == "pinned_rrf" else None
        report = fuse(
            arms,
            limit=max(limit, DEFAULT_ARM_CAP),
            strategy=fusion_strategy(),
            pin_arms=("lexical", "lexical_local") if has_pg_bm25 else ("lexical_local", "lexical"),
            route=route,
        )
        report.elapsed_s = time.monotonic() - started_at
        report.deadline_s = search_deadline_s()

        if report.ids:
            pool_texts = [
                f"{getattr(found[i], 'title', '')} {getattr(found[i], 'content', '')}"
                for i in report.ids
                if i in found
            ]
            pool_ids = [i for i in report.ids if i in found]
            outcome = self._reranker().rerank(query, pool_texts)
            if outcome.applied:
                logger.debug("search rerank: %s", outcome.detail)
                report.ids = [pool_ids[i] for i in outcome.order]
            else:
                report.ids = pool_ids
        report.ids = _demote_superseded(report.ids, found)[:limit]
        self._log_retrieval(query, report)

        if report.failed_arms or report.timed_out_arms:
            logger.warning(
                "search fusion ran with %d arm(s) failed and %d timed out: %s",
                len(report.failed_arms),
                len(report.timed_out_arms),
                report.summary(),
            )
        else:
            logger.debug("search fusion: %s", report.summary())

        if not report.ids:
            return (None, report) if return_report else None

        ordered = [found[i] for i in report.ids if i in found]
        # Only what is returned, and only on explicit request, counts as a hit.
        if record_access:
            self._reinforce([m.id for m in ordered], debounce_s=SEARCH_HIT_DEBOUNCE_S)
        return (ordered, report) if return_report else ordered

    def check_duplicate(
        self,
        content: str,
        threshold: float = 0.73,
        k: int = 5,
        reader: str | None = None,
    ) -> list[dict]:
        """Advisory pre-write duplicate check — does NOT write or merge anything.

        Ported from MemPalace's ``mempalace_check_duplicate``: lets a caller ask
        "does something like this already exist?" *before* calling snapshot(),
        using semantic (embedding) similarity rather than the exact SHA-256
        content-hash match that snapshot() already performs automatically.
        This is purely a read-only query — snapshot()'s own dedup behavior is
        untouched by this method.

        Args:
            content: Candidate content to check for near-duplicates.
            threshold: Minimum similarity (0.0-1.0) to count as a match.
                Default 0.73 — tuned empirically for mxbai-embed-large
                (2026-07-03). On a labeled dup/non-dup set, mxbai cosine
                similarity separated cleanly: near-duplicates 0.76-0.94,
                distinct content 0.27-0.70 (gap 0.703-0.763). 0.73 is the
                gap midpoint, favoring recall (an advisory dedup check should
                surface candidates for the caller to judge, not silently miss
                them). MemPalace's 0.9 was tuned for MiniLM and is too high here.
            k: Max number of candidates to fetch from the backend before
                filtering by threshold.

        Returns:
            list[dict]: Matches with ``{"id", "content_preview", "similarity"}``,
                sorted by similarity descending. Empty list if there's no
                vector backend, the backend doesn't support similarity
                lookups, or nothing clears the threshold.
        """
        if not self.vector:
            return []

        find_similar = getattr(self.vector, "find_similar", None)
        if not callable(find_similar):
            return []

        try:
            candidates = find_similar(content, k=k)
        except Exception as exc:
            logger.warning("check_duplicate: backend find_similar failed: %s", exc)
            return []

        hits = [c for c in candidates if c.get("similarity", 0.0) >= threshold]
        if not resolve_reader(reader).unrestricted:
            hits = [c for c in hits if self.reader_can_see(str(c.get("id", "")), reader)]
        return hits

    def novelty_search(self, query: str, limit: int = 10) -> list[dict]:
        """Surface potentially novel or under-linked memories for a query."""
        memories = self.search(query, limit=max(limit * 3, limit))
        query_terms = set(_extract_query_terms(query))
        signal_counts: Counter[str] = Counter()
        for memory in memories:
            signal_counts.update(_memory_signal_set(memory))

        scored: list[dict] = []
        for memory in memories:
            signals = _memory_signal_set(memory)
            rare_signals = [signal for signal in signals if signal_counts[signal] == 1]
            authority_tier = str(memory.metadata.get("authority_tier", "memory"))
            authority_bonus = {
                "statute": 0.25,
                "rule": 0.2,
                "form": 0.1,
                "secondary": 0.05,
                "case": 0.12,
                "template": -0.05,
                "memory": 0.0,
            }.get(authority_tier, 0.0)
            score = round(
                len(rare_signals) * 1.2
                + min(memory.emotional.intensity, 8.0) * 0.05
                + authority_bonus,
                3,
            )
            score = max(
                score,
                novelty_score(
                    query,
                    title=memory.title,
                    tags=memory.tags,
                    metadata=memory.metadata,
                ),
            )
            scored.append(
                {
                    "id": memory.id,
                    "title": memory.title,
                    "authority_tier": authority_tier,
                    "authority_weight": authority_weight(authority_tier),
                    "novelty_score": score,
                    "rare_signals": rare_signals[:8],
                    "query_overlap": sorted(query_terms & signals)[:8],
                    "tags": memory.tags,
                    "summary": memory.summary or memory.content[:220],
                    "trace": _unique_list(
                        [f"authority:{authority_tier}"]
                        + [f"rare:{signal}" for signal in rare_signals[:4]]
                    )[:6],
                }
            )

        scored.sort(key=lambda item: (-item["novelty_score"], item["title"].casefold()))
        return scored[:limit]

    def create_task_pack(
        self,
        task: str,
        *,
        query: str | None = None,
        limit: int = 8,
        layer: MemoryLayer = MemoryLayer.MID,
        role: MemoryRole = MemoryRole.GENERAL,
        tags: list[str] | None = None,
    ) -> Memory:
        """Capture a reusable task pack tying one problem to its strongest memories."""
        query_text = query or task
        brief = self.build_session_brief(query_text, limit=limit)
        novelty = brief["novelty"]
        related_ids = [item["id"] for item in brief["top_matches"]]
        summary_lines = [f"Task: {task}", f"Query: {query_text}", "", "Top matches:"]
        for item in brief["top_matches"]:
            summary_lines.append(
                f"- {item['title']} [{item['authority_tier']}] score={item['ranking_score']}"
            )
        if brief["deadlines"]:
            summary_lines.extend(["", "Deadlines / hearing rights:"])
            summary_lines.extend(f"- {item}" for item in brief["deadlines"][:4])
        if brief["defenses"]:
            summary_lines.extend(["", "Defense tracks:"])
            summary_lines.extend(f"- {item}" for item in brief["defenses"][:4])
        if novelty:
            summary_lines.extend(["", "Novel leads:"])
            for item in novelty[:3]:
                summary_lines.append(
                    f"- {item['title']}: {', '.join(item['rare_signals'][:3]) or 'low-linked signal'}"
                )
        return self.snapshot(
            title=f"Task Pack: {task}",
            content="\n".join(summary_lines),
            layer=layer,
            role=role,
            tags=_unique_list(list(tags or []) + [TASK_PACK_TAG]),
            source="task-pack",
            related_ids=related_ids,
            metadata={
                "authority_tier": "memory",
                "task_pack": {
                    "task": task,
                    "query": query_text,
                    "brief": brief,
                    "memory_ids": related_ids,
                    "novelty": novelty,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            decompose=False,
        )

    def build_session_brief(self, task: str, limit: int = 6) -> dict:
        """Build a structured session brief for a live issue."""
        direct_hits = self.search(task, limit=limit)
        novelty_hits = self.novelty_search(task, limit=min(5, limit))
        query_terms = set(_extract_query_terms(task))
        missing_facts: list[str] = []
        deadlines: list[str] = []
        defenses: list[str] = []
        top_matches: list[dict] = []
        task_lower = task.casefold()
        if "judgment" in task_lower:
            missing_facts.append(
                "Need judgment date, court, and whether it was default or contested."
            )
        if "repossess" in task_lower or "levy" in task_lower or "execution" in task_lower:
            missing_facts.append(
                "Need the exact enforcement instrument: repossession notice, levy, writ of execution, or garnishment."
            )
        if "exempt" in task_lower:
            missing_facts.append(
                "Need jurisdiction and property/funds categories to test exemptions and objection deadlines."
            )

        for memory in direct_hits:
            authority_tier = str(memory.metadata.get("authority_tier", "memory"))
            summary = memory.summary or memory.content[:220]
            overlap = sorted(query_terms & _memory_signal_set(memory))
            ranking_score = round(
                authority_weight(authority_tier)
                + 0.2 * len(overlap)
                + min(memory.emotional.intensity, 8.0) * 0.03,
                3,
            )
            top_matches.append(
                {
                    "id": memory.id,
                    "title": memory.title,
                    "authority_tier": authority_tier,
                    "ranking_score": ranking_score,
                    "summary": summary,
                    "tags": memory.tags,
                    "trace": _unique_list(
                        [f"authority:{authority_tier}"]
                        + [f"query_overlap:{item}" for item in overlap[:4]]
                        + [
                            f"citation:{item}"
                            for item in memory.metadata.get("decomposition", {}).get(
                                "citations", []
                            )[:3]
                        ]
                    )[:8],
                }
            )
            lowered = f"{memory.title} {summary}".casefold()
            if any(
                token in lowered
                for token in ("deadline", "hearing", "objection", "within", "notice", "response")
            ):
                deadlines.append(summary)
            if any(
                token in lowered
                for token in ("vacate", "service", "jurisdiction", "default", "void", "exempt")
            ):
                defenses.append(summary)

        top_matches.sort(key=lambda item: (-item["ranking_score"], item["title"].casefold()))

        return {
            "task": task,
            "authority_summary": summarize_authorities(direct_hits),
            "top_matches": top_matches[:limit],
            "facts": [
                {
                    "id": memory.id,
                    "title": memory.title,
                    "authority_tier": memory.metadata.get("authority_tier", "memory"),
                    "summary": memory.summary or memory.content[:220],
                    "tags": memory.tags,
                }
                for memory in direct_hits
            ],
            "novelty": novelty_hits,
            "deadlines": _unique_list(deadlines)[:6],
            "defenses": _unique_list(defenses)[:6],
            "citations": sorted(
                {
                    citation
                    for memory in direct_hits
                    for citation in (
                        memory.metadata.get("decomposition", {}).get("citations", [])
                        + _extract_inline_citations(
                            " ".join(
                                [
                                    memory.title,
                                    memory.summary or "",
                                    memory.content,
                                ]
                            )
                        )
                    )
                    if citation
                }
            )[:12],
            "entities": sorted(
                {
                    entity
                    for memory in direct_hits
                    for entity in memory.metadata.get("decomposition", {}).get("entities", [])
                    if entity
                }
            )[:12],
            "missing_facts": missing_facts,
            "recommended_queries": _unique_list(
                [task]
                + [
                    f"{task} {item['rare_signals'][0]}"
                    for item in novelty_hits
                    if item["rare_signals"]
                ]
                + [f"{task} authority", f"{task} deadline", f"{task} exemption"]
            )[:8],
        }

    def _tombstone_mem_dir(self) -> str | None:
        """Resolve the flat-memory dir where a forget tombstone should live.

        This must be the same per-agent memory dir that
        :func:`skmemory.reconcile.reconcile` scans, so the tombstone it writes is
        the one reconcile honours. Prefer the primary flat backend's
        ``base_path`` (a :class:`FileBackend` uses exactly that dir); fall back
        to the active agent's ``memory`` dir. Returns ``None`` if neither can be
        resolved (a forget must never fail because a marker location is unknown).
        """
        base = getattr(self.primary, "base_path", None)
        if base is not None:
            return str(base)
        try:
            return str(get_agent_paths()["base"] / "memory")
        except Exception as e:
            logger.warning("store.py: could not resolve tombstone dir: %s", e)
            return None

    @staticmethod
    def _vector_store_key(vector: BaseBackend) -> str:
        """Label a wired vector backend for the forget report.

        ``PGVectorBackend`` is the skmem-pg store; anything else is a generic
        ``vector`` leg.
        """
        if type(vector).__name__ == "PGVectorBackend":
            return "skmem_pg"
        return "vector"

    def _pg_plan_only_purge(self, memory_id: str) -> StorePurge:
        """A plan-only skmem-pg leg: SQL is reported, nothing is executed.

        Used when no live pgvector backend is wired. The delete PLAN is still
        emitted (agent-scoped, verifiable without a DB) so the report names what
        a reconcile/live node would run, but the cascade does not connect to
        Postgres here (fail-safe when absent).
        """
        from .backends.pgvector_backend import PGVectorBackend

        plan = PGVectorBackend.build_forget_plan(memory_id, resolve_agent())
        return StorePurge(store="skmem_pg", attached=False, plan=plan.sql)

    def _probe_primary(self, memory_id: str) -> tuple[int, int]:
        """Return ``(flat_found, index_found)`` before a primary delete.

        ``flat_found`` counts the flat JSON file (0/1). ``index_found`` counts
        the index.db row (0/1), and is always 0 for a non-SQLite primary.
        """
        flat_found = 0
        find = getattr(self.primary, "_find_file", None)
        if callable(find):
            flat_found = 1 if find(memory_id) is not None else 0

        index_found = 0
        if isinstance(self.primary, SQLiteBackend):
            try:
                row = (
                    self.primary._get_conn()
                    .execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,))
                    .fetchone()
                )
                index_found = 1 if row is not None else 0
            except Exception as e:  # index unavailable: report unknown as 0
                logger.warning("store.py: index probe failed for %s: %s", memory_id, e)
        return flat_found, index_found

    def _index_still_has(self, memory_id: str) -> bool:
        """True if the index.db row for ``memory_id`` still exists (post-delete)."""
        if not isinstance(self.primary, SQLiteBackend):
            return False
        try:
            row = (
                self.primary._get_conn()
                .execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,))
                .fetchone()
            )
            return row is not None
        except Exception:
            return False

    def forget_cascade(self, memory_id: str) -> ForgetReport:
        """Delete a memory from EVERY store and return a verification report.

        Cascades the removal across the flat JSON files (source of truth), the
        index.db SQLite index, the derived skmem-pg Postgres cache, plus the AGE graph when wired. Records
        a durable tombstone (see :mod:`skmemory.tombstones`) so a later
        reconcile refuses to resurrect the memory from a stale flat copy.

        Derived-store failures are recorded in the report, never raised, so one
        dead backend cannot block the others. A primary (source-of-truth) delete
        failure is fatal and re-raised, matching the legacy contract. The
        skmem-pg leg is expressed as a delete PLAN and only executed when a live
        pgvector backend is wired (otherwise plan-only, fail-safe).

        Args:
            memory_id: The memory to forget.

        Returns:
            ForgetReport: One :class:`~skmemory.forget.StorePurge` per store.
        """
        report = ForgetReport(memory_id=memory_id)

        # --- 1 + 2: primary (flat JSON + index.db) -------------------------
        flat_found, index_found = self._probe_primary(memory_id)
        self._wal.log_pending("forget", memory_id, "", "")
        try:
            deleted = self.primary.delete(memory_id)
            self._wal.log_done("forget", memory_id)
        except Exception as exc:
            logger.warning("store.py: %s", exc)
            self._wal.log_failed("forget", memory_id, str(exc))
            raise

        report.stores.append(
            StorePurge(
                store="flat",
                attached=True,
                found=flat_found,
                removed=1 if (flat_found and deleted) else 0,
            )
        )
        if isinstance(self.primary, SQLiteBackend):
            index_removed = index_found - (1 if self._index_still_has(memory_id) else 0)
            report.stores.append(
                StorePurge(
                    store="index_db",
                    attached=True,
                    found=index_found,
                    removed=max(0, index_removed),
                )
            )

        # Resurrection guard (card 7d3e9fcc): record the deliberate forget so a
        # future reconcile refuses to re-create it from a stale flat copy. Best
        # effort and non-fatal: the delete above already happened.
        mem_dir = self._tombstone_mem_dir()
        if mem_dir:
            write_tombstone(mem_dir, memory_id, reason="forget")

        # --- 3 / 4: derived vector store + skmem-pg ------------------------
        if self.vector is not None:
            key = self._vector_store_key(self.vector)
            purge = StorePurge(store=key, attached=True)
            if key == "skmem_pg":
                # A live pgvector backend IS the skmem-pg store: execute the
                # plan (via remove()) and report the SQL it ran.
                from .backends.pgvector_backend import PGVectorBackend

                purge.plan = PGVectorBackend.build_forget_plan(
                    memory_id, getattr(self.vector, "agent", resolve_agent())
                ).sql
            remover = getattr(self.vector, "remove", None)
            if callable(remover):
                try:
                    removed = remover(memory_id)
                    purge.found = purge.removed = 1 if removed else 0
                except Exception as exc:  # best-effort: record, keep going
                    purge.error = str(exc)
                    logger.warning(
                        "store.py: vector backend %s remove(%s) failed: %s",
                        type(self.vector).__name__,
                        memory_id,
                        exc,
                    )
            else:
                purge.error = f"{type(self.vector).__name__} has no remove()"
                logger.warning(
                    "store.py: vector backend %s has no remove(); memory %s NOT "
                    "removed from the vector store at forget time (lingers until "
                    "reconcile); add a remove() to this backend",
                    type(self.vector).__name__,
                    memory_id,
                )
            report.stores.append(purge)
            # When the wired vector store is NOT skmem-pg, skmem-pg is still a
            # distinct store: emit its plan (plan-only, fail-safe, not executed).
            if key != "skmem_pg":
                report.stores.append(self._pg_plan_only_purge(memory_id))
        else:
            report.stores.append(self._pg_plan_only_purge(memory_id))

        # --- graph (AGE) ---------------------------------------------------
        if self.graph is not None:
            graph_purge = StorePurge(store="graph", attached=True)
            try:
                removed = self.graph.remove_memory(memory_id)
                graph_purge.found = graph_purge.removed = 1 if removed else 0
            except Exception as exc:  # best-effort: record, keep going
                graph_purge.error = str(exc)
                logger.warning("store.py: graph remove failed: %s", exc)
            report.stores.append(graph_purge)

        return report

    def forget(self, memory_id: str) -> bool:
        """Delete a memory from all stores (thin bool shim over the cascade).

        Backward-compatible wrapper around :meth:`forget_cascade`; returns
        whether the source-of-truth (flat) store removed the memory. Callers
        that need the full per-store verification report should call
        :meth:`forget_cascade` directly.

        Args:
            memory_id: The memory to remove.

        Returns:
            bool: True if deleted from the flat (source-of-truth) store.
        """
        return self.forget_cascade(memory_id).deleted

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        created_after: str | None = None,
        created_before: str | None = None,
        source: str | None = None,
        reader: str | None = None,
    ) -> list[Memory]:
        """List memories with optional filtering.

        Args:
            layer: Filter by layer.
            tags: Filter by tags (AND logic).
            limit: Max results.
            source: Filter by the memory's ``source`` field, applied by
                the backend's own query before ``limit`` truncates the
                result.
            created_after: ISO 8601 timestamp lower bound (inclusive),
                applied by the backend's own query BEFORE ``limit``
                truncates the result. Pass this (with ``created_before``)
                when scoping to a historical date range -- fetching the N
                most recent and filtering by date afterward silently
                returns nothing once the store holds more than ``limit``
                memories (card: skmemory
                synthesis-silent-empty-on-historical-dates, 2026-10-17).
            created_before: ISO 8601 timestamp upper bound (exclusive).
            reader: Who is asking (see ``audience.resolve_reader``). A
                non-owner reader only receives memories it may see, and the
                page is filled from further down the list rather than
                shortened by the filter.

        Returns:
            list[Memory]: Matching memories sorted newest first.
        """
        scope = resolve_reader(reader)
        if not scope.unrestricted:
            return self._list_as_reader(
                scope,
                layer=layer,
                tags=tags,
                limit=limit,
                created_after=created_after,
                created_before=created_before,
                source=source,
            )
        return self.primary.list_memories(
            layer=layer,
            tags=tags,
            limit=limit,
            created_after=created_after,
            created_before=created_before,
            source=source,
        )

    def _list_as_reader(self, scope: ReaderScope, *, limit: int, **kw) -> list[Memory]:
        """Page through the primary until ``limit`` visible rows or exhaustion."""
        fetch = max(limit * 3, 50)
        while True:
            rows = self.primary.list_memories(limit=fetch, **kw)
            kept = [m for m in rows if scope.allows(m.context_tag, m.tags)]
            if len(kept) >= limit or len(rows) < fetch:
                return kept[:limit]
            fetch *= 4

    def history(self, memory_id: str, reader: str | None = None) -> list[dict]:
        """The supersession chain through ``memory_id``, oldest first.

        Walks ``supersedes_id`` back to the first record and forward over
        ``metadata.superseded_by`` (or, on a SQLite primary, the records that
        name this one in ``supersedes_id``). Returns ``[]`` for an unknown id.

        A declared reader sees only what it may see: a hidden start is the same
        as an unknown id, and the walk STOPS at the first hidden neighbour in
        either direction, so nothing past a hidden member is returned. The
        ``supersedes_id`` / ``superseded_by`` links on returned rows are kept
        only when they point at a row that is also returned.
        """
        scope = resolve_reader(reader)

        def visible(m: Memory | None) -> bool:
            return m is not None and scope.allows(m.context_tag, m.tags)

        start = self.primary.load(memory_id)
        if not visible(start):
            return []
        chain: list[Memory] = [start]
        seen = {start.id}
        cur = start
        for _ in range(MAX_CHAIN):
            prev = self.primary.load(cur.supersedes_id) if cur.supersedes_id else None
            if prev is None or prev.id in seen or not visible(prev):
                break
            chain.insert(0, prev)
            seen.add(prev.id)
            cur = prev
        cur = start
        ids_superseding = getattr(self.primary, "ids_superseding", None)
        hidden_next = False
        for _ in range(MAX_CHAIN):
            nxt_ids = [(cur.metadata.get("superseded_by") or {}).get("id")]
            if callable(ids_superseding):
                nxt_ids += ids_superseding(cur.id)
            nxt = next(
                (
                    m
                    for m in (self.primary.load(i) for i in dict.fromkeys(nxt_ids) if i)
                    if m is not None and m.id not in seen
                ),
                None,
            )
            if nxt is None:
                break
            if not visible(nxt):
                hidden_next = True
                break
            chain.append(nxt)
            seen.add(nxt.id)
            cur = nxt
        keep = {m.id for m in chain}
        # The last visible member was closed by a record this reader cannot see.
        # Show it as it would look with no hidden edit (open), so its valid_to
        # and `current` do not reveal that a hidden successor exists.
        masked = chain[-1].id if hidden_next else None
        return [
            {
                "id": m.id,
                "title": m.title,
                "layer": m.layer.value,
                "created_at": m.created_at,
                "valid_from": m.valid_from,
                "valid_to": None if m.id == masked else m.valid_to,
                "supersedes_id": m.supersedes_id if m.supersedes_id in keep else None,
                "superseded_by": (
                    sid
                    if (sid := (m.metadata.get("superseded_by") or {}).get("id")) in keep
                    else (chain[i + 1].id if i + 1 < len(chain) else None)
                ),
                "current": m.valid_to is None or m.id == masked,
                "requested": m.id == memory_id,
            }
            for i, m in enumerate(chain)
        ]

    def reader_can_see(self, memory_id: str, reader: str | None = None) -> bool:
        """Whether the reader may see this memory. An unknown id is not visible."""
        scope = resolve_reader(reader)
        if scope.unrestricted:
            return True
        memory = self.primary.load(memory_id)
        return memory is not None and scope.allows(memory.context_tag, memory.tags)

    # ── Reader-filtered graph reads ──────────────────────────────────────
    #
    # Graph nodes carry no context_tag, so visibility is decided against the
    # primary store. A hidden node is never expanded: a neighbour reachable
    # only THROUGH a private node is not shown, because its edge would reveal
    # the private node's existence.

    def graph_related(
        self, memory_id: str, depth: int = 2, reader: str | None = None
    ) -> list[dict]:
        """Related memories around ``memory_id`` as seen by ``reader``."""
        if self.graph is None:
            return []
        scope = resolve_reader(reader)
        if scope.unrestricted:
            return self.graph.get_related(memory_id, depth=depth)
        if not self.reader_can_see(memory_id, reader):
            return []
        depth = max(1, min(int(depth), 5))
        seen = {memory_id}
        frontier = [memory_id]
        out: list[dict] = []
        for level in range(1, depth + 1):
            nxt: list[str] = []
            for node in frontier:
                for row in self.graph.get_related(node, depth=1):
                    rid = row.get("id")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    if not self._graph_row_visible(row, reader):
                        continue
                    out.append({**row, "distance": level})
                    nxt.append(rid)
            frontier = nxt
        out.sort(key=lambda r: (r["distance"], -(r.get("intensity") or 0), r.get("title") or ""))
        return out

    def graph_lineage(self, memory_id: str, reader: str | None = None) -> list[dict]:
        """Promotion lineage, cut at the first ancestor the reader may not see."""
        if self.graph is None:
            return []
        chain = self.graph.get_lineage(memory_id)
        if resolve_reader(reader).unrestricted:
            return chain
        if not self.reader_can_see(memory_id, reader):
            return []
        kept: list[dict] = []
        for row in chain:
            if not self.reader_can_see(row.get("id", ""), reader):
                break
            kept.append(row)
        return kept

    def graph_clusters(self, min_size: int = 3, reader: str | None = None) -> list[dict]:
        """Cluster hubs the reader may see, counting only visible neighbours."""
        if self.graph is None:
            return []
        hubs = self.graph.find_clusters(min_size=min_size)
        if resolve_reader(reader).unrestricted:
            return hubs
        out = []
        for hub in hubs:
            if not self.reader_can_see(hub.get("id", ""), reader):
                continue
            n = len(self.graph_related(hub["id"], depth=1, reader=reader))
            if n >= min_size:
                out.append({**hub, "connections": n})
        out.sort(key=lambda h: -h["connections"])
        return out

    def _graph_row_visible(self, row: dict, reader: str | None) -> bool:
        ids = {row.get("id"), row.get("canonical_id")} - {None, ""}
        return bool(ids) and all(self.reader_can_see(i, reader) for i in ids)

    def filter_graph_rows(self, rows: list[dict], reader: str | None = None) -> list[dict]:
        """Drop graph search hits the reader may not see (fail closed).

        Rows keyed by ``id`` are kept only when visible. Related-claim rows
        (``memory_ids``) keep only their visible memories, and are dropped
        when none remain, so a claim asserted solely by private memories is
        not revealed.
        """
        if resolve_reader(reader).unrestricted:
            return rows
        out: list[dict] = []
        for row in rows:
            if "memory_ids" in row:
                ids = list(row.get("memory_ids") or [])
                titles = list(row.get("memory_titles") or [])
                keep = [i for i, mid in enumerate(ids) if self.reader_can_see(mid, reader)]
                if not keep:
                    continue
                out.append(
                    {
                        **row,
                        "memory_ids": [ids[i] for i in keep],
                        "memory_titles": [titles[i] for i in keep if i < len(titles)],
                        "support_count": len(keep),
                    }
                )
            elif self._graph_row_visible(row, reader):
                out.append(row)
        return out

    def promote(
        self,
        memory_id: str,
        target: MemoryLayer,
        summary: str = "",
    ) -> Memory | None:
        """Promote a memory to a higher persistence tier.

        Creates a new memory at the target layer linked to the original.
        The original stays in place as the detailed version.

        Args:
            memory_id: ID of the memory to promote.
            target: Target layer (should be higher than current).
            summary: Optional compressed summary.

        Returns:
            Optional[Memory]: The promoted memory, or None if source not found.

        Raises:
            ValueError: ``memory_id`` is empty or whitespace. An empty id is a
                CALLER bug, not a lookup that happened to miss, and returning
                None for it made the two indistinguishable: `promote("")`
                silently returned None, which reads as "that memory does not
                exist" when nothing was ever looked for. Every other write path
                here fails closed on an empty id (`require_memory_id`); promote
                was the one that did not.
        """
        if not str(memory_id or "").strip():
            raise ValueError("memory_id cannot be empty or null for promote()")

        source = self.primary.load(memory_id)
        if source is None:
            return None

        promoted = source.promote(target, summary=summary)
        self._run_pre_write_hooks(promoted)
        self._wal.log_pending("promote", promoted.id, promoted.title, target.value)
        try:
            self.primary.save(promoted)
            self._wal.log_done("promote", promoted.id)
        except Exception as exc:
            logger.warning("store.py: %s", exc)
            self._wal.log_failed("promote", promoted.id, str(exc))
            raise

        if self.vector:
            try:
                self.vector.save(promoted)
            except Exception as exc:
                logger.warning(
                    "Vector indexing failed for promoted memory %s: %s", promoted.id, exc
                )

        if self.graph:
            try:
                self.graph.index_memory(promoted)
            except Exception as exc:
                logger.warning(
                    "Graph indexing failed for promoted memory %s: %s", promoted.id, exc
                )

        return promoted

    def ingest_seed(self, seed: SeedMemory, *, validate: bool = True) -> Memory:
        """Import a Cloud 9 seed as a long-term memory.

        Converts a seed into a Memory and stores it. This is how
        seeds planted by one AI instance become retrievable memories
        for the next.

        When *validate* is True (default), basic integrity checks run
        before storage: seed_id must be non-empty and
        experience_summary must contain content.

        Args:
            seed: The SeedMemory to import.
            validate: Run pre-import validation (default True).

        Returns:
            Memory: The created long-term memory.

        Raises:
            ValueError: If validation is enabled and the seed is invalid.
        """
        if validate:
            errors: list[str] = []
            if not seed.seed_id or not seed.seed_id.strip():
                errors.append("seed_id is empty")
            if not seed.experience_summary or not seed.experience_summary.strip():
                errors.append("experience_summary is empty")
            if errors:
                raise ValueError(f"Seed validation failed: {'; '.join(errors)}")

        memory = seed.to_memory()
        self._run_pre_write_hooks(memory)
        self.primary.save(memory)

        if self.vector:
            try:
                self.vector.save(memory)
            except Exception as exc:
                logger.warning("Vector indexing failed for seed memory %s: %s", memory.id, exc)

        if self.graph:
            try:
                self.graph.index_memory(memory)
            except Exception as exc:
                logger.warning("Graph indexing failed for seed memory %s: %s", memory.id, exc)

        return memory

    def session_dump(self, session_id: str) -> list[Memory]:
        """Get all memories from a specific session.

        Args:
            session_id: The session identifier.

        Returns:
            list[Memory]: All memories tagged with this session.
        """
        return self.primary.list_memories(
            layer=MemoryLayer.SHORT,
            tags=[f"session:{session_id}"],
        )

    def consolidate_session(
        self,
        session_id: str,
        summary: str,
        emotional: EmotionalSnapshot | None = None,
    ) -> Memory:
        """Compress a session's short-term memories into a single mid-term memory.

        This is the "end of day" operation: take all the short-term snapshots
        from a session and create one consolidated mid-term memory that captures
        the essence. Individual short-term memories are preserved.

        Args:
            session_id: The session to consolidate.
            summary: Human/AI-written summary of the session.
            emotional: Overall emotional snapshot for the session.

        Returns:
            Memory: The consolidated mid-term memory.
        """
        session_memories = self.session_dump(session_id)
        related = [m.id for m in session_memories]
        all_tags = set()
        for m in session_memories:
            all_tags.update(m.tags)
        all_tags.add(f"session:{session_id}")
        all_tags.add("consolidated")

        return self.snapshot(
            title=f"Session: {session_id}",
            content=summary,
            layer=MemoryLayer.MID,
            role=MemoryRole.AI,
            tags=list(all_tags),
            emotional=emotional or EmotionalSnapshot(),
            source="consolidation",
            source_ref=session_id,
            related_ids=related,
            metadata={
                "source_count": len(session_memories),
                "consolidated_at": datetime.now(timezone.utc).isoformat(),
            },
            decompose=False,
        )

    def load_context(
        self,
        max_tokens: int = 4000,
        strongest_count: int = 5,
        recent_count: int = 5,
        include_seeds: bool = True,
        reader: str | None = None,
    ) -> dict:
        """Load tiered memory context for agent injection (lazy loading).

        Uses date-based tiers per memory-architecture.md:
        - Today's memories: full content (title + body)
        - Yesterday's memories: summary only (title + first 2 sentences)
        - Older than 2 days: reference count only

        Args:
            max_tokens: Approximate token budget (default: 4000).
                Uses word_count * 1.3 approximation for estimation.
            strongest_count: How many top-intensity memories to include.
            recent_count: How many recent memories to include.
            include_seeds: Whether to include seed memories.
            reader: Who is asking. A non-owner reader gets the same tiers
                built only from memories it may see; its counts and stats
                cover only those memories, so totals do not leak either.

        Returns:
            dict: Token-efficient tiered context with metadata.
        """
        scope = resolve_reader(reader)
        if not scope.unrestricted:
            return self._load_context_as_reader(scope, max_tokens, include_seeds)
        context: dict = {
            "today": [],
            "yesterday": [],
            "older_summary": {},
            "seeds": [],
            "stats": {},
        }
        used_tokens = 0

        if isinstance(self.primary, SQLiteBackend):
            conn = self.primary._get_conn()

            # --- Tier 1: Today's memories (full content) ---
            today_rows = conn.execute(
                "SELECT * FROM memories WHERE DATE(created_at) = DATE('now') "
                "ORDER BY importance DESC, created_at DESC LIMIT 20"
            ).fetchall()

            for row in today_rows:
                summary_dict = self.primary._row_to_memory_summary(row)
                # Include full content for today
                content = summary_dict.get("summary") or summary_dict.get("content_preview") or ""
                entry = {
                    "id": summary_dict["id"],
                    "title": summary_dict["title"],
                    "content": content,
                    "tags": summary_dict["tags"],
                    "layer": summary_dict["layer"],
                    "emotional_intensity": summary_dict["emotional_intensity"],
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + content)
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["today"].append(entry)

            # --- Tier 2: Yesterday's memories (summary only: title + first 2 sentences) ---
            yesterday_rows = conn.execute(
                "SELECT * FROM memories WHERE DATE(created_at) = DATE('now', '-1 day') "
                "ORDER BY importance DESC, created_at DESC LIMIT 20"
            ).fetchall()

            for row in yesterday_rows:
                summary_dict = self.primary._row_to_memory_summary(row)
                raw_text = summary_dict.get("summary") or summary_dict.get("content_preview") or ""
                short_summary = _first_n_sentences(raw_text, 2)
                entry = {
                    "id": summary_dict["id"],
                    "title": summary_dict["title"],
                    "summary": short_summary,
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + short_summary)
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["yesterday"].append(entry)

            # --- Tier 3: Older memories (reference count only) ---
            mid_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'mid-term'"
            ).fetchone()[0]
            long_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'long-term'"
            ).fetchone()[0]
            short_old_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE DATE(created_at) < DATE('now', '-1 day') "
                "AND layer = 'short-term'"
            ).fetchone()[0]

            context["older_summary"] = {
                "mid_term_count": mid_count,
                "long_term_count": long_count,
                "short_term_count": short_old_count,
                "total": mid_count + long_count + short_old_count,
                "hint": (
                    f"{mid_count} mid-term memories, {long_count} long-term memories "
                    "available via memory_search"
                ),
            }
            used_tokens += _estimate_tokens(context["older_summary"]["hint"])

            # --- Seeds (titles only to save tokens) ---
            if include_seeds:
                seed_rows = self.primary.list_summaries(
                    tags=["seed"],
                    limit=10,
                    order_by="emotional_intensity",
                )
                seen_ids = {m["id"] for m in context["today"]}
                seen_ids.update(m["id"] for m in context["yesterday"])

                for seed in seed_rows:
                    if seed["id"] in seen_ids:
                        continue
                    entry = {
                        "id": seed["id"],
                        "title": seed["title"],
                    }
                    entry_tokens = _estimate_tokens(seed["title"])
                    if used_tokens + entry_tokens > max_tokens:
                        break
                    used_tokens += entry_tokens
                    context["seeds"].append(entry)

            stats = self.primary.stats()
            context["stats"] = stats
        else:
            # Fallback for non-SQLite backends: simple recent list
            all_mems = self.primary.list_memories(limit=strongest_count + recent_count)
            for mem in all_mems:
                content_text = mem.summary or mem.content[:CONTENT_PREVIEW_LENGTH]
                entry = {
                    "id": mem.id,
                    "title": mem.title,
                    "summary": _first_n_sentences(content_text, 2),
                    "emotional_intensity": mem.emotional.intensity,
                    "layer": mem.layer.value,
                }
                entry_tokens = _estimate_tokens(entry["title"] + " " + entry["summary"])
                if used_tokens + entry_tokens > max_tokens:
                    break
                used_tokens += entry_tokens
                context["today"].append(entry)

        context["token_estimate"] = used_tokens
        context["token_budget"] = max_tokens
        return context

    def _load_context_as_reader(
        self, scope: ReaderScope, max_tokens: int, include_seeds: bool
    ) -> dict:
        """Tiered context for a non-owner reader, built from visible rows only."""
        from datetime import timedelta

        visible = self._list_as_reader(scope, limit=2000)
        today = datetime.now(timezone.utc).date()
        context: dict = {
            "today": [],
            "yesterday": [],
            "older_summary": {},
            "seeds": [],
            "stats": {"total_memories": len(visible)},
        }
        used = 0
        counts = {"mid-term": 0, "long-term": 0, "short-term": 0}
        shown: set[str] = set()
        for mem in visible:
            try:
                day = datetime.fromisoformat(str(mem.created_at).replace("Z", "+00:00")).date()
            except ValueError:
                day = None
            text = mem.summary or mem.content[:CONTENT_PREVIEW_LENGTH]
            if day == today:
                entry = {
                    "id": mem.id,
                    "title": mem.title,
                    "content": text,
                    "tags": mem.tags,
                    "layer": mem.layer.value,
                    "emotional_intensity": mem.emotional.intensity,
                }
                bucket, body = "today", text
            elif day == today - timedelta(days=1):
                body = _first_n_sentences(text, 2)
                entry = {"id": mem.id, "title": mem.title, "summary": body}
                bucket = "yesterday"
            else:
                counts[mem.layer.value] = counts.get(mem.layer.value, 0) + 1
                continue
            cost = _estimate_tokens(mem.title + " " + body)
            if used + cost > max_tokens:
                continue
            used += cost
            context[bucket].append(entry)
            shown.add(mem.id)
        total = sum(counts.values())
        hint = (
            f"{counts['mid-term']} mid-term memories, {counts['long-term']} long-term "
            "memories available via memory_search"
        )
        context["older_summary"] = {
            "mid_term_count": counts["mid-term"],
            "long_term_count": counts["long-term"],
            "short_term_count": counts["short-term"],
            "total": total,
            "hint": hint,
        }
        used += _estimate_tokens(hint)
        if include_seeds:
            seeds = [m for m in visible if "seed" in m.tags and m.id not in shown]
            seeds.sort(key=lambda m: m.emotional.intensity, reverse=True)
            for seed in seeds[:10]:
                cost = _estimate_tokens(seed.title)
                if used + cost > max_tokens:
                    break
                used += cost
                context["seeds"].append({"id": seed.id, "title": seed.title})
        context["token_estimate"] = used
        context["token_budget"] = max_tokens
        return context

    def export_backup(self, output_path: str | None = None, reader: str | None = None) -> str:
        """Export all memories to a dated JSON backup.

        Args:
            output_path: Destination file. Defaults to
                ``~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json``.

        Returns:
            str: Path to the written backup file.

        Raises:
            RuntimeError: If the primary backend doesn't support export.
            PermissionError: A non-owner reader asked for a whole-store dump.
        """
        if not resolve_reader(reader).unrestricted:
            raise PermissionError("export is owner-only: a declared reader may not dump the store")
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.export_all(output_path)
        if isinstance(self.primary, FileBackend):
            # Reason: wrap FileBackend in a temporary SQLiteBackend for export
            temp = SQLiteBackend(base_path=str(self.primary.base_path))
            temp.reindex()
            return temp.export_all(output_path)
        raise RuntimeError(f"Export not supported for backend: {type(self.primary).__name__}")

    def import_backup(self, backup_path: str) -> int:
        """Restore memories from a JSON backup file.

        Args:
            backup_path: Path to the backup JSON.

        Returns:
            int: Number of memories restored.

        Raises:
            RuntimeError: If the primary backend doesn't support import.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.import_backup(backup_path)
        raise RuntimeError(f"Import not supported for backend: {type(self.primary).__name__}")

    def list_backups(self, backup_dir: str | None = None) -> list[dict]:
        """List all skmemory backup files, sorted newest first.

        Args:
            backup_dir: Directory to scan. Defaults to
                ``~/.skcapstone/backups/``.

        Returns:
            list[dict]: Backup entries with ``path``, ``name``,
                ``size_bytes``, and ``date`` keys.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.list_backups(backup_dir)
        return []

    def prune_backups(self, keep: int = 7, backup_dir: str | None = None) -> list[str]:
        """Delete oldest backups, keeping only the N most recent.

        Args:
            keep: Number of backups to retain (default: 7).
            backup_dir: Directory to prune. Defaults to
                ``~/.skcapstone/backups/``.

        Returns:
            list[str]: Paths of deleted backup files.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.prune_backups(keep=keep, backup_dir=backup_dir)
        return []

    def reindex(self, force: bool = False, allow_preview_stubs: bool = False) -> int:
        """Rebuild the SQLite index from JSON files.

        Only works if the primary backend is SQLiteBackend. By default,
        SQLite-only memories survive the rebuild: those whose full content pg
        still holds are written to flat files, and preview-only ones keep
        their index row (never a lossy stub in the flat tier unless
        ``allow_preview_stubs``). Pass ``force=True`` to skip that safety step.

        Returns:
            int: Number of memories indexed, or -1 if not applicable.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.reindex(
                force=force,
                allow_preview_stubs=allow_preview_stubs,
                full_content_lookup=self._pg_fallback_content_lookup(),
            )
        return -1

    def export_orphans_to_flat(self, allow_preview_stubs: bool = False) -> dict:
        """Write any SQLite-only memories out as flat JSON files.

        Useful before a destructive operation, or after an import that
        wrote into SQLite without producing flat files. For each orphan,
        the configured vector/pg backend (``self.vector``, e.g. a
        ``PGVectorBackend`` backing skmem-pg) is tried FIRST for its full
        RECORD before falling back to the lossy ~150-char SQLite preview
        (card: skmemory sqlite-only-orphans recovery follow-up, 2026-10-16).
        Recovering the record rather than only its text keeps the metadata
        that the index has no column for; see ``_pg_fallback_record_lookup``.
        ``allow_preview_stubs`` defaults to False: a truncated stub destroys
        the real content, so it is opt-in.
        Returns ``{"exported", "skipped", "errors", "recovered_full",
        "orphan_ids"}``.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.export_orphans_to_flat(
                full_content_lookup=self._pg_fallback_content_lookup(),
                allow_preview_stubs=allow_preview_stubs,
                full_record_lookup=self._pg_fallback_record_lookup(),
            )
        return {"exported": 0, "skipped": 0, "errors": 0, "recovered_full": 0, "orphan_ids": []}

    def _pg_fallback_record_lookup(self) -> Callable[[str], Memory | None] | None:
        """Build the ``full_record_lookup`` for ``export_orphans_to_flat()``.

        Same backend and same failure handling as
        ``_pg_fallback_content_lookup``, but hands back the whole ``Memory``
        instead of just its text. ``PGVectorBackend.load()`` already parses
        the full record out of its ``memory_json`` column, so the metadata
        was always there; returning ``mem.content`` threw it away at this
        boundary and the orphan export then wrote a two-key metadata dict.
        A rescue that keeps id, title and content but silently drops
        ``access_count``, ``soul_context`` and every caller-set key looks
        like a success in every count and log line (noroc2027, 2026-11-06).
        """
        vector = self.vector
        if vector is None:
            return None

        def _lookup(memory_id: str) -> Memory | None:
            try:
                return vector.load(memory_id)
            except Exception as e:
                logger.warning(
                    "store.py: full-record fallback lookup failed for orphan %s: %s",
                    memory_id,
                    e,
                )
                return None

        return _lookup

    def _pg_fallback_content_lookup(self) -> Callable[[str], str | None] | None:
        """Build the ``full_content_lookup`` for ``export_orphans_to_flat()``.

        Backed by ``self.vector`` (whatever full-content-capable backend is
        configured -- typically the pg backend behind skmem-pg, but the same
        duck-typed ``load()`` works for any other backend wired in) when one
        is present. Returns ``None`` when no such backend is configured, so
        the orphan export degrades to the old lossy-preview-only behavior
        exactly as before. A per-id failure (unreachable backend, etc.) is
        caught here too and logged loudly rather than raised, so one bad
        lookup never aborts the whole rescue.
        """
        vector = self.vector
        if vector is None:
            return None

        def _lookup(memory_id: str) -> str | None:
            try:
                mem = vector.load(memory_id)
            except Exception as e:
                logger.warning(
                    "store.py: full-content fallback lookup failed for orphan %s: %s",
                    memory_id,
                    e,
                )
                return None
            return mem.content if mem is not None else None

        return _lookup

    def reconcile_sync_failures(self) -> dict:
        """Clear recorded sync failures for memories that ARE now indexed.

        `snapshot()` records a row when vector or graph indexing fails, and
        clears it when that same write later succeeds. Nothing else ever
        cleared one. A `reconcile` that successfully backfilled the memory
        left the record standing, so `health()`'s `unindexed` count only ever
        went up unless the identical snapshot path happened to run again.

        Found 2026-10-20: of 10 recorded failures on one agent, 6 were already
        present in pg (later syncs had fixed them) and 4 were real. Having to
        tell those apart by hand is the signal crying wolf, which is how a
        health number gets ignored, which is how this whole class of bug
        survived six months in the first place.

        Checks the live backend for each recorded id and clears only what is
        genuinely there. Returns counts; never raises.
        """
        report = {"checked": 0, "cleared": 0, "still_missing": 0, "errors": 0}
        get_failures = getattr(self.primary, "get_sync_failures", None)
        clear = getattr(self.primary, "clear_sync_failure", None)
        if not callable(get_failures) or not callable(clear):
            return report

        try:
            failures = get_failures(limit=1000)
        except Exception as e:
            logger.warning("store.py: could not read sync failures: %s", e)
            report["errors"] += 1
            return report

        for row in failures:
            memory_id = str(row.get("memory_id") or "")
            backend = str(row.get("backend") or "")
            if not memory_id:
                continue
            report["checked"] += 1
            # Which store the record is ABOUT decides which store to ask.
            target = self.graph if backend in {"skgraph", "graph", "age"} else self.vector
            loader = getattr(target, "load", None) or getattr(target, "get", None)
            if not callable(loader):
                continue
            try:
                present = loader(memory_id) is not None
            except Exception as e:
                logger.warning("store.py: presence check failed for %s: %s", memory_id, e)
                report["errors"] += 1
                continue
            if present:
                try:
                    clear(memory_id, backend)
                    report["cleared"] += 1
                except Exception as e:
                    logger.warning("store.py: could not clear %s/%s: %s", memory_id, backend, e)
                    report["errors"] += 1
            else:
                report["still_missing"] += 1
        return report

    def health(self, reader: str | None = None) -> dict:
        """Check health of all backends.

        Surfaces flat-vs-index drift as an explicit top-level ``status``
        (card: sqlite-only orphans, 2026-10-16 -- 23,525 orphaned index rows
        sat undetected for months because nothing turned drift into a
        non-green signal):

          * ``"ok"``       -- no known drift.
          * ``"degraded"`` -- ``sqlite_only`` and/or ``flat_only`` > 0 on the
            primary backend's local index.
          * ``"red"``      -- ``pg_only`` > 0 on a wired skmem-pg backend
            (a row with no backing flat file in the PRODUCTION derived
            store, the more severe sibling of the same failure shape).
            An ARCHIVED memory is not drift: its pg row is counted as
            ``pg_archived`` and does not colour the status.

        ``sqlite_only``/``flat_only``/``pg_only`` are always present in the
        returned dict (``None`` when not applicable/detectable, e.g. no
        SQLite index or no pgvector backend wired) so a caller never has to
        dig into nested backend-specific shapes to check for drift.

        A declared non-owner reader gets only the verdict flags: the full
        report carries store-wide counts, sync-failure and WAL detail that
        describe memories the reader may not see.

        Returns:
            dict: Combined health status.
        """
        status = {"primary": self.primary.health_check()}

        sqlite_only: int | None = None
        flat_only: int | None = None
        primary_sync = (
            status["primary"].get("sync") if isinstance(status["primary"], dict) else None
        )
        missing_tiers: list[str] = []
        unloadable: int | None = None
        if isinstance(primary_sync, dict):
            sqlite_only = primary_sync.get("sqlite_only")
            flat_only = primary_sync.get("flat_only")
            missing_tiers = list(primary_sync.get("missing_tiers") or [])
            unloadable = primary_sync.get("unloadable")

        status["vector"] = self._probe_backend("vector", self.vector)

        pg_only: int | None = None
        flat_id_set = getattr(self.primary, "flat_id_set", None)
        pg_drift_check = getattr(self.vector, "drift_check", None) if self.vector else None
        if callable(flat_id_set) and callable(pg_drift_check):
            try:
                # Pass the archive stems so a memory the promoter moved to cold
                # storage is reported as pg_archived, not as pg_only drift (and
                # so does not turn the whole node red). Optional capability:
                # a primary without _archived_stems keeps the old behaviour.
                archived_stems = getattr(self.primary, "_archived_stems", None)
                try:
                    archived_ids = archived_stems() if callable(archived_stems) else None
                except Exception:  # noqa: BLE001
                    archived_ids = None
                if archived_ids is None:
                    pg_drift = pg_drift_check(flat_id_set())
                else:
                    pg_drift = pg_drift_check(flat_id_set(), archived_ids)
                pg_only = pg_drift.get("pg_only")
                status["pg"] = pg_drift
            except Exception as e:
                # Best-effort: an unreachable skmem-pg must not make the
                # whole health check fail, but it also must not be
                # misreported as "pg_only: 0" -- leave it None (undetectable).
                logger.warning("store.py: pg drift check failed: %s", e)

        status["graph"] = self._probe_backend("graph", self.graph)
        # The graph SEARCH ARM being off by config is not the backend being dead:
        # the backend probe above is untouched and lineage/related keep working.
        status["arms"] = self.search_arm_states()
        if self.graph is not None and isinstance(status["graph"], dict):
            status["graph"]["search_arm"] = status["arms"]["graph"]["state"]

        # The sync-failure table had NO readers. `snapshot()` records a row
        # every time vector/graph indexing fails on a write, which is exactly
        # what happens whenever a derived backend is down, and then nothing
        # ever looked at it again: not health(), not the CLI, not the MCP
        # server. A memory that never made it into the index was discoverable
        # only by hand-querying index.db. The flat file is safe; its
        # SEARCHABILITY is silently gone, which is the half that matters.
        unindexed: int | None = None
        get_sync_failures = getattr(self.primary, "get_sync_failures", None)
        if callable(get_sync_failures):
            try:
                failures = get_sync_failures(limit=1000)
                unindexed = len(failures)
                if unindexed:
                    by_backend: dict[str, int] = {}
                    for row in failures:
                        name = str(row.get("backend") or "unknown")
                        by_backend[name] = by_backend.get(name, 0) + 1
                    status["unindexed_detail"] = {
                        "count": unindexed,
                        "by_backend": by_backend,
                        "hint": "memories whose derived-store indexing failed; "
                        "re-run `skmemory sync --vector --graph`",
                    }
            except Exception as e:
                # Undetectable, not zero. Same rule as pg_only.
                logger.warning("store.py: sync-failure read failed: %s", e)

        # The WAL's own crash-recovery feature had NO consumer. `wal.py`
        # faithfully logs every write as pending then done/failed, and
        # `pending_writes()` exists precisely to "detect incomplete writes
        # after a crash", but nothing in the repo ever called it: not health,
        # not the CLI, not the MCP server, not a startup check. A write
        # interrupted by a crash or OOM left a pending entry that no code path
        # would ever read. Its unit test passes against the class directly,
        # which hid the absence of any integration point.
        interrupted: int | None = None
        try:
            pending = self._wal.pending_writes()
            interrupted = len(pending)
            if interrupted:
                ops: dict[str, int] = {}
                for row in pending:
                    name = str(row.get("op") or "unknown")
                    ops[name] = ops.get(name, 0) + 1
                status["interrupted_writes_detail"] = {
                    "count": interrupted,
                    "by_op": ops,
                    "hint": "writes logged as pending that never completed; "
                    "verify those memories landed, then `skmemory sync`",
                }
        except Exception as e:
            # Undetectable, not zero. Same rule as pg_only.
            logger.warning("store.py: WAL pending-write read failed: %s", e)

        # A derived backend whose embedder does not answer is degraded even
        # when every count is clean. Semantic recall is silently gone and the
        # legacy chain answers from lexical matching instead, which looks like
        # a result and is not one. `None` means the probe did not run, which is
        # not evidence of health, so only an explicit False counts.
        vector_status = status.get("vector")
        embed_ok = vector_status.get("embed_ok") if isinstance(vector_status, dict) else None
        vector_ok = vector_status.get("ok") if isinstance(vector_status, dict) else None
        # A configured vector backend that is broken cannot have a working
        # embedder, and `None` here would read as "not probed". Say so.
        if vector_ok is False and embed_ok is None:
            embed_ok = False
            vector_status["embed_ok"] = False
            vector_status.setdefault("embed_error", "not probed: vector backend unavailable")

        overall = "ok"
        if (sqlite_only or 0) > 0 or (flat_only or 0) > 0:
            overall = "degraded"
        primary_wal = status["primary"].get("wal") if isinstance(status["primary"], dict) else None
        if isinstance(primary_wal, dict):
            status["wal"] = primary_wal
            if primary_wal.get("wal_ok") is False:
                overall = "degraded"
        # An ABSENT tier directory is not an empty one. A layer that never
        # synced onto this node has no index rows and no files, so every count
        # reads zero and the old check called that in sync.
        if missing_tiers:
            overall = "degraded"
        if (unindexed or 0) > 0:
            overall = "degraded"
        # A flat file that exists and cannot be parsed is the SOURCE OF TRUTH
        # being unreadable, which is worse than a stale derived cache.
        if (unloadable or 0) > 0:
            overall = "degraded"
        if (interrupted or 0) > 0:
            overall = "degraded"
        # A vector backend that is WIRED and reporting ok:false was green at the
        # top level until now. Observed 2026-10-20: two agents whose derived
        # backend was a retired vector store answering "Not initialized", and
        # one with 13 unparseable rows, every one of them reporting an overall
        # `status: ok`. If a backend is attached, its own verdict counts.
        if vector_ok is False:
            overall = "degraded"
        if embed_ok is False:
            overall = "degraded"
        # The graph backend got the same treatment as the vector one, which it
        # had been missing. `status["graph"]` was set and then never read when
        # computing the verdict, so a wired graph answering ok:false left the
        # top level green. Confirmed 2026-10-20 with a stub backend:
        # graph.ok False, overall "ok".
        graph_status = status.get("graph")
        graph_ok = graph_status.get("ok") if isinstance(graph_status, dict) else None
        if graph_ok is False:
            overall = "degraded"
        if (pg_only or 0) > 0:
            overall = "red"

        # Which corpus is this? A template is a scaffold, never a live store,
        # and a primary that reports ok while rooted in one is the worst
        # available answer (measured 2026-11-06: 367 template memories served
        # while the real home held 51,270).
        profile = self._profile_info()
        status["profile"] = profile
        if profile["is_template"] or not profile["resolved"]:
            overall = "red"

        # Arm liveness from the retrieval log. Only errors and timeouts (or a
        # zero-hit streak with a failing backend probe) degrade: a disabled arm,
        # or one that legitimately finds nothing, does not.
        try:
            liveness = self.arm_liveness(status)
            status["retrieval"] = liveness
            if overall == "ok" and any(
                a["state"] == "degraded" for a in liveness["arms"].values()
            ):
                overall = "degraded"
        except Exception as e:  # noqa: BLE001
            logger.warning("store.py: arm liveness read failed: %s", e)

        status["status"] = overall
        status["sqlite_only"] = sqlite_only
        status["flat_only"] = flat_only
        status["pg_only"] = pg_only
        status["pg_archived"] = (status.get("pg") or {}).get("pg_archived")
        status["embed_ok"] = embed_ok
        status["vector_ok"] = vector_ok
        status["graph_ok"] = graph_ok
        status["missing_tiers"] = missing_tiers
        status["unindexed"] = unindexed
        status["unloadable"] = unloadable
        status["interrupted_writes"] = interrupted
        if not resolve_reader(reader).unrestricted:
            keep = ("status", "embed_ok", "vector_ok", "graph_ok")
            return {k: status[k] for k in keep}
        return status


def _estimate_tokens(text: str) -> int:
    """Estimate token count using word_count * 1.3 approximation.

    Args:
        text: The text to estimate.

    Returns:
        int: Approximate token count.
    """
    if not text:
        return 0
    word_count = len(text.split())
    return int(word_count * 1.3)


def _first_n_sentences(text: str, n: int = 2) -> str:
    """Extract the first N sentences from text.

    Args:
        text: Source text.
        n: Number of sentences to extract.

    Returns:
        str: The first N sentences, or the full text if fewer exist.
    """
    if not text:
        return ""
    # Split on sentence-ending punctuation followed by whitespace
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    result = " ".join(sentences[:n])
    # Cap at 200 chars as a safety net
    if len(result) > 200:
        result = result[:197] + "..."
    return result
