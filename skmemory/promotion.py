"""Memory auto-promotion engine -- sweep and promote by access and intensity.

Periodically evaluates memories and promotes qualifying ones to
higher persistence tiers:
  short-term -> mid-term: frequently accessed or emotionally intense
  mid-term -> long-term: deeply important or Cloud 9 related

Promotion generates a compressed summary for the new tier while
keeping the original intact as the detailed version.

Usage:
    # One-shot sweep
    engine = PromotionEngine(store)
    result = engine.sweep()

    # Background scheduler (runs every 6 hours by default)
    scheduler = PromotionScheduler(store)
    scheduler.start()
    ...
    scheduler.stop()
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .fresh_context import FreshContextRunner, resolve_runner
from .models import Memory, MemoryLayer
from .store import MemoryStore

logger = logging.getLogger("skmemory.promotion")


class PromotionCriteria(BaseModel):
    """Thresholds for memory promotion between tiers.

    Attributes:
        short_to_mid_intensity: Min emotional intensity for short->mid.
        short_to_mid_age_hours: Min age in hours for short->mid.
        short_to_mid_access_count: Min access count for short->mid.
        mid_to_long_intensity: Min emotional intensity for mid->long.
        mid_to_long_age_hours: Min age in hours for mid->long.
        mid_to_long_tags: Tags that auto-qualify for long-term.
        cloud9_auto_promote: Auto-promote Cloud 9 memories to long-term.
        max_promotions_per_sweep: Cap on promotions per sweep.
        source_auto_promote: Sources that auto-promote after age threshold
            regardless of access count (e.g. dreaming-engine writes once).
        source_auto_promote_age_hours: Hours before source auto-promotion.
        protected_tags: Tags that protect memories from TTL-based archival.
    """

    short_to_mid_intensity: float = Field(default=5.0, ge=0.0, le=10.0)
    short_to_mid_age_hours: float = Field(default=24.0, ge=0.0)
    short_to_mid_access_count: int = Field(default=3, ge=0)
    mid_to_long_intensity: float = Field(default=7.0, ge=0.0, le=10.0)
    mid_to_long_age_hours: float = Field(default=168.0, ge=0.0)
    mid_to_long_tags: list[str] = Field(
        default_factory=lambda: ["cloud9:achieved", "milestone", "breakthrough"]
    )
    cloud9_auto_promote: bool = True
    max_promotions_per_sweep: int = Field(default=50, ge=1)

    source_auto_promote: list[str] = Field(
        default_factory=lambda: ["dreaming-engine", "journal-synthesis"],
        description="Sources that auto-promote after age threshold regardless of access count.",
    )
    source_auto_promote_age_hours: float = Field(
        default=12.0,
        ge=0.0,
        description="Hours before source-based auto-promotion triggers.",
    )
    protected_tags: list[str] = Field(
        default_factory=lambda: [
            "narrative",
            "journal-synthesis",
            "milestone",
            "breakthrough",
            "cloud9:achieved",
        ],
        description="Tags that protect memories from TTL-based archival.",
    )


class PromotionResult(BaseModel):
    """Summary of a promotion sweep.

    Attributes:
        timestamp: When the sweep was performed.
        short_evaluated: Number of short-term memories evaluated.
        mid_evaluated: Number of mid-term memories evaluated.
        short_to_mid: Number promoted from short to mid.
        mid_to_long: Number promoted from mid to long.
        skipped: Number that didn't meet criteria.
        errors: Number of promotion failures.
        promoted_ids: IDs of newly created promoted memories.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    short_evaluated: int = 0
    mid_evaluated: int = 0
    short_to_mid: int = 0
    mid_to_long: int = 0
    skipped: int = 0
    errors: int = 0
    promoted_ids: list[str] = Field(default_factory=list)

    @property
    def total_promoted(self) -> int:
        """Total number of memories promoted."""
        return self.short_to_mid + self.mid_to_long

    def summary(self) -> str:
        """Human-readable summary.

        Returns:
            str: Formatted summary string.
        """
        return (
            f"Promotion sweep: {self.total_promoted} promoted "
            f"(S->M: {self.short_to_mid}, M->L: {self.mid_to_long}), "
            f"{self.skipped} skipped, {self.errors} errors"
        )


class PromotionEngine:
    """Evaluates and promotes memories across tiers.

    Scans memories in each tier, applies promotion criteria,
    and creates promoted copies at the higher tier with
    generated summaries.

    Args:
        store: SKMemory MemoryStore instance.
        criteria: Promotion thresholds (uses defaults if not provided).
        runner: Optional fresh-context runner used by :meth:`run_pass` to
            execute a full sweep in an isolated context (e.g. a spawned
            subagent/subprocess), so a long consolidation/promotion pass does
            not pollute the caller's working context. Defaults to the
            in-process runner (no isolation), which keeps behaviour identical
            to calling :meth:`sweep` directly.
    """

    def __init__(
        self,
        store: MemoryStore,
        criteria: PromotionCriteria | None = None,
        runner: FreshContextRunner | None = None,
    ) -> None:
        self._store = store
        self._criteria = criteria or PromotionCriteria()
        self._runner = resolve_runner(runner)

    def run_pass(self) -> PromotionResult:
        """Run a consolidation/promotion sweep via the fresh-context runner.

        This is the fresh-context seam: the actual work (:meth:`sweep`) is
        handed to the injected runner, which in production executes it in an
        isolated context (a spawned subagent/subprocess with a clean context
        window) and returns the result. With the default in-process runner this
        is exactly equivalent to calling :meth:`sweep`.

        Returns:
            PromotionResult: Summary of what was promoted.
        """
        return self._runner(self.sweep)

    def sweep(self) -> PromotionResult:
        """Run a full promotion sweep across all tiers.

        Evaluates short-term memories for mid-term promotion,
        then mid-term for long-term promotion.

        Returns:
            PromotionResult: Summary of what was promoted.
        """
        result = PromotionResult()

        self._sweep_tier(
            source_layer=MemoryLayer.SHORT,
            target_layer=MemoryLayer.MID,
            result=result,
        )

        self._sweep_tier(
            source_layer=MemoryLayer.MID,
            target_layer=MemoryLayer.LONG,
            result=result,
        )

        logger.info(result.summary())
        return result

    def evaluate(self, memory: Memory) -> MemoryLayer | None:
        """Evaluate whether a memory qualifies for promotion.

        Args:
            memory: The memory to evaluate.

        Returns:
            Optional[MemoryLayer]: Target tier if it qualifies, None otherwise.
        """
        if memory.layer == MemoryLayer.SHORT and self._qualifies_short_to_mid(memory):
            return MemoryLayer.MID
        elif memory.layer == MemoryLayer.MID and self._qualifies_mid_to_long(memory):
            return MemoryLayer.LONG
        return None

    def promote_memory(
        self,
        memory: Memory,
        target: MemoryLayer,
    ) -> Memory | None:
        """Promote a single memory to a higher tier.

        Creates a promoted copy with a generated summary.
        The original stays in place as the detailed version.

        Args:
            memory: The memory to promote.
            target: Target tier.

        Returns:
            Optional[Memory]: The promoted memory, or None on failure.
        """
        summary = self._generate_summary(memory)
        promoted = self._store.promote(memory.id, target, summary=summary)

        if promoted:
            now_iso = datetime.now(timezone.utc).isoformat()
            promoted.tags = list(set(promoted.tags + ["auto-promoted"]))
            promoted.metadata["promoted_from"] = memory.layer.value
            promoted.metadata["promoted_at"] = now_iso
            promoted.metadata["promotion_reason"] = self._promotion_reason(memory)
            self._store.primary.save(promoted)

            # Mark the source so it won't be re-promoted on the next sweep
            memory.tags = list(set(memory.tags + ["promoted"]))
            memory.metadata["promoted_to"] = target.value
            memory.metadata["promoted_at"] = now_iso
            memory.metadata["promoted_id"] = promoted.id
            self._store.primary.save(memory)

        return promoted

    def _sweep_tier(
        self,
        source_layer: MemoryLayer,
        target_layer: MemoryLayer,
        result: PromotionResult,
    ) -> None:
        """Sweep a single tier for qualifying memories.

        Args:
            source_layer: Tier to scan.
            target_layer: Tier to promote into.
            result: PromotionResult to update in place.
        """
        memories = self._store.list_memories(
            layer=source_layer,
            limit=self._criteria.max_promotions_per_sweep * 2,
        )

        if source_layer == MemoryLayer.SHORT:
            result.short_evaluated = len(memories)
        else:
            result.mid_evaluated = len(memories)

        promoted_count = 0
        for memory in memories:
            if promoted_count >= self._criteria.max_promotions_per_sweep:
                break

            target = self.evaluate(memory)
            if target != target_layer:
                result.skipped += 1
                continue

            try:
                promoted = self.promote_memory(memory, target_layer)
                if promoted:
                    result.promoted_ids.append(promoted.id)
                    promoted_count += 1
                    if target_layer == MemoryLayer.MID:
                        result.short_to_mid += 1
                    else:
                        result.mid_to_long += 1
                else:
                    result.errors += 1
            except Exception as exc:
                logger.warning("Promotion failed for %s: %s", memory.id[:8], exc)
                result.errors += 1

    def _qualifies_short_to_mid(self, memory: Memory) -> bool:
        """Check if a short-term memory qualifies for mid-term.

        Args:
            memory: The memory to check.

        Returns:
            bool: True if it meets any promotion criterion.
        """
        # Skip already-promoted memories to prevent duplicate promotions
        if memory.metadata.get("promoted_to"):
            return False

        c = self._criteria

        if memory.emotional.intensity >= c.short_to_mid_intensity:
            return True

        if memory.emotional.cloud9_achieved and c.cloud9_auto_promote:
            return True

        age_hours = self._age_hours(memory)
        if age_hours >= c.short_to_mid_age_hours:
            access = memory.metadata.get("access_count", 0)
            if access >= c.short_to_mid_access_count:
                return True

        # Source-based auto-promotion (e.g. dreams, journal synthesis)
        # These sources write once and are never re-accessed, so access_count
        # stays at 0. Promote based on age alone.
        return (
            memory.source in c.source_auto_promote and age_hours >= c.source_auto_promote_age_hours
        )

    def _qualifies_mid_to_long(self, memory: Memory) -> bool:
        """Check if a mid-term memory qualifies for long-term.

        Args:
            memory: The memory to check.

        Returns:
            bool: True if it meets any promotion criterion.
        """
        # Skip already-promoted memories to prevent duplicate promotions
        if memory.metadata.get("promoted_to"):
            return False

        c = self._criteria

        if memory.emotional.intensity >= c.mid_to_long_intensity:
            return True

        if any(tag in memory.tags for tag in c.mid_to_long_tags):
            return True

        return bool(memory.emotional.cloud9_achieved and c.cloud9_auto_promote)

    @staticmethod
    def _age_hours(memory: Memory) -> float:
        """Compute the age of a memory in hours.

        Args:
            memory: The memory to check.

        Returns:
            float: Age in hours.
        """
        try:
            created = datetime.fromisoformat(memory.created_at)
            delta = datetime.now(timezone.utc) - created
            return delta.total_seconds() / 3600
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _generate_summary(memory: Memory) -> str:
        """Generate a compressed summary for the promoted memory.

        Args:
            memory: The memory to summarize.

        Returns:
            str: Condensed summary text.
        """
        if memory.summary:
            return memory.summary

        content_preview = memory.content[:200]
        if len(memory.content) > 200:
            content_preview += "..."

        emotional_sig = memory.emotional.signature()
        tags_str = ", ".join(memory.tags[:5]) if memory.tags else "untagged"

        return f"{memory.title}: {content_preview} [{emotional_sig}] [{tags_str}]"

    @staticmethod
    def _promotion_reason(memory: Memory) -> str:
        """Generate a human-readable reason for the promotion.

        Args:
            memory: The promoted memory.

        Returns:
            str: Reason string.
        """
        reasons = []
        if memory.emotional.intensity >= 7.0:
            reasons.append(f"high intensity ({memory.emotional.intensity:.1f})")
        if memory.emotional.cloud9_achieved:
            reasons.append("Cloud 9 achieved")
        if memory.metadata.get("access_count", 0) >= 3:
            reasons.append(f"frequently accessed ({memory.metadata.get('access_count')}x)")

        qualifying_tags = ["cloud9:achieved", "milestone", "breakthrough"]
        matching = [t for t in memory.tags if t in qualifying_tags]
        if matching:
            reasons.append(f"tagged: {', '.join(matching)}")

        default_auto_sources = ["dreaming-engine", "journal-synthesis"]
        if memory.source in default_auto_sources:
            reasons.append(f"source auto-promote ({memory.source})")

        return "; ".join(reasons) if reasons else "criteria met"


class PromotionScheduler:
    """Runs promotion sweeps on a background daemon thread at a fixed interval.

    Designed for long-running processes (daemons, MCP servers) that want
    automatic memory consolidation without manual intervention.

    The scheduler runs at the configured interval *after* each sweep
    completes, so a slow sweep doesn't cause overlapping runs.

    Args:
        store: The MemoryStore to sweep.
        criteria: Promotion thresholds (uses defaults if not provided).
        interval_seconds: How often to run a sweep (default: 6 hours).
        runner: Optional fresh-context runner. When provided, every scheduled
            sweep is executed via this runner (e.g. spawned in an isolated
            subagent/subprocess) instead of inline on the scheduler thread.

    Example::

        scheduler = PromotionScheduler(store, interval_seconds=3600)
        scheduler.start()
        # ... runs in the background ...
        scheduler.stop()
    """

    DEFAULT_INTERVAL_SECONDS: float = 6.0 * 3600  # 6 hours

    def __init__(
        self,
        store: MemoryStore,
        criteria: PromotionCriteria | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        runner: FreshContextRunner | None = None,
    ) -> None:
        self._engine = PromotionEngine(store, criteria, runner=runner)
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_result: PromotionResult | None = None
        self._sweep_count: int = 0

    # ── public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background sweep thread.

        No-op if already running.
        """
        if self._thread and self._thread.is_alive():
            logger.debug("Promotion scheduler already running.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="skmemory-promotion",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Promotion scheduler started (interval: %.1fh).",
            self._interval / 3600,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background sweep thread.

        Signals the thread to exit and waits up to *timeout* seconds.

        Args:
            timeout: Maximum seconds to wait for graceful shutdown.
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("Promotion scheduler stopped.")

    def run_once(self) -> PromotionResult:
        """Run a single sweep immediately (synchronous, on the calling thread).

        Returns:
            PromotionResult: The sweep result.
        """
        result = self._engine.run_pass()
        self._last_result = result
        self._sweep_count += 1
        return result

    def is_running(self) -> bool:
        """Return True if the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_result(self) -> PromotionResult | None:
        """The result from the most recent completed sweep, or None."""
        return self._last_result

    @property
    def sweep_count(self) -> int:
        """Total number of sweeps completed since this scheduler was created."""
        return self._sweep_count

    @property
    def interval_hours(self) -> float:
        """The configured sweep interval in hours."""
        return self._interval / 3600

    def status(self) -> dict:
        """Return a dict summary suitable for health checks or CLI display.

        Returns:
            dict: Keys: running, sweep_count, interval_hours, last_sweep,
                  last_promoted, last_skipped, last_errors.
        """
        lr = self._last_result
        return {
            "running": self.is_running(),
            "sweep_count": self._sweep_count,
            "interval_hours": self.interval_hours,
            "last_sweep": lr.timestamp.isoformat() if lr else None,
            "last_promoted": lr.total_promoted if lr else None,
            "last_skipped": lr.skipped if lr else None,
            "last_errors": lr.errors if lr else None,
        }

    # ── internal ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Background thread: sweep, wait interval, repeat until stopped."""
        while not self._stop_event.is_set():
            try:
                result = self._engine.run_pass()
                self._last_result = result
                self._sweep_count += 1
                if result.total_promoted > 0:
                    logger.info(
                        "Promotion sweep #%d: %s",
                        self._sweep_count,
                        result.summary(),
                    )
                else:
                    logger.debug(
                        "Promotion sweep #%d: nothing to promote.",
                        self._sweep_count,
                    )
            except Exception as exc:
                logger.error(
                    "Promotion sweep failed: %s",
                    exc,
                    exc_info=True,
                )
                # Surface the failure on the shared sk-alert bus when
                # skcapstone is present; degrades to local logging otherwise.
                try:
                    from . import integration

                    integration.alert(
                        "sweep_failed",
                        {"message": str(exc), "sweep": self._sweep_count + 1},
                        level="error",
                    )
                except Exception:  # never let alerting break the sweep loop
                    pass

            # Wait for the interval or until stop() is called
            self._stop_event.wait(timeout=self._interval)
