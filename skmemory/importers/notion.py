"""Notion export importer (prototype) — wires the two-gate admission.

This is the first importer to run incoming rows through
``skmemory.admission``. Telegram + cross-agent rehydration are next.

Notion's "Export → Markdown & CSV" produces a folder of ``.md`` files,
one per page, plus a top-level CSV per database. This importer accepts
either:

* a directory of Markdown pages (each file → one row); or
* a JSONL "intermediate" file where each line is a pre-extracted page
  dict (used by tests and by upstream batch jobs).

For each row, we run Gate 1 (deterministic recovery) → Gate 2
(admission policy) and only admit-pass rows are saved to skmemory's
short-term tier. Refused rows are stored under the sentinel
``source_type`` so they remain auditable but invisible to retrieval.

Scope here is deliberately small — just enough to exercise the gates
and ship Phase 1 acceptance. Full Notion-block fidelity (toggles, child
pages, embedded files) lands in a later pass.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..admission import (
    SENTINEL_UNRECOVERABLE_SOURCE,
    AdmissionPolicy,
    Gate2Result,
    admit,
    enqueue_review,
    evaluate_rerun,
    recover,
)
from ..models import EmotionalSnapshot, MemoryLayer, MemoryRole

logger = logging.getLogger(__name__)


@dataclass
class ImportStats:
    """Per-run counters for a Notion import."""

    seen: int = 0
    admitted: int = 0
    refused: int = 0
    queued_for_review: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, bucket: dict[str, int]) -> None:
        bucket[key] = bucket.get(key, 0) + 1


def _iter_markdown_dir(root: Path) -> Iterator[dict[str, Any]]:
    """Yield one row dict per ``.md`` file under ``root``.

    Each row carries a bare-string ``source`` of ``"notion"`` so Gate 1
    classes it as ``LEGACY_BARE_STRING`` (the realistic case for old
    exports — Notion doesn't write skmemory-shaped provenance for us).
    """
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("notion import: skipping unreadable %s: %s", path, exc)
            continue
        title = path.stem
        yield {
            "row_id": str(path.relative_to(root)),
            "title": title,
            "content": text,
            "source": "notion",
            "tags": ["notion-import"],
            "external_path": str(path),
        }


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "notion import: skipping bad json at %s:%d: %s",
                    path,
                    lineno,
                    exc,
                )
                continue
            row.setdefault("row_id", f"{path.name}#L{lineno}")
            yield row


def iter_rows(source_path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield one row dict per Notion page from ``source_path``.

    Accepts either a directory (Markdown export) or a ``.jsonl`` file
    (intermediate format used by tests).
    """
    p = Path(source_path)
    if p.is_dir():
        yield from _iter_markdown_dir(p)
        return
    if p.suffix.lower() == ".jsonl" and p.is_file():
        yield from _iter_jsonl(p)
        return
    raise ValueError(
        f"notion importer: unsupported source path {p!r} (expected a directory or a .jsonl file)"
    )


def _row_metadata(
    row: Mapping[str, Any],
    gate2: Gate2Result,
    recovered_source_type: str,
) -> dict[str, Any]:
    """Build metadata blob to persist alongside the saved memory."""
    md = dict(row.get("metadata") or {})
    md.update(gate2.to_metadata())
    md["admission_recovered_source_type"] = recovered_source_type
    if "external_path" in row:
        md["external_path"] = row["external_path"]
    return md


def import_notion(
    store: Any,
    source_path: Path | str,
    *,
    rows: Iterable[Mapping[str, Any]] | None = None,
    policy: AdmissionPolicy | None = None,
    agent_home: Path | str | None = None,
    stored_decisions: Mapping[str, Mapping[str, Any]] | None = None,
    layer: MemoryLayer = MemoryLayer.SHORT,
    role: MemoryRole = MemoryRole.GENERAL,
) -> ImportStats:
    """Import a Notion export through the two-gate admission flow.

    Args:
        store: Object exposing ``snapshot(...)`` (a ``MemoryStore``).
        source_path: Directory or ``.jsonl`` path. Ignored when ``rows``
            is supplied (tests).
        rows: Optional iterable of pre-built row dicts. When provided,
            ``source_path`` is used only as a label.
        policy: Admission policy override. Defaults to
            ``AdmissionPolicy()``.
        agent_home: Used to locate the review queue for loosening.
            Optional; if omitted, loosening is logged but not queued.
        stored_decisions: Map of ``row_id`` →
            ``Gate2Result.to_metadata()`` shape, representing the prior
            run's decision. Drives the monotonic re-run check.
        layer: Memory layer to write admitted rows into.
        role: Memory role to tag admitted rows with.

    Returns:
        ImportStats with counters.
    """
    policy = policy or AdmissionPolicy()
    stored_decisions = stored_decisions or {}
    stats = ImportStats()

    iterable = rows if rows is not None else iter_rows(source_path)

    for row in iterable:
        stats.seen += 1
        row_id = str(row.get("row_id") or row.get("id") or f"row-{stats.seen}")

        gate1 = recover(row)
        stats.bump(gate1.cls.value, stats.by_class)

        gate2 = admit(row, gate1, policy=policy)
        stats.bump(gate2.reason.value, stats.by_reason)

        rerun = evaluate_rerun(stored_decisions.get(row_id), gate2)

        if rerun.needs_review:
            stats.queued_for_review += 1
            stats.refused += 1  # blocked write — stays at prior refuse state
            if agent_home is not None:
                enqueue_review(
                    agent_home,
                    row_id=row_id,
                    importer="notion",
                    rerun_result=rerun,
                    new_decision=gate2,
                    extra={"title": row.get("title", "")},
                )
            else:
                logger.info(
                    "notion import: loosening blocked for %s (no agent_home given)",
                    row_id,
                )
            continue

        # Persist refused rows under sentinel so audit can find them,
        # but mark them excluded from default retrieval/ritual.
        if not gate2.admit:
            stats.refused += 1
            recovered = SENTINEL_UNRECOVERABLE_SOURCE
            metadata = _row_metadata(row, gate2, recovered)
            metadata["admission_excluded_from_retrieval"] = True
            store.snapshot(
                title=str(row.get("title") or f"notion:{row_id}"),
                content=str(row.get("content") or ""),
                layer=layer,
                role=role,
                tags=list(row.get("tags") or []) + ["admission:refused"],
                source=recovered,
                source_ref=row_id,
                metadata=metadata,
            )
            continue

        stats.admitted += 1
        recovered = gate1.recovered_source_type
        metadata = _row_metadata(row, gate2, recovered)
        metadata["admission_excluded_from_retrieval"] = False
        store.snapshot(
            title=str(row.get("title") or f"notion:{row_id}"),
            content=str(row.get("content") or ""),
            layer=layer,
            role=role,
            tags=list(row.get("tags") or []) + ["admission:admitted"],
            emotional=EmotionalSnapshot(
                intensity=float(row.get("intensity", 0.0) or 0.0),
                valence=float(row.get("valence", 0.0) or 0.0),
                labels=list(row.get("emotional_labels") or []),
            ),
            source=recovered,
            source_ref=row_id,
            metadata=metadata,
        )

    return stats
