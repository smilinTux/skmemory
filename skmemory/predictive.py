"""
Predictive Memory Recall — anticipate what memories you'll need.

Inspired by Jonathan Clements' Adaptive Memory Kernel (AMK).
Instead of waiting for a search query, this module learns access
patterns and pre-loads the memories most likely to be relevant
for the current context.

The predictor tracks:
  - Which memories are accessed together (co-occurrence)
  - Time-of-day patterns (morning routines vs late-night deep work)
  - Tag affinity (if you access 'cloud9' memories, you probably want 'trust' too)
  - Recency-weighted frequency (recent access patterns matter more)

The output is a ranked list of memory IDs to pre-load into context,
sorted by predicted relevance. This feeds directly into the
`skmemory context` and `skmemory ritual` commands.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .config import SKMEMORY_HOME

logger = logging.getLogger("skmemory.predictive")

DEFAULT_ACCESS_LOG = SKMEMORY_HOME / "access_log.json"


class AccessEvent(BaseModel):
    """A single memory access event for pattern learning."""

    memory_id: str
    timestamp: float = Field(default_factory=time.time)
    tags: list[str] = Field(default_factory=list)
    layer: str = ""
    context: str = Field(
        default="",
        description="What was happening when this memory was accessed",
    )


class PredictiveRecall:
    """Learns memory access patterns and predicts what you'll need next.

    Tracks co-occurrence (which memories are accessed together),
    tag affinity, and temporal patterns to generate ranked predictions.

    Args:
        log_path: Path to the access log JSON file.
        max_events: Maximum events to retain (older events are pruned).
    """

    def __init__(
        self,
        log_path: Optional[Path] = None,
        max_events: int = 5000,
    ) -> None:
        self._log_path = log_path or DEFAULT_ACCESS_LOG
        self._max_events = max_events
        self._events: list[AccessEvent] = []
        self._cooccurrence: dict[str, Counter] = defaultdict(Counter)
        self._tag_affinity: dict[str, Counter] = defaultdict(Counter)
        self._frequency: Counter = Counter()
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Load the access log from disk if not already loaded."""
        if self._loaded:
            return
        self._loaded = True

        if not self._log_path.exists():
            return

        try:
            raw = json.loads(self._log_path.read_text())
            self._events = [AccessEvent(**e) for e in raw]
            self._rebuild_indices()
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to load access log: %s", exc)

    def _rebuild_indices(self) -> None:
        """Rebuild co-occurrence, tag affinity, and frequency indices."""
        self._cooccurrence.clear()
        self._tag_affinity.clear()
        self._frequency.clear()

        session_window = 300
        sessions: list[list[AccessEvent]] = []
        current_session: list[AccessEvent] = []

        for event in sorted(self._events, key=lambda e: e.timestamp):
            if current_session and (event.timestamp - current_session[-1].timestamp) > session_window:
                sessions.append(current_session)
                current_session = []
            current_session.append(event)
        if current_session:
            sessions.append(current_session)

        for session in sessions:
            ids_in_session = [e.memory_id for e in session]
            for i, mid in enumerate(ids_in_session):
                self._frequency[mid] += 1
                for other in ids_in_session[i + 1:]:
                    if other != mid:
                        self._cooccurrence[mid][other] += 1
                        self._cooccurrence[other][mid] += 1

        for event in self._events:
            for tag in event.tags:
                self._tag_affinity[tag][event.memory_id] += 1

    def log_access(self, memory_id: str, tags: Optional[list[str]] = None, layer: str = "", context: str = "") -> None:
        """Record a memory access event for pattern learning.

        Args:
            memory_id: The accessed memory's ID.
            tags: Tags on the accessed memory.
            layer: Memory layer (short-term, mid-term, long-term).
            context: What was happening during access.
        """
        self._ensure_loaded()

        event = AccessEvent(
            memory_id=memory_id,
            tags=tags or [],
            layer=layer,
            context=context,
        )
        self._events.append(event)

        self._frequency[memory_id] += 1
        for tag in event.tags:
            self._tag_affinity[tag][memory_id] += 1

        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
            self._rebuild_indices()

        self._save()

    def predict(
        self,
        recent_ids: Optional[list[str]] = None,
        active_tags: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Predict which memories will be needed next.

        Uses co-occurrence patterns, tag affinity, and recency-weighted
        frequency to rank memory IDs by predicted relevance.

        Args:
            recent_ids: Memory IDs accessed in the current session.
            active_tags: Tags active in the current context.
            limit: Maximum predictions to return.

        Returns:
            list[dict]: Ranked predictions with id, score, and reason.
        """
        self._ensure_loaded()

        scores: Counter = Counter()
        reasons: dict[str, list[str]] = defaultdict(list)

        if recent_ids:
            for mid in recent_ids:
                for co_id, count in self._cooccurrence.get(mid, {}).items():
                    if co_id not in recent_ids:
                        scores[co_id] += count * 2.0
                        reasons[co_id].append(f"co-occurs with {mid[:8]}")

        if active_tags:
            for tag in active_tags:
                for mid, count in self._tag_affinity.get(tag, {}).items():
                    if not recent_ids or mid not in recent_ids:
                        scores[mid] += count * 1.5
                        reasons[mid].append(f"tag affinity: {tag}")

        now = time.time()
        for mid, freq in self._frequency.items():
            if not recent_ids or mid not in recent_ids:
                last_access = max(
                    (e.timestamp for e in self._events if e.memory_id == mid),
                    default=0,
                )
                recency = math.exp(-(now - last_access) / 86400) if last_access else 0
                recency_score = freq * recency * 0.5
                if recency_score > 0.1:
                    scores[mid] += recency_score
                    reasons[mid].append(f"frequency={freq}, recency={recency:.2f}")

        ranked = scores.most_common(limit)
        return [
            {
                "memory_id": mid,
                "score": round(score, 2),
                "reasons": reasons.get(mid, []),
            }
            for mid, score in ranked
        ]

    def get_stats(self) -> dict:
        """Return statistics about the prediction engine.

        Returns:
            dict: Event count, unique memories, top accessed, etc.
        """
        self._ensure_loaded()
        return {
            "total_events": len(self._events),
            "unique_memories": len(self._frequency),
            "top_accessed": self._frequency.most_common(5),
            "unique_tags": len(self._tag_affinity),
            "cooccurrence_pairs": sum(len(v) for v in self._cooccurrence.values()),
        }

    def _save(self) -> None:
        """Persist the access log to disk."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.model_dump() for e in self._events[-self._max_events:]]
        self._log_path.write_text(json.dumps(data, indent=2))
