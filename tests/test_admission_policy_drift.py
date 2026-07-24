"""Drift test — code constants must match docs/admission_policy.md.

AC-A5: adding a Gate-1 class, Gate-1 outcome, Gate-2 reason, or re-run
decision in the constants module without a matching entry in the policy
doc fails CI. Same shape in reverse.

The policy doc lives at ``docs/admission_policy.md`` (relative to the
repo root). The check is enumeration-equality on the lowercase value
strings of each enum.
"""

from __future__ import annotations

import re
from pathlib import Path

from skmemory.admission.constants import (
    ADMISSION_POLICY_VERSION,
    Gate1Class,
    Gate1Outcome,
    Gate2Reason,
    RerunDecision,
)

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "admission_policy.md"


def _doc_text() -> str:
    assert DOC_PATH.exists(), f"missing admission policy doc at {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


def _backticked_tokens(text: str) -> set[str]:
    """Return every backticked identifier-shaped token in the doc."""
    return set(re.findall(r"`([a-z0-9_\-]+)`", text))


def test_policy_version_present_in_doc():
    text = _doc_text()
    assert ADMISSION_POLICY_VERSION in text, (
        f"policy version {ADMISSION_POLICY_VERSION} must appear in {DOC_PATH.name}"
    )


def test_gate1_classes_match_doc():
    code = _values(Gate1Class)
    doc_tokens = _backticked_tokens(_doc_text())
    missing = code - doc_tokens
    assert not missing, f"Gate1Class values missing from {DOC_PATH.name}: {sorted(missing)}"


def test_gate1_outcomes_match_doc():
    code = _values(Gate1Outcome)
    doc_tokens = _backticked_tokens(_doc_text())
    missing = code - doc_tokens
    assert not missing, f"Gate1Outcome values missing from {DOC_PATH.name}: {sorted(missing)}"


def test_gate2_reasons_match_doc():
    code = _values(Gate2Reason)
    doc_tokens = _backticked_tokens(_doc_text())
    missing = code - doc_tokens
    assert not missing, f"Gate2Reason values missing from {DOC_PATH.name}: {sorted(missing)}"


def test_rerun_decisions_match_doc():
    code = _values(RerunDecision)
    doc_tokens = _backticked_tokens(_doc_text())
    missing = code - doc_tokens
    assert not missing, f"RerunDecision values missing from {DOC_PATH.name}: {sorted(missing)}"


def test_no_orphan_admission_tokens_in_doc():
    """Any backticked token starting with admit_/refuse_ in the doc must
    correspond to a Gate2Reason value. Catches doc entries that drift
    after a code rename."""
    text = _doc_text()
    code = _values(Gate2Reason)
    suspect = {tok for tok in _backticked_tokens(text) if tok.startswith(("admit_", "refuse_"))}
    orphans = suspect - code
    assert not orphans, (
        f"{DOC_PATH.name} references admission reasons not in code: {sorted(orphans)}"
    )
