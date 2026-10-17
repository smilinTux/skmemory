"""Tests for standing answers (R4): watermark-keyed staleness, durable JSON
persistence under an injected FreshContextRunner seam, and a plain-read path
that never calls out.

Covers: watermark staleness true/false, a failed refresh leaving the
watermark untouched, a refresh that finds nothing not hiding later memories,
the read path making zero retrieval calls, durable write round-trip via
atomic_io, deterministic serialization, the no-op `refresh_if_stale` when
fresh, and that the injected runner is actually used.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skmemory import atomic_io
from skmemory.standing import (
    ComputedAnswer,
    RefreshOutcome,
    StandingAnswer,
    StandingAnswerStore,
    standing_answer_id,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)


@pytest.fixture()
def standing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's agent-path resolution at a throwaway directory."""
    target = tmp_path / "standing"
    monkeypatch.setattr("skmemory.standing._standing_dir", lambda agent_name=None: target)
    return target


@pytest.fixture()
def store(standing_dir: Path) -> StandingAnswerStore:
    return StandingAnswerStore()


class _CountingCompute:
    """A trivial injected AnswerComputer that counts how many times it ran."""

    def __init__(self, answer: str = "computed answer", newest: datetime | None = T1) -> None:
        self.calls = 0
        self._answer = answer
        self._newest = newest

    def __call__(self, current: StandingAnswer) -> ComputedAnswer:
        self.calls += 1
        return ComputedAnswer(answer=self._answer, newest_memory_seen_at=self._newest)


class _FailingCompute:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, current: StandingAnswer) -> ComputedAnswer:
        self.calls += 1
        raise RuntimeError("retrieval backend unreachable")


# ──────────────────────────────────────────────────────────────────────────
# standing_answer_id
# ──────────────────────────────────────────────────────────────────────────


def test_id_is_deterministic() -> None:
    assert standing_answer_id("what is my status?") == standing_answer_id("what is my status?")


def test_id_differs_for_different_questions() -> None:
    assert standing_answer_id("question a") != standing_answer_id("question b")


def test_id_is_filesystem_safe_slug_with_hash_suffix() -> None:
    answer_id = standing_answer_id("What's my status, Chef?!")
    assert answer_id.replace("-", "").isalnum()
    assert "/" not in answer_id


def test_similar_questions_that_slugify_the_same_still_get_distinct_ids() -> None:
    # "A B" and "a-b" both slugify to "a-b", but are different question text.
    assert standing_answer_id("A B") != standing_answer_id("a-b")


# ──────────────────────────────────────────────────────────────────────────
# Watermark staleness: true/false
# ──────────────────────────────────────────────────────────────────────────


def test_stale_when_newer_memory_exists_in_scope() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="a", last_refreshed_at=T0, last_memory_seen_at=T0
    )
    assert answer.is_stale(T1) is True


def test_fresh_when_nothing_newer_in_scope() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="a", last_refreshed_at=T0, last_memory_seen_at=T0
    )
    assert answer.is_stale(T0) is False


def test_fresh_when_scope_is_empty() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="a", last_refreshed_at=T0, last_memory_seen_at=T0
    )
    assert answer.is_stale(None) is False


def test_stale_when_never_seen_anything_and_scope_nonempty() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="", last_refreshed_at=T0, last_memory_seen_at=None
    )
    assert answer.is_stale(T0) is True


def test_is_stale_accepts_a_source_object_instead_of_a_bare_datetime() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="a", last_refreshed_at=T0, last_memory_seen_at=T0
    )

    class _Source:
        def newest_memory_timestamp(self) -> datetime | None:
            return T1

    assert answer.is_stale(_Source()) is True


def test_is_stale_rejects_something_that_is_neither_datetime_nor_source() -> None:
    answer = StandingAnswer(
        id="x", question="q", answer="a", last_refreshed_at=T0, last_memory_seen_at=T0
    )
    with pytest.raises(TypeError):
        answer.is_stale("not a datetime or source")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────
# refresh_if_stale: no-op when fresh
# ──────────────────────────────────────────────────────────────────────────


def test_refresh_if_stale_is_noop_when_fresh(store: StandingAnswerStore) -> None:
    compute = _CountingCompute(newest=T0)
    store.refresh_if_stale("what's open?", T0, compute, now=T0)
    assert compute.calls == 1

    outcome = store.refresh_if_stale("what's open?", T0, compute, now=T0)

    assert outcome.refreshed is False
    assert outcome.reason == "not_stale"
    assert compute.calls == 1  # compute was NOT invoked again


def test_refresh_if_stale_reports_noop_in_its_return_value(store: StandingAnswerStore) -> None:
    compute = _CountingCompute(newest=T0)
    store.refresh_if_stale("q", T0, compute, now=T0)
    outcome = store.refresh_if_stale("q", T0, compute, now=T0)
    assert isinstance(outcome, RefreshOutcome)
    assert outcome.refreshed is False


# ──────────────────────────────────────────────────────────────────────────
# refresh_if_stale: success path advances the watermark honestly
# ──────────────────────────────────────────────────────────────────────────


def test_refresh_if_stale_runs_compute_and_advances_watermark(
    store: StandingAnswerStore,
) -> None:
    compute = _CountingCompute(answer="the answer", newest=T1)
    outcome = store.refresh_if_stale("what's open?", T1, compute, now=T1)

    assert compute.calls == 1
    assert outcome.refreshed is True
    assert outcome.reason == "refreshed"
    assert outcome.answer.answer == "the answer"
    assert outcome.answer.last_memory_seen_at == T1
    assert outcome.answer.last_refreshed_at == T1
    assert outcome.answer.last_refresh_failed_at is None


# ──────────────────────────────────────────────────────────────────────────
# A failed refresh leaves the watermark exactly where it was
# ──────────────────────────────────────────────────────────────────────────


def test_failed_refresh_leaves_watermark_unchanged(store: StandingAnswerStore) -> None:
    good = _CountingCompute(answer="original", newest=T0)
    first = store.refresh_if_stale("q", T0, good, now=T0)
    assert first.answer.last_memory_seen_at == T0
    assert first.answer.answer == "original"

    failing = _FailingCompute()
    outcome = store.refresh_if_stale("q", T1, failing, now=T1)

    assert failing.calls == 1
    assert outcome.refreshed is False
    assert outcome.reason.startswith("failed:")
    # Watermark and answer text are untouched by the failure.
    assert outcome.answer.last_memory_seen_at == T0
    assert outcome.answer.answer == "original"
    assert outcome.answer.last_refresh_failed_at == T1

    # And the failure is durable: reading it back shows the same thing.
    reread = store.get("q")
    assert reread is not None
    assert reread.last_memory_seen_at == T0
    assert reread.answer == "original"
    assert reread.last_refresh_failed_at == T1


def test_failed_refresh_never_raises_out_of_refresh_if_stale(
    store: StandingAnswerStore,
) -> None:
    """A bad compute must not take down a boot-time sweep over many answers."""
    failing = _FailingCompute()
    outcome = store.refresh_if_stale("q", T0, failing, now=T0)
    assert outcome.refreshed is False
    assert "retrieval backend unreachable" in outcome.reason


# ──────────────────────────────────────────────────────────────────────────
# A refresh that finds nothing does not hide later memories
# ──────────────────────────────────────────────────────────────────────────


def test_refresh_that_sees_nothing_does_not_hide_a_later_memory(
    store: StandingAnswerStore,
) -> None:
    # First refresh genuinely observes memories up to T0.
    first_pass = _CountingCompute(answer="a1", newest=T0)
    store.refresh_if_stale("q", T0, first_pass, now=T0)

    # A memory newer than T0 now exists (T1). The answer is correctly stale.
    before = store.get("q")
    assert before.is_stale(T1) is True

    # A second refresh runs (because it was stale) but its compute pass
    # legitimately cannot vouch for having seen anything newer -- e.g. its
    # retrieval returned zero results and it has no honest scan boundary to
    # report. It must NOT silently claim to have seen up to T1.
    nothing_found = _CountingCompute(answer="a1", newest=None)
    outcome = store.refresh_if_stale("q", T1, nothing_found, now=T1)

    assert nothing_found.calls == 1
    assert outcome.refreshed is True  # it did run, and did update last_refreshed_at
    assert outcome.answer.last_memory_seen_at == T0  # watermark NOT advanced to T1

    # The memory at T1 is therefore still correctly detected as unseen.
    after = store.get("q")
    assert after.is_stale(T1) is True

    # Only once a pass genuinely reports having seen up to T1 does the
    # watermark advance and the answer become fresh for T1.
    sees_it = _CountingCompute(answer="a2", newest=T1)
    store.refresh_if_stale("q", T1, sees_it, now=T1)
    assert store.get("q").is_stale(T1) is False


def test_watermark_only_advances_never_regresses(store: StandingAnswerStore) -> None:
    """A compute reporting an older timestamp than the stored watermark must
    not rewind it -- that would un-hide something already accounted for.
    """
    ahead = _CountingCompute(answer="a1", newest=T2)
    store.refresh_if_stale("q", T2, ahead, now=T2)
    assert store.get("q").last_memory_seen_at == T2

    stale_check_newest = T2 + timedelta(seconds=1)
    behind = _CountingCompute(answer="a2", newest=T0)
    store.refresh_if_stale("q", stale_check_newest, behind, now=stale_check_newest)
    # Watermark stays at the max ever observed (T2), not the lesser T0.
    assert store.get("q").last_memory_seen_at == T2


# ──────────────────────────────────────────────────────────────────────────
# Reading is a plain read: zero retrieval calls
# ──────────────────────────────────────────────────────────────────────────


def test_reading_an_answer_never_calls_compute(store: StandingAnswerStore) -> None:
    compute = _CountingCompute(newest=T0)
    store.refresh_if_stale("q", T0, compute, now=T0)
    assert compute.calls == 1

    for _ in range(10):
        got = store.get("q")
        assert got is not None

    # Ten plain reads. Zero additional calls to the thing that does retrieval.
    assert compute.calls == 1


def test_get_of_unregistered_question_returns_none(store: StandingAnswerStore) -> None:
    assert store.get("never asked") is None


def test_list_answers_is_also_a_plain_read(store: StandingAnswerStore) -> None:
    compute = _CountingCompute(newest=T0)
    store.refresh_if_stale("q1", T0, compute, now=T0)
    store.refresh_if_stale("q2", T0, compute, now=T0)
    assert compute.calls == 2

    answers = store.list_answers()
    assert len(answers) == 2
    assert compute.calls == 2  # list_answers didn't call compute again


# ──────────────────────────────────────────────────────────────────────────
# Durable write round-trip via atomic_io
# ──────────────────────────────────────────────────────────────────────────


def test_register_writes_through_atomic_write_text(
    store: StandingAnswerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real_atomic_write_text = atomic_io.atomic_write_text

    def spy(path: Path, text: str, encoding: str = "utf-8") -> None:
        calls.append(Path(path))
        real_atomic_write_text(path, text, encoding)

    monkeypatch.setattr("skmemory.standing.atomic_write_text", spy)

    store.register("does this use atomic_write_text?")

    assert len(calls) == 1
    assert calls[0].suffix == ".json"


def test_write_then_read_round_trips_all_fields(store: StandingAnswerStore) -> None:
    compute = _CountingCompute(answer="the full answer", newest=T1)
    store.refresh_if_stale("round trip question", T1, compute, now=T1)

    reloaded = store.get("round trip question")
    assert reloaded is not None
    assert reloaded.question == "round trip question"
    assert reloaded.answer == "the full answer"
    assert reloaded.last_memory_seen_at == T1
    assert reloaded.last_refreshed_at == T1
    assert reloaded.last_refresh_failed_at is None
    assert reloaded.id == standing_answer_id("round trip question")


def test_no_leftover_tmp_files_after_a_write(
    store: StandingAnswerStore, standing_dir: Path
) -> None:
    store.register("clean write")
    leftovers = [p for p in standing_dir.iterdir() if not p.name.endswith(".json")]
    assert leftovers == []


# ──────────────────────────────────────────────────────────────────────────
# Deterministic serialization
# ──────────────────────────────────────────────────────────────────────────


def test_serialization_is_byte_identical_for_unchanged_answer(
    store: StandingAnswerStore, standing_dir: Path
) -> None:
    store.register("deterministic question", now=T0)
    answer_id = standing_answer_id("deterministic question")
    path = standing_dir / f"{answer_id}.json"
    first_bytes = path.read_bytes()

    # Re-save the identical, unchanged object: must write byte-identical JSON.
    answer = store.get("deterministic question")
    store._save(answer)
    second_bytes = path.read_bytes()

    assert first_bytes == second_bytes


def test_serialized_json_has_sorted_keys(store: StandingAnswerStore, standing_dir: Path) -> None:
    store.register("sorted keys question", now=T0)
    answer_id = standing_answer_id("sorted keys question")
    path = standing_dir / f"{answer_id}.json"
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())


# ──────────────────────────────────────────────────────────────────────────
# register() is idempotent
# ──────────────────────────────────────────────────────────────────────────


def test_register_is_idempotent_and_never_overwrites(store: StandingAnswerStore) -> None:
    first = store.register("idempotent question", now=T0)
    compute = _CountingCompute(answer="refreshed text", newest=T1)
    store.refresh_if_stale("idempotent question", T1, compute, now=T1)

    second = store.register("idempotent question", now=T2)

    assert second.answer == "refreshed text"
    assert second.last_memory_seen_at == T1
    assert first.id == second.id


# ──────────────────────────────────────────────────────────────────────────
# The injected runner is actually used
# ──────────────────────────────────────────────────────────────────────────


def test_injected_runner_is_actually_invoked(standing_dir: Path) -> None:
    seen: dict = {"called": 0, "pass_fn": None}

    def mock_runner(pass_fn):
        seen["called"] += 1
        seen["pass_fn"] = pass_fn
        return pass_fn()

    store = StandingAnswerStore(runner=mock_runner)
    compute = _CountingCompute(answer="via injected runner", newest=T0)

    outcome = store.refresh_if_stale("q", T0, compute, now=T0)

    assert seen["called"] == 1
    assert callable(seen["pass_fn"])
    assert outcome.answer.answer == "via injected runner"
    assert compute.calls == 1


def test_injected_runner_result_is_returned_unmodified(standing_dir: Path) -> None:
    canned = ComputedAnswer(answer="canned, never computed", newest_memory_seen_at=T1)

    def mock_runner(pass_fn):  # does NOT call pass_fn
        return canned

    compute = _CountingCompute(answer="should never appear")
    store = StandingAnswerStore(runner=mock_runner)

    outcome = store.refresh_if_stale("q", T1, compute, now=T1)

    assert outcome.answer.answer == "canned, never computed"
    assert compute.calls == 0  # the real compute never ran


def test_default_runner_is_in_process_and_works_offline(standing_dir: Path) -> None:
    """With no runner injected, refresh still works: zero dependencies."""
    store = StandingAnswerStore()
    compute = _CountingCompute(answer="in-process default", newest=T0)
    outcome = store.refresh_if_stale("q", T0, compute, now=T0)
    assert outcome.refreshed is True
    assert outcome.answer.answer == "in-process default"
    assert compute.calls == 1


def test_injected_runner_exception_is_caught_and_recorded_as_failure(
    standing_dir: Path,
) -> None:
    def boom_runner(pass_fn):
        raise RuntimeError("fresh-context spawn failed")

    store = StandingAnswerStore(runner=boom_runner)
    compute = _CountingCompute()

    outcome = store.refresh_if_stale("q", T0, compute, now=T0)

    assert outcome.refreshed is False
    assert "fresh-context spawn failed" in outcome.reason
    assert outcome.answer.last_refresh_failed_at == T0
