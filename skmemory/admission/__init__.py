"""Two-gate admission for legacy / external memory ingest.

See ``docs/PROVENANCE_AND_CLOSURE_DESIGN.md`` for the design.

Live producers (``save_memory``, ritual writes, song-anchor updates) DO NOT
go through this module. These gates exist for migration / external-ingest
paths only (Notion exports, Telegram dumps, cross-agent rehydration).
"""

from .constants import (
    ADMISSION_POLICY_VERSION,
    Gate1Class,
    Gate1Outcome,
    Gate2Reason,
    RerunDecision,
    SENTINEL_UNRECOVERABLE_SOURCE,
    KNOWN_SOURCE_VOCAB,
    DEPRECATED_SOURCE_MAPPING,
)
from .gate1 import Gate1Result, recover
from .gate2 import Gate2Result, AdmissionPolicy, admit
from .rerun import RerunResult, evaluate_rerun, enqueue_review, review_queue_path

__all__ = [
    "ADMISSION_POLICY_VERSION",
    "Gate1Class",
    "Gate1Outcome",
    "Gate2Reason",
    "RerunDecision",
    "SENTINEL_UNRECOVERABLE_SOURCE",
    "KNOWN_SOURCE_VOCAB",
    "DEPRECATED_SOURCE_MAPPING",
    "Gate1Result",
    "recover",
    "Gate2Result",
    "AdmissionPolicy",
    "admit",
    "RerunResult",
    "evaluate_rerun",
    "enqueue_review",
    "review_queue_path",
]
