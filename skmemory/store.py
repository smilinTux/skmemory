"""
MemoryStore - the main interface for storing and recalling memories.

This is the "camera" -- you point it at a moment, click, and it stores
a polaroid with full emotional context. Later, you recall by feeling
or by search, and the polaroid comes back with everything intact.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from .agents import get_agent_paths
from .backends.base import BaseBackend
from .backends.file_backend import FileBackend
from .backends.skgraph_backend import SKGraphBackend
from .backends.sqlite_backend import CONTENT_PREVIEW_LENGTH, SQLiteBackend
from .cascade import CascadeExecutor, CascadeStep
from .decompose import CHUNK_OVERLAP, CHUNK_TARGET, decompose_content
from .models import (
    EmotionalSnapshot,
    Memory,
    MemoryLayer,
    MemoryRole,
    SeedMemory,
)
from .query_sanitizer import sanitize_query
from .retrieval import authority_weight, novelty_score, prepare_metadata, summarize_authorities
from .skseed_validation import annotate_truth_score, resolve_auto_validate
from .tombstones import write_tombstone
from .validation import (
    PreWriteHook,
    default_pre_write_hooks,
    run_pre_write_hooks,
)
from .wal import WriteAheadLog

logger = logging.getLogger("skmemory.store")

MAX_CONTENT_LENGTH = 10000
CONTENT_OVERFLOW_STRATEGY = "split"  # "truncate" or "split"
DECOMPOSE_MIN_LENGTH = 1200
TASK_PACK_TAG = "task-pack"


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


class MemoryStore:
    """Main entry point for all memory operations.

    Delegates to one or more backends. The primary backend handles
    all CRUD. A vector backend (optional) handles semantic search.
    A graph backend (optional) indexes relationships for traversal.

    Args:
        primary: The primary storage backend (default: FileBackend).
        vector: Optional vector search backend (e.g., SKVectorBackend).
        graph: Optional graph backend (e.g., SKGraphBackend) for relationship indexing.
        max_content_length: Max chars before overflow strategy applies (default: 10000).
        content_overflow_strategy: "truncate" or "split" (default: "split").
    """

    def __init__(
        self,
        primary: BaseBackend | None = None,
        vector: BaseBackend | None = None,
        graph: SKGraphBackend | None = None,
        use_sqlite: bool = True,
        max_content_length: int = MAX_CONTENT_LENGTH,
        content_overflow_strategy: str = CONTENT_OVERFLOW_STRATEGY,
        decompose_min_length: int = DECOMPOSE_MIN_LENGTH,
        pre_write_hooks: list[PreWriteHook] | None = None,
        skseed_auto_validate: bool | None = None,
    ) -> None:
        if primary is not None:
            self.primary = primary
        elif use_sqlite:
            self.primary = SQLiteBackend()
        else:
            self.primary = FileBackend()
        self.vector = vector
        self.graph = graph
        self.max_content_length = max_content_length
        self.content_overflow_strategy = content_overflow_strategy
        self.decompose_min_length = decompose_min_length

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

        # Write-ahead log — resilient init so missing agent config doesn't block
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

        # Executor for fanning a single op out across the derived backends
        # (vector + graph). Centralises the best-effort partial-failure handling
        # that store operations used to hand-roll at each call site.
        self._cascade = CascadeExecutor(logger)

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

        Returns:
            Memory: The stored memory with its assigned ID.
        """
        metadata = self._enrich_metadata(title, source, source_ref, tags, metadata)

        # Dedup guard: check for existing memory with same content
        _content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        if isinstance(self.primary, SQLiteBackend):
            existing = self.primary.find_by_content_hash(_content_hash)
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
                source_ref=source_ref,
                related_ids=related_ids,
                metadata=metadata,
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
                    source_ref=source_ref,
                    related_ids=related_ids,
                    metadata=metadata,
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
            source_ref=source_ref,
            related_ids=related_ids or [],
            metadata=metadata,
        )

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

        # Vector indexing (resilient)
        if self.vector:
            try:
                self.vector.save(memory)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.clear_sync_failure(memory.id, "skvector")
            except Exception as e:
                logger.warning("SKVector save failed for %s: %s", memory.id, e)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.record_sync_failure(memory.id, "skvector", str(e))

        # Graph indexing (resilient)
        if self.graph:
            try:
                self.graph.index_memory(memory)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.clear_sync_failure(memory.id, "skgraph")
            except Exception as e:
                logger.warning("SKGraph index failed for %s: %s", memory.id, e)
                if isinstance(self.primary, SQLiteBackend):
                    self.primary.record_sync_failure(memory.id, "skgraph", str(e))

        return memory

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
                source_ref=source_ref,
                related_ids=[],
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
            source_ref=source_ref,
            related_ids=all_related,
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
                source_ref=source_ref,
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
            source_ref=source_ref,
            related_ids=all_related,
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

    def recall(self, memory_id: str) -> Memory | None:
        """Retrieve a specific memory by ID with integrity verification.

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

        # Update LRU tracking
        if isinstance(self.primary, SQLiteBackend):
            try:
                conn = self.primary._get_conn()
                conn.execute(
                    "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), memory_id),
                )
                conn.commit()
            except Exception as e:
                logger.warning("store.py: %s", e)
                pass

        return memory

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        tags: list[str] | None = None,
        layer: str | None = None,
        source: str | None = None,
    ) -> list[Memory]:
        """Search memories by text.

        Uses vector backend if available, falls back to text search.

        Args:
            query: Search query string.
            limit: Maximum results.
            tags: Optional tag filter (AND logic).
            layer: Optional layer filter ("short-term", "mid-term", "long-term").
            source: Optional source filter.

        Returns:
            list[Memory]: Matching memories ranked by relevance.
        """
        query = sanitize_query(query)
        if self.vector:
            try:
                results = self.vector.search_text(
                    query, limit=limit, layer=layer, tags=tags, source=source
                )
                if results:
                    return results
            except Exception as exc:
                logger.warning("Vector search failed, falling back to text search: %s", exc)

        return self.primary.search_text(query, limit=limit)

    def check_duplicate(
        self,
        content: str,
        threshold: float = 0.73,
        k: int = 5,
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

        return [c for c in candidates if c.get("similarity", 0.0) >= threshold]

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

    def forget(self, memory_id: str) -> bool:
        """Delete a memory from all backends and record a durable tombstone.

        The tombstone (see :mod:`skmemory.tombstones`) is what stops a later
        reconcile from resurrecting the memory when a stale flat copy reappears
        (Syncthing re-deliver, a second source path, or an ingest re-import).

        Args:
            memory_id: The memory to remove.

        Returns:
            bool: True if deleted from primary backend.
        """
        self._wal.log_pending("forget", memory_id, "", "")
        try:
            deleted = self.primary.delete(memory_id)
            self._wal.log_done("forget", memory_id)
        except Exception as exc:
            logger.warning("store.py: %s", exc)
            self._wal.log_failed("forget", memory_id, str(exc))
            raise

        # Resurrection guard (card 7d3e9fcc): record that this id was
        # deliberately forgotten so a future reconcile refuses to re-create it
        # from a stale flat copy. Best effort and non-fatal: the delete above
        # has already happened, so a marker that cannot be written must not turn
        # a successful forget into a failure.
        mem_dir = self._tombstone_mem_dir()
        if mem_dir:
            write_tombstone(mem_dir, memory_id, reason="forget")

        # Fan the removal out to the derived backends through the cascade
        # executor (best-effort, per-backend reporting). Two steps:
        #
        #  * vector.remove(id) - checked for presence: a vector backend that
        #    lacks remove() means a forget silently leaves its rows behind (they
        #    linger until reconcile), so surface that loudly instead of
        #    swallowing an AttributeError (Gap A: pgvector once shipped delete()
        #    but no remove()).
        #  * graph.remove_memory(id) - called directly; any failure (including a
        #    missing method) is caught and warned, never fatal.
        vname = type(self.vector).__name__ if self.vector else ""
        self._cascade.run(
            "forget",
            [
                CascadeStep(
                    role="vector",
                    backend=self.vector,
                    method="remove",
                    args=(memory_id,),
                    check_presence=True,
                    warn_missing=(
                        f"vector backend {vname} has no remove(); memory "
                        f"{memory_id} NOT removed from the vector store at "
                        f"forget time (lingers until reconcile); add a "
                        f"remove() to this backend"
                    ),
                    warn_fail=lambda e: f"vector backend {vname} remove({memory_id}) failed: {e}",
                ),
                CascadeStep(
                    role="graph",
                    backend=self.graph,
                    method="remove_memory",
                    args=(memory_id,),
                    check_presence=False,
                    warn_fail=lambda e: f"SKGraph remove failed: {e}",
                ),
            ],
        )

        return deleted

    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories with optional filtering.

        Args:
            layer: Filter by layer.
            tags: Filter by tags (AND logic).
            limit: Max results.

        Returns:
            list[Memory]: Matching memories sorted newest first.
        """
        return self.primary.list_memories(layer=layer, tags=tags, limit=limit)

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
        """
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

        Returns:
            dict: Token-efficient tiered context with metadata.
        """
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

    def export_backup(self, output_path: str | None = None) -> str:
        """Export all memories to a dated JSON backup.

        Args:
            output_path: Destination file. Defaults to
                ``~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json``.

        Returns:
            str: Path to the written backup file.

        Raises:
            RuntimeError: If the primary backend doesn't support export.
        """
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

    def reindex(self, force: bool = False) -> int:
        """Rebuild the SQLite index from JSON files.

        Only works if the primary backend is SQLiteBackend. By default,
        SQLite-only memories are exported to flat files first so they
        survive the rebuild; pass ``force=True`` to skip that safety step.

        Returns:
            int: Number of memories indexed, or -1 if not applicable.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.reindex(force=force)
        return -1

    def export_orphans_to_flat(self) -> dict:
        """Write any SQLite-only memories out as flat JSON files.

        Useful before a destructive operation, or after an import that
        wrote into SQLite without producing flat files. Returns
        ``{"exported", "skipped", "errors", "orphan_ids"}``.
        """
        if isinstance(self.primary, SQLiteBackend):
            return self.primary.export_orphans_to_flat()
        return {"exported": 0, "skipped": 0, "errors": 0, "orphan_ids": []}

    def health(self) -> dict:
        """Check health of all backends.

        Returns:
            dict: Combined health status.
        """
        status = {"primary": self.primary.health_check()}
        if self.vector:
            try:
                status["vector"] = self.vector.health_check()
            except Exception as e:
                logger.warning("store.py: %s", e)
                status["vector"] = {"ok": False, "error": str(e)}
        if self.graph:
            try:
                status["graph"] = self.graph.health_check()
            except Exception as e:
                logger.warning("store.py: %s", e)
                status["graph"] = {"ok": False, "error": str(e)}
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
