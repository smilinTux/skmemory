"""Write-Ahead Log (WAL) — audit trail for all memory write operations.

Every memory write is logged BEFORE execution for crash recovery and
tamper detection. Each line is a self-contained JSON record.

Inspired by MemPalace's WAL pattern. Provides:
- Pre-write logging (detect incomplete writes after crash)
- Post-write confirmation
- Failure logging with error details
- Tail reading for recent audit entries

Default path: ~/.skcapstone/agents/{agent}/memory/wal/write_log.jsonl
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("skmemory.wal")


class WriteAheadLog:
    """Append-only JSONL log for memory write operations.

    Each line: {"ts": "...", "op": "...", "memory_id": "...", "status": "pending|done|failed"}

    Args:
        path: Path to the JSONL file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, entry: dict[str, Any]) -> None:
        """Append a single JSON line to the log."""
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.warning("WAL write failed: %s", exc)

    def log_pending(
        self,
        op: str,
        memory_id: str,
        title: str = "",
        layer: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Log a pending write operation BEFORE it executes."""
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "title": title[:100],
            "layer": layer,
            "status": "pending",
        }
        if metadata:
            entry["meta"] = metadata
        self._append(entry)

    def log_done(self, op: str, memory_id: str) -> None:
        """Log that a pending write completed successfully."""
        self._append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "status": "done",
        })

    def log_failed(self, op: str, memory_id: str, error: str) -> None:
        """Log that a pending write failed."""
        self._append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "status": "failed",
            "error": str(error)[:500],
        })

    def tail(self, n: int = 50) -> list[dict]:
        """Read the last n entries from the log.

        Returns:
            List of parsed JSON entries, newest last.
        """
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text().strip().split("\n")
            entries = []
            for line in lines[-n:]:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return entries
        except Exception as exc:
            logger.warning("WAL read failed: %s", exc)
            return []

    def pending_writes(self) -> list[dict]:
        """Find writes that were logged as pending but never completed.

        These represent potential crash-interrupted writes.
        """
        entries = self.tail(500)
        pending: dict[str, dict] = {}
        for entry in entries:
            mid = entry.get("memory_id", "")
            op = entry.get("op", "")
            status = entry.get("status", "")
            key = f"{op}:{mid}"
            if status == "pending":
                pending[key] = entry
            elif status in ("done", "failed"):
                pending.pop(key, None)
        return list(pending.values())
