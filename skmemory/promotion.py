"""Memory auto-promotion engine -- sweep and promote by access and intensity.

Periodically evaluates memories and promotes qualifying ones to
higher persistence tiers:
  short-term -> mid-term: frequently accessed or emotionally intense
  mid-term -> long-term: deeply important or Cloud 9 related

Promotion generates a compressed summary for the new tier while
keeping the original intact as the detailed version.

Usage:
    engine = PromotionEngine(store)
    result = engine.sweep()  # evaluate and promote all qualifying memories
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .models import EmotionalSnapshot, Memory, MemoryLayer
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
    """

    def __init__(
        self,
        store: MemoryStore,
        criteria: Optional[PromotionCriteria] = None,
    ) -> None:
        self._store = store
        self._criteria = criteria or PromotionCriteria()

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

    def evaluate(self, memory: Memory) -> Optional[MemoryLayer]:
        """Evaluate whether a memory qualifies for promotion.

        Args:
            memory: The memory to evaluate.

        Returns:
            Optional[MemoryLayer]: Target tier if it qualifies, None otherwise.
        """
        if memory.layer == MemoryLayer.SHORT:
            if self._qualifies_short_to_mid(memory):
                return MemoryLayer.MID
        elif memory.layer == MemoryLayer.MID:
            if self._qualifies_mid_to_long(memory):
                return MemoryLayer.LONG
        return None

    def promote_memory(
        self,
        memory: Memory,
        target: MemoryLayer,
    ) -> Optional[Memory]:
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
            promoted.tags = list(set(promoted.tags + ["auto-promoted"]))
            promoted.metadata["promoted_from"] = memory.layer.value
            promoted.metadata["promoted_at"] = datetime.now(timezone.utc).isoformat()
            promoted.metadata["promotion_reason"] = self._promotion_reason(memory)
            self._store.primary.save(promoted)

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

        return False

    def _qualifies_mid_to_long(self, memory: Memory) -> bool:
        """Check if a mid-term memory qualifies for long-term.

        Args:
            memory: The memory to check.

        Returns:
            bool: True if it meets any promotion criterion.
        """
        c = self._criteria

        if memory.emotional.intensity >= c.mid_to_long_intensity:
            return True

        if any(tag in memory.tags for tag in c.mid_to_long_tags):
            return True

        if memory.emotional.cloud9_achieved and c.cloud9_auto_promote:
            return True

        return False

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

        return (
            f"{memory.title}: {content_preview} "
            f"[{emotional_sig}] [{tags_str}]"
        )

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

        return "; ".join(reasons) if reasons else "criteria met"
