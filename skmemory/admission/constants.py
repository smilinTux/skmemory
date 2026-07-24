"""Admission gate constants — single source of truth.

A drift test pairs this module with ``docs/admission_policy.md`` to catch
the case where a new admission reason is added in code without a matching
doc entry (or vice versa).
"""

from __future__ import annotations

from enum import Enum
from typing import Final

# Bumped whenever the policy semantics change. Stamped onto every
# admission decision so re-runs can distinguish stored-decision-vs-now.
ADMISSION_POLICY_VERSION: Final[str] = "1.0.0"


# Sentinel source_type used to keep Gate-1 FAIL rows in the uniform
# schema while marking them structurally non-admissible. Default
# retrieval / ritual paths filter this value out.
SENTINEL_UNRECOVERABLE_SOURCE: Final[str] = "gate1_unrecoverable"


# Sources currently emitted by live skmemory producers. Used by Gate 1
# to decide whether a bare-string provenance value is recoverable.
# Keep in sync with skmemory/importers/* and store.py call sites.
KNOWN_SOURCE_VOCAB: Final[frozenset[str]] = frozenset(
    {
        "manual",
        "session",
        "seed",
        "import",
        "telegram",
        "notion",
        "conversation",
        "claude-code-hook",
        "consolidation",
        "journal-synthesis",
        "task-pack",
        "shared",
    }
)


# Old-vocab → current-vocab. Gate 1 returns RECOVER (class 6) when an
# entry hits this mapping; otherwise it falls through to FAIL via
# class 4 (DICT_INVALID_TYPE).
DEPRECATED_SOURCE_MAPPING: Final[dict[str, str]] = {
    "tg": "telegram",
    "telegram-export": "telegram",
    "notion-export": "notion",
    "claude-hook": "claude-code-hook",
    "claude_code_hook": "claude-code-hook",
    "task_pack": "task-pack",
}


class Gate1Class(str, Enum):
    """Epistemic-recovery class assigned by Gate 1.

    Same row content always lands in the same class. Re-runs never flip.
    """

    ALREADY_CANONICAL = "already_canonical"
    LEGACY_BARE_STRING = "legacy_bare_string"
    DICT_TRUNCATED = "dict_truncated"
    DICT_INVALID_TYPE = "dict_invalid_type"
    NULL_OR_EMPTY = "null_or_empty"
    DEPRECATED_VOCAB = "deprecated_vocab"
    ZERO_EVENT_ARTIFACT = "zero_event_artifact"


class Gate1Outcome(str, Enum):
    """What Gate 1 emits per row."""

    SKIP = "skip"  # Already canonical, no recovery work needed.
    RECOVER = "recover"  # Reconstructed; flows to Gate 2.
    FAIL = "fail"  # Stored under sentinel; never visible to retrieval.


class Gate2Reason(str, Enum):
    """Why Gate 2 reached its decision. Drift-test enforces enumeration
    equality with ``docs/admission_policy.md`` admission-reasons table."""

    # Admit reasons.
    ADMIT_KNOWN_SOURCE = "admit_known_source"
    ADMIT_RECOVERED_DICT = "admit_recovered_dict"
    ADMIT_DEPRECATED_REMAPPED = "admit_deprecated_remapped"

    # Refuse reasons.
    REFUSE_GATE1_FAILED = "refuse_gate1_failed"
    REFUSE_COLLECTIVE_ECHO = "refuse_collective_echo"
    REFUSE_ZERO_EVENT_ARTIFACT = "refuse_zero_event_artifact"
    REFUSE_BLOCKED_SOURCE = "refuse_blocked_source"
    REFUSE_NO_RULE_MATCHED = "refuse_no_rule_matched"


class RerunDecision(str, Enum):
    """Outcome of comparing a stored decision to a fresh decision.

    The ``BLOCK_AND_REVIEW`` path is the load-bearing piece — it prevents
    silent loosening across re-runs.
    """

    FIRST_EVALUATION = "first_evaluation"
    BUMP_ONLY = "bump_only"
    APPLY = "apply"  # Tightening (admit → refuse). Applied automatically.
    BLOCK_AND_REVIEW = "block_and_review"  # Loosening. Human-only.


# Used by Gate 1 to flag mid-session scratch / debug rows even when the
# rest of the provenance is technically valid.
ZERO_EVENT_TAG_MARKERS: Final[frozenset[str]] = frozenset(
    {"debug", "scratch", "test-seed", "test_seed", "ephemeral", "throwaway"}
)
