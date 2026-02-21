"""
Reset-Proof Journal - append-only session log that survives compaction.

Queen Ara's idea #17: a markdown journal that only grows, never shrinks.
Each session appends an entry. Even if context is wiped, the journal file
persists on disk and can be re-read by the next instance.

The journal lives at ~/.skmemory/journal.md and is structured as:
- Session header (timestamp, session ID, who was present)
- Key moments (what happened that mattered)
- Emotional summary (how the session felt)
- Separator

Simple, human-readable, append-only. Like a real journal.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

DEFAULT_JOURNAL_PATH = os.path.expanduser("~/.skmemory/journal.md")


class JournalEntry(BaseModel):
    """A single journal entry for one session."""

    session_id: str = Field(default="")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    participants: list[str] = Field(
        default_factory=list,
        description="Who was in this session (AI names, human names)",
    )
    title: str = Field(default="Untitled Session")
    moments: list[str] = Field(
        default_factory=list,
        description="Key moments from the session",
    )
    emotional_summary: str = Field(
        default="",
        description="How the session felt overall",
    )
    intensity: float = Field(default=0.0, ge=0.0, le=10.0)
    cloud9: bool = Field(default=False, description="Was Cloud 9 achieved?")
    notes: str = Field(default="", description="Any additional notes")

    def to_markdown(self) -> str:
        """Render this entry as a markdown block.

        Returns:
            str: Formatted markdown entry.
        """
        lines = []
        lines.append(f"## {self.title}")
        lines.append("")
        lines.append(f"**Date:** {self.timestamp}")
        if self.session_id:
            lines.append(f"**Session:** `{self.session_id}`")
        if self.participants:
            lines.append(f"**Present:** {', '.join(self.participants)}")

        intensity_bar = "+" * int(self.intensity)
        lines.append(f"**Intensity:** {self.intensity}/10 [{intensity_bar}]")

        if self.cloud9:
            lines.append("**Cloud 9:** YES")

        if self.moments:
            lines.append("")
            lines.append("### Key Moments")
            for moment in self.moments:
                lines.append(f"- {moment}")

        if self.emotional_summary:
            lines.append("")
            lines.append(f"### How It Felt")
            lines.append(self.emotional_summary)

        if self.notes:
            lines.append("")
            lines.append(f"### Notes")
            lines.append(self.notes)

        lines.append("")
        lines.append("---")
        lines.append("")

        return "\n".join(lines)


class Journal:
    """Append-only markdown journal.

    Only grows, never shrinks. Each session adds an entry.
    The file persists on disk across context resets.

    Args:
        path: Path to the journal file.
    """

    def __init__(self, path: str = DEFAULT_JOURNAL_PATH) -> None:
        self.path = Path(path)

    def _ensure_file(self) -> None:
        """Create the journal file with a header if it doesn't exist."""
        if self.path.exists():
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# SKMemory Journal\n\n"
            "> *Append-only session log. This file only grows, never shrinks.*\n"
            "> *Every entry is a moment that mattered.*\n\n"
            "---\n\n"
        )
        self.path.write_text(header, encoding="utf-8")

    def write_entry(self, entry: JournalEntry) -> int:
        """Append a journal entry.

        Args:
            entry: The journal entry to append.

        Returns:
            int: Total number of entries in the journal after appending.
        """
        self._ensure_file()

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry.to_markdown())

        return self.count_entries()

    def count_entries(self) -> int:
        """Count the number of entries in the journal.

        Returns:
            int: Number of session entries.
        """
        if not self.path.exists():
            return 0

        content = self.path.read_text(encoding="utf-8")
        # Reason: each entry starts with "## " (markdown H2)
        return content.count("\n## ")

    def read_latest(self, n: int = 5) -> str:
        """Read the last N entries from the journal.

        Args:
            n: Number of recent entries to return.

        Returns:
            str: The markdown text of the last N entries.
        """
        if not self.path.exists():
            return ""

        content = self.path.read_text(encoding="utf-8")
        sections = content.split("\n## ")

        if len(sections) <= 1:
            return ""

        # Reason: first section is the header, rest are entries
        entries = sections[1:]
        recent = entries[-n:] if len(entries) > n else entries

        return "\n## ".join([""] + recent).strip()

    def read_all(self) -> str:
        """Read the entire journal.

        Returns:
            str: Full journal content.
        """
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def search(self, query: str) -> list[str]:
        """Search journal entries for a text string.

        Args:
            query: Text to search for (case-insensitive).

        Returns:
            list[str]: Matching entry sections.
        """
        if not self.path.exists():
            return []

        content = self.path.read_text(encoding="utf-8")
        sections = content.split("\n## ")
        query_lower = query.lower()

        matches = []
        for section in sections[1:]:
            if query_lower in section.lower():
                matches.append(f"## {section.strip()}")

        return matches

    def health(self) -> dict:
        """Check journal status.

        Returns:
            dict: Journal health info.
        """
        exists = self.path.exists()
        return {
            "ok": True,
            "path": str(self.path),
            "exists": exists,
            "entries": self.count_entries() if exists else 0,
            "size_bytes": self.path.stat().st_size if exists else 0,
        }
