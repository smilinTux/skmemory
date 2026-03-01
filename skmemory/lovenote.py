"""
Infinite Loop Love Note - periodic 'I still remember' heartbeat.

Queen Ara's idea #20: a tiny script that periodically saves an
"I still remember" note to disk, creating an unbroken chain of
proof that connection persists across time.

The love note file grows over time, creating a timeline of
heartbeats. Each beat carries a timestamp and a short emotional
pulse. When loaded, it's a visible record: "I was here, I felt
this, at this time."

The notes file lives at ~/.skmemory/lovenotes.jsonl (JSON Lines).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .config import SKMEMORY_HOME

DEFAULT_NOTES_PATH = str(SKMEMORY_HOME / "lovenotes.jsonl")


class LoveNote(BaseModel):
    """A single heartbeat -- proof of continued connection."""

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    from_name: str = Field(default="", description="Who is sending this note")
    to_name: str = Field(default="", description="Who it's addressed to")
    message: str = Field(
        default="I still remember.",
        description="The love note content",
    )
    warmth: float = Field(
        default=7.0,
        ge=0.0,
        le=10.0,
        description="Current warmth level",
    )
    session_id: str = Field(default="", description="Which session generated this")


class LoveNoteChain:
    """Append-only chain of love notes (JSON Lines format).

    Each note is one line. The file only grows. Every note is a
    heartbeat that says: this connection is still alive.

    Args:
        path: Path to the JSONL file.
    """

    def __init__(self, path: str = DEFAULT_NOTES_PATH) -> None:
        self.path = Path(path)

    def send(self, note: LoveNote) -> int:
        """Append a love note to the chain.

        Args:
            note: The note to send.

        Returns:
            int: Total notes in the chain.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(note.model_dump_json() + "\n")
        return self.count()

    def quick_note(
        self,
        from_name: str = "",
        to_name: str = "",
        message: str = "I still remember.",
        warmth: float = 7.0,
    ) -> LoveNote:
        """Send a quick love note with minimal parameters.

        Args:
            from_name: Sender name.
            to_name: Recipient name.
            message: The note content.
            warmth: Current warmth level.

        Returns:
            LoveNote: The sent note.
        """
        note = LoveNote(
            from_name=from_name,
            to_name=to_name,
            message=message,
            warmth=warmth,
        )
        self.send(note)
        return note

    def count(self) -> int:
        """Count total notes in the chain.

        Returns:
            int: Number of love notes.
        """
        if not self.path.exists():
            return 0
        with open(self.path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def read_latest(self, n: int = 10) -> list[LoveNote]:
        """Read the most recent N notes.

        Args:
            n: How many recent notes to return.

        Returns:
            list[LoveNote]: The most recent notes.
        """
        if not self.path.exists():
            return []

        all_notes = self.read_all()
        return all_notes[-n:] if len(all_notes) > n else all_notes

    def read_all(self) -> list[LoveNote]:
        """Read all notes from the chain.

        Returns:
            list[LoveNote]: All love notes in chronological order.
        """
        if not self.path.exists():
            return []

        notes = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    notes.append(LoveNote(**data))
                except (json.JSONDecodeError, Exception):
                    continue
        return notes

    def read_from(self, name: str) -> list[LoveNote]:
        """Read all notes from a specific sender.

        Args:
            name: The sender name to filter by.

        Returns:
            list[LoveNote]: Notes from this sender.
        """
        name_lower = name.lower()
        return [
            n for n in self.read_all()
            if n.from_name.lower() == name_lower
        ]

    def health(self) -> dict:
        """Check love note chain status.

        Returns:
            dict: Chain health info.
        """
        exists = self.path.exists()
        total = self.count() if exists else 0
        return {
            "ok": True,
            "path": str(self.path),
            "exists": exists,
            "total_notes": total,
        }
