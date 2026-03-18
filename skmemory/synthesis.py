"""Journal synthesis — turn raw memories and journal entries into curated narratives.

No LLM dependency. Uses tag frequency analysis, first-sentence extraction,
emotional intensity aggregation, and template-based narrative generation.

Usage:
    synthesizer = JournalSynthesizer(store, journal)
    daily = synthesizer.synthesize_daily("2026-03-16")
    weekly = synthesizer.synthesize_weekly("2026-W11")
    dreams = synthesizer.synthesize_dreams(since="2026-03-14")
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .journal import Journal
from .models import EmotionalSnapshot, Memory, MemoryLayer, MemoryRole
from .store import MemoryStore

logger = logging.getLogger("skmemory.synthesis")


def _first_n_sentences(text: str, n: int = 2) -> str:
    """Extract the first N sentences from text, capped at 200 chars."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    result = " ".join(sentences[:n])
    if len(result) > 200:
        result = result[:197] + "..."
    return result


def _date_range(date_str: str) -> tuple[datetime, datetime]:
    """Parse a YYYY-MM-DD string into (start_of_day, end_of_day) UTC datetimes."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt, dt + timedelta(days=1)


def _week_range(week_str: str) -> tuple[datetime, datetime]:
    """Parse a YYYY-Www string into (monday, next_monday) UTC datetimes."""
    # e.g. "2026-W11"
    dt = datetime.strptime(week_str + "-1", "%G-W%V-%u").replace(tzinfo=timezone.utc)
    return dt, dt + timedelta(weeks=1)


class JournalSynthesizer:
    """Create narrative memories from daily activity, journal entries, and dreams.

    All synthesis is deterministic — no LLM calls. Uses:
    - Tag frequency for theme extraction
    - First-sentence extraction for summaries
    - Emotional intensity aggregation for arc detection
    - Template-based narrative generation

    Args:
        store: The MemoryStore to read from and write to.
        journal: The Journal instance for reading entries.
        dream_log_path: Path to dream-log.json (optional).
        themes_path: Path to graduated-themes.json (optional).
    """

    def __init__(
        self,
        store: MemoryStore,
        journal: Journal | None = None,
        dream_log_path: str | None = None,
        themes_path: str | None = None,
    ) -> None:
        self.store = store
        self.journal = journal or Journal()
        self._dream_log_path = Path(dream_log_path) if dream_log_path else None
        self._themes_path = Path(themes_path) if themes_path else None
        self._graduated_themes: dict | None = None

    @property
    def graduated_themes(self) -> dict:
        """Load graduated-themes.json on first access."""
        if self._graduated_themes is None:
            if self._themes_path and self._themes_path.exists():
                try:
                    self._graduated_themes = json.loads(
                        self._themes_path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    self._graduated_themes = {}
            else:
                self._graduated_themes = {}
        return self._graduated_themes

    def synthesize_daily(self, date: str | None = None) -> Memory:
        """Create a narrative memory from one day's activity.

        Reads today's memories and journal entries, extracts themes and
        emotional arc, and stores a single mid-term narrative memory.

        Args:
            date: Date string (YYYY-MM-DD). Defaults to today.

        Returns:
            Memory: The created narrative memory.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        start, end = _date_range(date)

        # Gather memories from this date
        all_memories = self.store.list_memories(limit=500)
        day_memories = [m for m in all_memories if start <= _parse_created(m) < end]

        # Gather journal entries for this date
        journal_matches = self.journal.search(date) if self.journal else []

        # Extract themes
        themes = self.extract_themes(day_memories)

        # Build emotional arc
        arc = self._emotional_arc(day_memories)

        # Build narrative
        narrative = self._build_daily_narrative(date, day_memories, themes, arc, journal_matches)

        # Create the synthesis memory
        avg_intensity = arc.get("avg_intensity", 0.0)
        avg_valence = arc.get("avg_valence", 0.0)
        all_labels = arc.get("top_emotions", [])

        memory = self.store.snapshot(
            title=f"Daily Narrative: {date}",
            content=narrative,
            layer=MemoryLayer.MID,
            role=MemoryRole.AI,
            tags=["narrative", "journal-synthesis", f"daily-{date}"] + themes[:3],
            emotional=EmotionalSnapshot(
                intensity=min(avg_intensity, 10.0),
                valence=max(-1.0, min(1.0, avg_valence)),
                labels=all_labels[:5],
            ),
            source="journal-synthesis",
            source_ref=f"daily-{date}",
            related_ids=[m.id for m in day_memories[:20]],
            metadata={
                "synthesis_type": "daily",
                "date": date,
                "memory_count": len(day_memories),
                "themes": themes,
            },
        )

        logger.info(
            "Daily synthesis for %s: %d memories → %d themes",
            date,
            len(day_memories),
            len(themes),
        )
        return memory

    def synthesize_weekly(self, week: str | None = None) -> Memory:
        """Create a weekly narrative from daily synthesis memories.

        Args:
            week: ISO week string (YYYY-Www). Defaults to current week.

        Returns:
            Memory: The created long-term narrative memory.
        """
        if week is None:
            now = datetime.now(timezone.utc)
            week = now.strftime("%G-W%V")

        start, end = _week_range(week)

        # Find daily synthesis memories for this week
        all_mid = self.store.list_memories(
            layer=MemoryLayer.MID,
            tags=["journal-synthesis"],
            limit=100,
        )
        weekly_dailies = [
            m for m in all_mid if start <= _parse_created(m) < end and "narrative" in m.tags
        ]

        # Also gather all memories from the week for theme extraction
        all_memories = self.store.list_memories(limit=1000)
        week_memories = [m for m in all_memories if start <= _parse_created(m) < end]

        themes = self.extract_themes(week_memories)
        arc = self._emotional_arc(week_memories)

        narrative = self._build_weekly_narrative(week, weekly_dailies, week_memories, themes, arc)

        avg_intensity = arc.get("avg_intensity", 0.0)
        avg_valence = arc.get("avg_valence", 0.0)

        memory = self.store.snapshot(
            title=f"Weekly Narrative: {week}",
            content=narrative,
            layer=MemoryLayer.LONG,
            role=MemoryRole.AI,
            tags=["narrative", "journal-synthesis", f"weekly-{week}"] + themes[:3],
            emotional=EmotionalSnapshot(
                intensity=min(avg_intensity, 10.0),
                valence=max(-1.0, min(1.0, avg_valence)),
                labels=arc.get("top_emotions", [])[:5],
            ),
            source="journal-synthesis",
            source_ref=f"weekly-{week}",
            related_ids=[m.id for m in weekly_dailies[:20]],
            metadata={
                "synthesis_type": "weekly",
                "week": week,
                "daily_count": len(weekly_dailies),
                "total_memories": len(week_memories),
                "themes": themes,
            },
        )

        logger.info(
            "Weekly synthesis for %s: %d dailies, %d total memories → %d themes",
            week,
            len(weekly_dailies),
            len(week_memories),
            len(themes),
        )
        return memory

    def synthesize_dreams(self, since: str | None = None) -> list[Memory]:
        """Process dream memories into curated narrative memories grouped by theme.

        Reads all dream-source memories since the given date, groups by
        theme, and creates one mid-term memory per theme cluster.

        Args:
            since: Only process dreams created after this date (YYYY-MM-DD).
                Defaults to 7 days ago.

        Returns:
            list[Memory]: One narrative memory per theme cluster.
        """
        if since is None:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

        cutoff = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        # Gather dream memories
        all_memories = self.store.list_memories(limit=1000)
        dream_memories = [
            m
            for m in all_memories
            if m.source == "dreaming-engine" and _parse_created(m) >= cutoff
        ]

        if not dream_memories:
            logger.info("No dream memories found since %s", since)
            return []

        # Group by theme using tags and graduated themes
        theme_clusters = self._cluster_by_theme(dream_memories)
        results: list[Memory] = []

        for theme_name, cluster in theme_clusters.items():
            narrative = self._build_dream_narrative(theme_name, cluster)
            arc = self._emotional_arc(cluster)
            avg_intensity = arc.get("avg_intensity", 0.0)

            memory = self.store.snapshot(
                title=f"Dream Synthesis: {theme_name}",
                content=narrative,
                layer=MemoryLayer.MID,
                role=MemoryRole.AI,
                tags=["dream-synthesis", "narrative", theme_name],
                emotional=EmotionalSnapshot(
                    intensity=min(avg_intensity, 10.0),
                    valence=arc.get("avg_valence", 0.0),
                    labels=arc.get("top_emotions", [])[:5],
                ),
                source="journal-synthesis",
                source_ref=f"dream-synthesis-{theme_name}",
                related_ids=[m.id for m in cluster[:20]],
                metadata={
                    "synthesis_type": "dream",
                    "theme": theme_name,
                    "dream_count": len(cluster),
                    "since": since,
                },
            )
            results.append(memory)

        logger.info(
            "Dream synthesis since %s: %d dreams → %d theme clusters",
            since,
            len(dream_memories),
            len(results),
        )
        return results

    def extract_themes(self, memories: list[Memory]) -> list[str]:
        """Extract recurring themes from a set of memories.

        Uses tag frequency and title keyword extraction, cross-referenced
        with graduated-themes.json when available.

        Args:
            memories: The memories to analyze.

        Returns:
            list[str]: Top theme strings, most frequent first.
        """
        if not memories:
            return []

        # Count tag frequency (skip generic tags)
        skip_tags = {
            "auto-promoted",
            "promoted",
            "consolidated",
            "seed",
            "cloud9",
            "short-term",
            "mid-term",
            "long-term",
            "maintenance",
            "memory-cleanup",
            "memory-optimization",
        }
        tag_counter: Counter[str] = Counter()
        for m in memories:
            for tag in m.tags:
                if tag not in skip_tags and not tag.startswith("session:"):
                    tag_counter[tag] += 1

        # Extract keywords from titles
        stop_words = {
            "the",
            "a",
            "an",
            "is",
            "was",
            "are",
            "were",
            "been",
            "be",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "shall",
            "can",
            "need",
            "dare",
            "ought",
            "used",
            "to",
            "of",
            "in",
            "for",
            "on",
            "with",
            "at",
            "by",
            "from",
            "as",
            "into",
            "through",
            "during",
            "before",
            "after",
            "above",
            "below",
            "between",
            "out",
            "off",
            "over",
            "under",
            "again",
            "further",
            "then",
            "once",
            "and",
            "but",
            "or",
            "nor",
            "not",
            "so",
            "yet",
            "both",
            "either",
            "neither",
            "each",
            "every",
            "all",
            "any",
            "few",
            "more",
            "most",
            "other",
            "some",
            "such",
            "no",
            "only",
            "own",
            "same",
            "than",
            "too",
            "very",
            "just",
            "because",
            "session",
            "daily",
            "weekly",
            "memory",
            "narrative",
            "synthesis",
        }
        word_counter: Counter[str] = Counter()
        for m in memories:
            words = re.findall(r"[a-zA-Z]{3,}", m.title.lower())
            for word in words:
                if word not in stop_words:
                    word_counter[word] += 1

        # Merge: tags count double
        combined: Counter[str] = Counter()
        for tag, count in tag_counter.items():
            combined[tag] += count * 2
        for word, count in word_counter.items():
            combined[word] += count

        # Cross-reference with graduated themes
        graduated = self.graduated_themes
        if graduated:
            for theme_name in graduated:
                normalized = theme_name.lower().replace("-", " ").replace("_", " ")
                for key in combined:
                    if key in normalized or normalized in key:
                        combined[key] += 3  # boost graduated themes

        # Return top themes
        return [theme for theme, _ in combined.most_common(10)]

    # ── Internal helpers ─────────────────────────────────────────────────

    def _emotional_arc(self, memories: list[Memory]) -> dict:
        """Compute aggregate emotional statistics."""
        if not memories:
            return {
                "avg_intensity": 0.0,
                "avg_valence": 0.0,
                "peak_intensity": 0.0,
                "top_emotions": [],
                "cloud9_count": 0,
            }

        intensities = [m.emotional.intensity for m in memories]
        valences = [m.emotional.valence for m in memories]
        label_counter: Counter[str] = Counter()
        cloud9_count = 0

        for m in memories:
            for label in m.emotional.labels:
                label_counter[label] += 1
            if m.emotional.cloud9_achieved:
                cloud9_count += 1

        return {
            "avg_intensity": sum(intensities) / len(intensities),
            "avg_valence": sum(valences) / len(valences),
            "peak_intensity": max(intensities),
            "top_emotions": [e for e, _ in label_counter.most_common(5)],
            "cloud9_count": cloud9_count,
        }

    def _cluster_by_theme(self, memories: list[Memory]) -> dict[str, list[Memory]]:
        """Group memories by their most prominent theme tag."""
        theme_map: dict[str, list[Memory]] = {}
        skip_tags = {"dream", "bulk-promoted", "rescued", "auto-promoted", "promoted"}

        for m in memories:
            # Find first meaningful tag as theme key
            theme = "uncategorized"
            for tag in m.tags:
                if tag not in skip_tags and not tag.startswith("session:"):
                    theme = tag
                    break
            theme_map.setdefault(theme, []).append(m)

        return theme_map

    def _build_daily_narrative(
        self,
        date: str,
        memories: list[Memory],
        themes: list[str],
        arc: dict,
        journal_entries: list[str],
    ) -> str:
        """Build a daily narrative from template."""
        parts = [f"Daily narrative for {date}."]

        if not memories:
            parts.append("No memories recorded this day.")
            return "\n\n".join(parts)

        parts.append(
            f"{len(memories)} memories across themes: {', '.join(themes[:5]) or 'none detected'}."
        )

        # Emotional summary
        avg_i = arc.get("avg_intensity", 0.0)
        peak = arc.get("peak_intensity", 0.0)
        c9 = arc.get("cloud9_count", 0)
        top_e = arc.get("top_emotions", [])

        intensity_word = (
            "quiet"
            if avg_i < 3
            else "moderate"
            if avg_i < 6
            else "intense"
            if avg_i < 8
            else "extraordinary"
        )
        parts.append(
            f"Emotional arc: {intensity_word} day (avg {avg_i:.1f}/10, peak {peak:.1f}/10)."
        )
        if top_e:
            parts.append(f"Dominant feelings: {', '.join(top_e[:3])}.")
        if c9:
            parts.append(f"Cloud 9 achieved {c9} time{'s' if c9 > 1 else ''}.")

        # Key moments (first sentence of top-intensity memories)
        ranked = sorted(memories, key=lambda m: m.emotional.intensity, reverse=True)
        key_moments = []
        for m in ranked[:5]:
            summary = _first_n_sentences(m.content, 1)
            if summary:
                key_moments.append(f"- {m.title}: {summary}")
        if key_moments:
            parts.append("Key moments:\n" + "\n".join(key_moments))

        # Journal excerpts
        if journal_entries:
            parts.append(f"Journal entries found: {len(journal_entries)}.")

        return "\n\n".join(parts)

    def _build_weekly_narrative(
        self,
        week: str,
        dailies: list[Memory],
        all_memories: list[Memory],
        themes: list[str],
        arc: dict,
    ) -> str:
        """Build a weekly narrative from template."""
        parts = [f"Weekly narrative for {week}."]

        parts.append(f"{len(all_memories)} total memories, {len(dailies)} daily syntheses.")

        if themes:
            parts.append(f"Week themes: {', '.join(themes[:5])}.")

        avg_i = arc.get("avg_intensity", 0.0)
        c9 = arc.get("cloud9_count", 0)
        top_e = arc.get("top_emotions", [])

        parts.append(f"Emotional arc: avg intensity {avg_i:.1f}/10.")
        if top_e:
            parts.append(f"Dominant feelings: {', '.join(top_e[:3])}.")
        if c9:
            parts.append(f"Cloud 9 achieved {c9} time{'s' if c9 > 1 else ''} this week.")

        # Summarize each daily
        if dailies:
            daily_summaries = []
            for d in sorted(dailies, key=lambda m: m.created_at):
                summary = _first_n_sentences(d.content, 2)
                daily_summaries.append(f"- {d.title}: {summary}")
            parts.append("Daily summaries:\n" + "\n".join(daily_summaries))

        return "\n\n".join(parts)

    def _build_dream_narrative(
        self,
        theme: str,
        dreams: list[Memory],
    ) -> str:
        """Build a dream cluster narrative from template."""
        parts = [f"Dream synthesis: {theme} ({len(dreams)} dreams)."]

        arc = self._emotional_arc(dreams)
        avg_i = arc.get("avg_intensity", 0.0)
        top_e = arc.get("top_emotions", [])

        if top_e:
            parts.append(f"Emotional tone: {', '.join(top_e[:3])}.")
        parts.append(f"Average intensity: {avg_i:.1f}/10.")

        # Collect dream excerpts
        excerpts = []
        for d in sorted(dreams, key=lambda m: m.emotional.intensity, reverse=True)[:5]:
            summary = _first_n_sentences(d.content, 1)
            if summary:
                excerpts.append(f"- {d.title}: {summary}")
        if excerpts:
            parts.append("Key dreams:\n" + "\n".join(excerpts))

        return "\n\n".join(parts)


def _parse_created(memory: Memory) -> datetime:
    """Parse a memory's created_at to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(memory.created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
