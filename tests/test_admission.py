"""Tests for skmemory.admission — Gate 1, Gate 2, re-run table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.admission import (
    ADMISSION_POLICY_VERSION,
    SENTINEL_UNRECOVERABLE_SOURCE,
    AdmissionPolicy,
    Gate1Class,
    Gate1Outcome,
    Gate2Reason,
    RerunDecision,
    admit,
    enqueue_review,
    evaluate_rerun,
    recover,
    review_queue_path,
)

# ── Gate 1 — deterministic recovery ────────────────────────────────────────


class TestGate1:
    def test_already_canonical_skips(self):
        row = {"source_type": "telegram", "parent_eids": ["abc"]}
        r = recover(row)
        assert r.cls is Gate1Class.ALREADY_CANONICAL
        assert r.outcome is Gate1Outcome.SKIP
        assert r.recovered_source_type == "telegram"
        assert r.recovered_parent_eids == ["abc"]

    def test_legacy_bare_string_recovers(self):
        r = recover({"source": "telegram"})
        assert r.cls is Gate1Class.LEGACY_BARE_STRING
        assert r.outcome is Gate1Outcome.RECOVER
        assert r.recovered_source_type == "telegram"
        assert r.recovered_parent_eids == []

    def test_dict_truncated_recovers_with_empty_parents(self):
        row = {"source_type": {"type": "notion"}}
        r = recover(row)
        assert r.cls is Gate1Class.DICT_TRUNCATED
        assert r.outcome is Gate1Outcome.RECOVER
        assert r.recovered_source_type == "notion"
        assert r.recovered_parent_eids == []

    def test_dict_truncated_keeps_provided_parents(self):
        row = {"source_type": {"type": "notion", "parent_eids": ["x", "y"]}}
        r = recover(row)
        assert r.cls is Gate1Class.DICT_TRUNCATED
        assert r.recovered_parent_eids == ["x", "y"]

    def test_dict_invalid_type_fails(self):
        row = {"source_type": {"type": "fictional-vocab"}}
        r = recover(row)
        assert r.cls is Gate1Class.DICT_INVALID_TYPE
        assert r.outcome is Gate1Outcome.FAIL
        assert r.recovered_source_type == SENTINEL_UNRECOVERABLE_SOURCE

    def test_null_or_empty_fails(self):
        for val in [{}, {"source": ""}, {"source": "   "}, {"source": None}]:
            r = recover(val)
            assert r.outcome is Gate1Outcome.FAIL
            assert r.cls in (Gate1Class.NULL_OR_EMPTY, Gate1Class.DICT_INVALID_TYPE)

    def test_deprecated_vocab_recovers_via_mapping(self):
        r = recover({"source": "notion-export"})
        assert r.cls is Gate1Class.DEPRECATED_VOCAB
        assert r.outcome is Gate1Outcome.RECOVER
        assert r.recovered_source_type == "notion"

    def test_zero_event_artifact_fails_distinct(self):
        # Tag-driven flag wins even over otherwise-canonical provenance.
        row = {"source_type": "manual", "parent_eids": [], "tags": ["debug"]}
        r = recover(row)
        assert r.cls is Gate1Class.ZERO_EVENT_ARTIFACT
        assert r.fail_reason == "zero_event_artifact"

    def test_recovery_is_deterministic(self):
        # AC-A2: same input → same outcome on repeated runs.
        row = {"source": "telegram-export"}  # deprecated mapping
        first = recover(row)
        for _ in range(5):
            again = recover(row)
            assert again == first


# ── Gate 2 — admission policy ──────────────────────────────────────────────


class TestGate2:
    def test_admit_known_source(self):
        row = {"source_type": "telegram", "parent_eids": []}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is True
        assert g2.reason is Gate2Reason.ADMIT_KNOWN_SOURCE
        assert g2.policy_version == ADMISSION_POLICY_VERSION

    def test_admit_recovered_dict(self):
        row = {"source_type": {"type": "notion"}}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is True
        assert g2.reason is Gate2Reason.ADMIT_RECOVERED_DICT

    def test_admit_deprecated_remap(self):
        row = {"source": "tg"}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is True
        assert g2.reason is Gate2Reason.ADMIT_DEPRECATED_REMAPPED

    def test_refuse_deprecated_when_disabled(self):
        row = {"source": "tg"}
        g1 = recover(row)
        policy = AdmissionPolicy(allow_deprecated_remap=False)
        g2 = admit(row, g1, policy=policy)
        assert g2.admit is False
        assert g2.reason is Gate2Reason.REFUSE_NO_RULE_MATCHED

    def test_refuse_blocked_source(self):
        # We bypass Gate 1 by hand here — recovered_source_type lives
        # in the blocked set, so Gate 2 must refuse even on success.
        from skmemory.admission.gate1 import Gate1Result

        g1 = Gate1Result(
            cls=Gate1Class.LEGACY_BARE_STRING,
            outcome=Gate1Outcome.RECOVER,
            recovered_source_type="collective",
        )
        g2 = admit({}, g1)
        assert g2.admit is False
        assert g2.reason is Gate2Reason.REFUSE_BLOCKED_SOURCE

    def test_refuse_collective_echo_tag(self):
        row = {"source_type": "manual", "parent_eids": [], "tags": ["egregore"]}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is False
        assert g2.reason is Gate2Reason.REFUSE_COLLECTIVE_ECHO

    def test_refuse_zero_event_artifact_distinct(self):
        row = {"source_type": "manual", "parent_eids": [], "tags": ["debug"]}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is False
        assert g2.reason is Gate2Reason.REFUSE_ZERO_EVENT_ARTIFACT

    def test_refuse_gate1_failed(self):
        row = {"source": "totally-unknown-vocab"}
        g1 = recover(row)
        g2 = admit(row, g1)
        assert g2.admit is False
        assert g2.reason is Gate2Reason.REFUSE_GATE1_FAILED

    def test_to_metadata_round_trips(self):
        row = {"source_type": "telegram", "parent_eids": []}
        g2 = admit(row, recover(row))
        md = g2.to_metadata()
        assert md["admission_admit"] is True
        assert md["admission_reason"] == Gate2Reason.ADMIT_KNOWN_SOURCE.value
        assert md["admission_policy_version"] == ADMISSION_POLICY_VERSION


# ── Re-run table — monotonic-in-tightness invariant ────────────────────────


class TestRerunTable:
    def _admit(self):
        row = {"source_type": "telegram", "parent_eids": []}
        return admit(row, recover(row))

    def _refuse(self):
        row = {"source_type": "manual", "parent_eids": [], "tags": ["egregore"]}
        return admit(row, recover(row))

    def test_first_evaluation(self):
        r = evaluate_rerun(None, self._admit())
        assert r.decision is RerunDecision.FIRST_EVALUATION
        assert r.applied is True

    def test_bump_only_when_admit_unchanged(self):
        new = self._admit()
        stored = new.to_metadata()
        r = evaluate_rerun(stored, new)
        assert r.decision is RerunDecision.BUMP_ONLY

    def test_bump_only_when_refuse_unchanged(self):
        new = self._refuse()
        stored = new.to_metadata()
        r = evaluate_rerun(stored, new)
        assert r.decision is RerunDecision.BUMP_ONLY

    def test_tightening_applies(self):
        admitted = self._admit().to_metadata()
        new_refuse = self._refuse()
        r = evaluate_rerun(admitted, new_refuse)
        assert r.decision is RerunDecision.APPLY
        assert r.applied is True

    def test_loosening_blocks_for_review(self):
        # AC-A3: refuse → admit re-run must NOT auto-apply.
        refused = self._refuse().to_metadata()
        new_admit = self._admit()
        r = evaluate_rerun(refused, new_admit)
        assert r.decision is RerunDecision.BLOCK_AND_REVIEW
        assert r.needs_review is True
        assert r.applied is False


class TestReviewQueue:
    def test_enqueue_writes_jsonl(self, tmp_path: Path):
        refused = admit(
            {"tags": ["egregore"], "source_type": "manual", "parent_eids": []},
            recover({"tags": ["egregore"], "source_type": "manual", "parent_eids": []}),
        )
        admitted = admit(
            {"source_type": "telegram", "parent_eids": []},
            recover({"source_type": "telegram", "parent_eids": []}),
        )
        rerun = evaluate_rerun(refused.to_metadata(), admitted)
        path = enqueue_review(
            tmp_path,
            row_id="row-001",
            importer="notion",
            rerun_result=rerun,
            new_decision=admitted,
        )
        assert path.exists()
        line = path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["row_id"] == "row-001"
        assert record["importer"] == "notion"
        assert record["new_admit"] is True
        assert record["stored_admit"] is False

    def test_enqueue_rejects_non_review_decisions(self, tmp_path: Path):
        admitted = admit(
            {"source_type": "telegram", "parent_eids": []},
            recover({"source_type": "telegram", "parent_eids": []}),
        )
        rerun = evaluate_rerun(None, admitted)  # FIRST_EVALUATION
        with pytest.raises(ValueError):
            enqueue_review(
                tmp_path,
                row_id="r",
                importer="notion",
                rerun_result=rerun,
                new_decision=admitted,
            )

    def test_review_queue_path_creates_dir(self, tmp_path: Path):
        path = review_queue_path(tmp_path)
        assert path.parent.exists()
        assert path.parent.name == ".admission_review"
