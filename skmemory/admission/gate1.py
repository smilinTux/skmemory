"""Gate 1 — epistemic recovery.

Deterministic per row content. The same input always lands in the same
``Gate1Class`` and emits the same ``Gate1Outcome``. Re-runs never flip
recovery — that's the load-bearing property other gates depend on.

Recovery answers *can we honestly reconstruct what this row's
provenance is?* It does NOT answer *should this row be allowed to
influence future retrieval?* — that's Gate 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    DEPRECATED_SOURCE_MAPPING,
    KNOWN_SOURCE_VOCAB,
    SENTINEL_UNRECOVERABLE_SOURCE,
    ZERO_EVENT_TAG_MARKERS,
    Gate1Class,
    Gate1Outcome,
)


@dataclass(frozen=True)
class Gate1Result:
    """One row, one verdict.

    The recovered fields are populated only on RECOVER. SKIP returns the
    row's existing canonical fields verbatim. FAIL writes the sentinel
    ``source_type`` so the row stays in the uniform schema.
    """

    cls: Gate1Class
    outcome: Gate1Outcome
    recovered_source_type: str
    recovered_parent_eids: list[str] = field(default_factory=list)
    fail_reason: str = ""

    @property
    def is_recoverable(self) -> bool:
        return self.outcome in (Gate1Outcome.SKIP, Gate1Outcome.RECOVER)


def _has_canonical_provenance(row: Mapping[str, Any]) -> bool:
    """Already passes skmemory's provenance schema.

    Canonical means a non-empty ``source_type`` in the known vocab AND
    a present (possibly empty) ``parent_eids`` list.
    """
    src = row.get("source_type")
    parents = row.get("parent_eids")
    if not isinstance(src, str) or src not in KNOWN_SOURCE_VOCAB:
        return False
    return isinstance(parents, list)


def _is_zero_event_artifact(row: Mapping[str, Any]) -> bool:
    """Debug artifact / test seed / mid-session scratch.

    Distinct FAIL reason — these aren't malformed, they're *intentional*
    non-corpus rows that just happened to share storage.
    """
    if row.get("zero_event") is True:
        return True
    tags = row.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.lower() in ZERO_EVENT_TAG_MARKERS:
                return True
    return False


def recover(row: Mapping[str, Any]) -> Gate1Result:
    """Classify ``row`` into one of the seven Gate-1 classes.

    Args:
        row: Mapping with any subset of skmemory provenance fields.
            Only keys this gate inspects: ``source_type`` (or legacy
            ``source``), ``parent_eids``, ``tags``, ``zero_event``.

    Returns:
        Gate1Result with deterministic class + outcome.
    """
    # Class 7 — Zero-event artifact. Checked first so it wins over
    # otherwise-canonical rows tagged debug/scratch.
    if _is_zero_event_artifact(row):
        return Gate1Result(
            cls=Gate1Class.ZERO_EVENT_ARTIFACT,
            outcome=Gate1Outcome.FAIL,
            recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
            fail_reason="zero_event_artifact",
        )

    # Class 1 — Already canonical. SKIP.
    if _has_canonical_provenance(row):
        return Gate1Result(
            cls=Gate1Class.ALREADY_CANONICAL,
            outcome=Gate1Outcome.SKIP,
            recovered_source_type=str(row["source_type"]),
            recovered_parent_eids=list(row.get("parent_eids") or []),
        )

    raw_source = row.get("source_type")
    if raw_source is None:
        raw_source = row.get("source")

    # Class 5 — Null/empty/garbage. FAIL.
    if raw_source is None or (isinstance(raw_source, str) and not raw_source.strip()):
        return Gate1Result(
            cls=Gate1Class.NULL_OR_EMPTY,
            outcome=Gate1Outcome.FAIL,
            recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
            fail_reason="null_or_empty_source",
        )

    # Class 2 — Bare string in source slot.
    if isinstance(raw_source, str):
        normalized = raw_source.strip().lower()
        if normalized in KNOWN_SOURCE_VOCAB:
            return Gate1Result(
                cls=Gate1Class.LEGACY_BARE_STRING,
                outcome=Gate1Outcome.RECOVER,
                recovered_source_type=normalized,
                recovered_parent_eids=[],
            )
        # Class 6 — Deprecated vocab with explicit mapping.
        if normalized in DEPRECATED_SOURCE_MAPPING:
            return Gate1Result(
                cls=Gate1Class.DEPRECATED_VOCAB,
                outcome=Gate1Outcome.RECOVER,
                recovered_source_type=DEPRECATED_SOURCE_MAPPING[normalized],
                recovered_parent_eids=[],
            )
        # Bare string but not in any vocab → falls to invalid-type FAIL.
        return Gate1Result(
            cls=Gate1Class.DICT_INVALID_TYPE,
            outcome=Gate1Outcome.FAIL,
            recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
            fail_reason=f"unknown_bare_source:{normalized}",
        )

    # Source is a dict (or dict-like).
    if isinstance(raw_source, Mapping):
        inner_type = raw_source.get("type") or raw_source.get("source_type")
        if not isinstance(inner_type, str) or not inner_type.strip():
            return Gate1Result(
                cls=Gate1Class.NULL_OR_EMPTY,
                outcome=Gate1Outcome.FAIL,
                recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
                fail_reason="dict_missing_type",
            )
        normalized = inner_type.strip().lower()

        if normalized in KNOWN_SOURCE_VOCAB:
            # Class 3 — Dict, valid type, missing parent links.
            parents = raw_source.get("parent_eids") or row.get("parent_eids") or []
            if not isinstance(parents, list):
                parents = []
            return Gate1Result(
                cls=Gate1Class.DICT_TRUNCATED,
                outcome=Gate1Outcome.RECOVER,
                recovered_source_type=normalized,
                recovered_parent_eids=list(parents),
            )
        if normalized in DEPRECATED_SOURCE_MAPPING:
            return Gate1Result(
                cls=Gate1Class.DEPRECATED_VOCAB,
                outcome=Gate1Outcome.RECOVER,
                recovered_source_type=DEPRECATED_SOURCE_MAPPING[normalized],
                recovered_parent_eids=list(
                    raw_source.get("parent_eids") or row.get("parent_eids") or []
                ),
            )
        # Class 4 — Dict, type not in vocab.
        return Gate1Result(
            cls=Gate1Class.DICT_INVALID_TYPE,
            outcome=Gate1Outcome.FAIL,
            recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
            fail_reason=f"unknown_dict_type:{normalized}",
        )

    # Anything else (int, list, etc.) — treat as invalid type.
    return Gate1Result(
        cls=Gate1Class.DICT_INVALID_TYPE,
        outcome=Gate1Outcome.FAIL,
        recovered_source_type=SENTINEL_UNRECOVERABLE_SOURCE,
        fail_reason=f"unsupported_source_python_type:{type(raw_source).__name__}",
    )
