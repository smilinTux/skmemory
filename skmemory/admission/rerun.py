"""Re-run decision table — monotonic-in-tightness invariant.

| Stored decision | New decision | Action            |
|-----------------|--------------|-------------------|
| (none)          | admit/refuse | FIRST_EVALUATION  |
| admit           | admit        | BUMP_ONLY         |
| refuse          | refuse       | BUMP_ONLY         |
| admit           | refuse       | APPLY (auto)      |
| refuse          | admit        | BLOCK_AND_REVIEW  |

Loosening (refuse → admit) lands in a per-agent review queue and is
applied only after explicit human ratification. This is the rule that
prevents "let me just re-import everything with a looser policy" from
quietly laundering rejected memory back into the corpus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .constants import RerunDecision
from .gate2 import Gate2Result


@dataclass(frozen=True)
class RerunResult:
    decision: RerunDecision
    stored_admit: bool | None
    new_admit: bool
    stored_policy_version: str | None
    new_policy_version: str
    note: str = ""

    @property
    def applied(self) -> bool:
        """True iff the new decision should be written to the row."""
        return self.decision in (
            RerunDecision.FIRST_EVALUATION,
            RerunDecision.APPLY,
            RerunDecision.BUMP_ONLY,
        )

    @property
    def needs_review(self) -> bool:
        return self.decision == RerunDecision.BLOCK_AND_REVIEW


def evaluate_rerun(
    stored: Mapping[str, Any] | None,
    new: Gate2Result,
) -> RerunResult:
    """Compare a stored admission decision to a fresh decision.

    Args:
        stored: Either ``None`` (first evaluation), or a mapping with at
            least ``admission_admit`` (bool) and
            ``admission_policy_version`` (str). The shape produced by
            ``Gate2Result.to_metadata()``.
        new: Fresh ``Gate2Result`` produced by the current run.

    Returns:
        ``RerunResult`` describing what to do.
    """
    if stored is None or "admission_admit" not in stored:
        return RerunResult(
            decision=RerunDecision.FIRST_EVALUATION,
            stored_admit=None,
            new_admit=new.admit,
            stored_policy_version=None,
            new_policy_version=new.policy_version,
        )

    stored_admit = bool(stored.get("admission_admit"))
    stored_policy_version = stored.get("admission_policy_version")

    if stored_admit == new.admit:
        # Same outcome on both runs. Refresh the version stamp + note
        # without altering admit state.
        return RerunResult(
            decision=RerunDecision.BUMP_ONLY,
            stored_admit=stored_admit,
            new_admit=new.admit,
            stored_policy_version=stored_policy_version,
            new_policy_version=new.policy_version,
            note="version_bump_only",
        )

    if stored_admit and not new.admit:
        # Tightening: previously admitted, now refused. Apply.
        return RerunResult(
            decision=RerunDecision.APPLY,
            stored_admit=True,
            new_admit=False,
            stored_policy_version=stored_policy_version,
            new_policy_version=new.policy_version,
            note=f"tighten_to:{new.reason.value}",
        )

    # Loosening: previously refused, now admitted. Block + queue.
    return RerunResult(
        decision=RerunDecision.BLOCK_AND_REVIEW,
        stored_admit=False,
        new_admit=True,
        stored_policy_version=stored_policy_version,
        new_policy_version=new.policy_version,
        note=f"would_loosen_to:{new.reason.value}",
    )


def review_queue_path(agent_home: Path | str) -> Path:
    """Resolve the per-agent review-queue path.

    ``agent_home`` is typically ``~/.skcapstone/agents/<agent>``.
    The queue lives under ``memory/.admission_review/queue.jsonl``.
    """
    base = Path(agent_home) / "memory" / ".admission_review"
    base.mkdir(parents=True, exist_ok=True)
    return base / "queue.jsonl"


def enqueue_review(
    agent_home: Path | str,
    *,
    row_id: str,
    importer: str,
    rerun_result: RerunResult,
    new_decision: Gate2Result,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Append a single loosening-blocked row to the review queue.

    Returns the path of the queue file. Write is line-flushed JSON so
    a partial write can't corrupt the queue.
    """
    if not rerun_result.needs_review:
        raise ValueError(
            "enqueue_review called for a decision that does not need review: "
            f"{rerun_result.decision.value}"
        )

    path = review_queue_path(agent_home)
    record: dict[str, Any] = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "row_id": row_id,
        "importer": importer,
        "stored_admit": rerun_result.stored_admit,
        "stored_policy_version": rerun_result.stored_policy_version,
        "new_admit": rerun_result.new_admit,
        "new_policy_version": rerun_result.new_policy_version,
        "new_reason": new_decision.reason.value,
        "new_note": new_decision.note,
        "rerun_note": rerun_result.note,
    }
    if extra:
        record["extra"] = dict(extra)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
