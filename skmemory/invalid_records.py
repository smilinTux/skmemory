"""Fail-closed handling for malformed flat memory records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def require_memory_id(value: Any) -> str:
    """Return a normalized non-empty memory ID or raise ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Memory ID cannot be empty or null")
    return value.strip()


def payload_memory_id(payload: Any, fallback: str = "") -> str:
    """Validate an explicit payload ID, falling back to the filename stem."""
    if isinstance(payload, dict):
        if "id" in payload:
            return require_memory_id(payload["id"])
        if "memory_id" in payload:
            return require_memory_id(payload["memory_id"])
    return require_memory_id(fallback)


def quarantine_invalid_flat_file(
    memory_root: str | Path,
    source: str | Path,
    *,
    reason: str,
) -> dict[str, str]:
    """Move one invalid flat record into a content-addressed quarantine.

    The payload remains byte-for-byte recoverable. The deterministic report
    records only its hash, original relative path, reason, and quarantine path;
    memory content never enters the report.
    """
    root = Path(memory_root)
    source_path = Path(source)
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    quarantine = root / "quarantine" / "invalid-memory-id"
    quarantine.mkdir(parents=True, exist_ok=True)
    payload_path = quarantine / f"{digest}.json"
    if not payload_path.exists():
        payload_path.write_bytes(raw)

    try:
        original = str(source_path.relative_to(root))
    except ValueError:
        original = str(source_path)

    report_path = quarantine / "report.json"
    entries: list[dict[str, str]] = []
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(report, dict) and isinstance(report.get("entries"), list):
                entries = [entry for entry in report["entries"] if isinstance(entry, dict)]
        except (OSError, json.JSONDecodeError):
            entries = []

    entry = {
        "sha256": digest,
        "source": original,
        "reason": reason,
        "quarantine": str(payload_path.relative_to(root)),
    }
    keyed = {(item.get("sha256"), item.get("source")): item for item in entries}
    keyed[(digest, original)] = entry
    ordered = sorted(
        keyed.values(),
        key=lambda item: (item.get("sha256", ""), item.get("source", "")),
    )
    temp_path = report_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps({"schema": "skmemory.invalid-records/v1", "entries": ordered}, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(report_path)
    source_path.unlink()
    return entry
