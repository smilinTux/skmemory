"""Tests for multi-arm retrieval fusion (R1).

The load-bearing test here is
``test_interleave_protects_a_single_arm_champion``: it pins the exact
regression that made plain RRF unusable as a final ranker, measured on
``skmemory.eval.fusion_bench`` as paraphrase recall@5 dropping from 0.833 to
0.000. If someone later simplifies ``rank_interleave`` back to plain RRF, that
test fails with the reason attached.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skmemory.fusion import (
    RRF_K,
    ArmOutcome,
    cap_per_source,
    fuse,
    parse_time_window,
    rank_interleave,
    reciprocal_rank_fusion,
    run_arm,
)

# ---------------------------------------------------------------------------
# run_arm: the four outcomes the old `if results:` could not express
# ---------------------------------------------------------------------------


def test_run_arm_unavailable_when_backend_absent():
    out = run_arm("semantic", None)
    assert out.status == "unavailable"
    assert out.ids == ()
    assert not out.contributed
    assert "not attached" in out.detail


def test_run_arm_abstained_is_distinct_from_unavailable():
    out = run_arm("temporal", lambda: [])
    assert out.status == "abstained"
    assert not out.contributed
    # The whole point: an arm that ran and found nothing is NOT the same fact
    # as an arm that was never there.
    assert out.status != run_arm("temporal", None).status


def test_run_arm_failed_records_the_error_instead_of_swallowing_it():
    def boom():
        raise RuntimeError("pg connection refused")

    out = run_arm("lexical", boom)
    assert out.status == "failed"
    assert "pg connection refused" in out.detail
    assert not out.contributed


def test_run_arm_ok_dedupes_within_arm_but_keeps_rank_order():
    out = run_arm("lexical", lambda: ["b", "a", "b", "c", "a"])
    assert out.status == "ok"
    assert out.ids == ("b", "a", "c")
    assert out.contributed


def test_run_arm_applies_the_cap():
    out = run_arm("bm25", lambda: [str(i) for i in range(100)], cap=5)
    assert len(out.ids) == 5
    assert out.ids == ("0", "1", "2", "3", "4")


def test_run_arm_drops_falsy_ids():
    out = run_arm("lexical", lambda: ["a", "", None, "b"])
    assert out.ids == ("a", "b")


# ---------------------------------------------------------------------------
# cap_per_source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ids,cap,expected",
    [
        (["a", "b", "c"], 2, ["a", "b"]),
        (["a", "b"], 5, ["a", "b"]),
        (["a", "b"], 0, ["a", "b"]),  # 0 disables the cap
        (["a", "b"], -1, ["a", "b"]),
    ],
)
def test_cap_per_source(ids, cap, expected):
    assert cap_per_source(ids, cap) == expected


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def test_rrf_scores_match_the_formula():
    fused = reciprocal_rank_fusion({"a": ["x"], "b": ["x"]})
    assert len(fused) == 1
    doc_id, score, prov = fused[0]
    assert doc_id == "x"
    # Rank 1 in both arms.
    assert score == pytest.approx(2.0 / (RRF_K + 1))
    assert prov == {"a": 1, "b": 1}


def test_rrf_rewards_agreement_across_arms():
    fused = reciprocal_rank_fusion({"a": ["agreed", "solo_a"], "b": ["agreed", "solo_b"]})
    assert fused[0][0] == "agreed"


def test_rrf_ordering_is_deterministic_on_ties():
    arms = {"a": ["x", "y"], "b": ["y", "x"]}
    first = [d for d, _, _ in reciprocal_rank_fusion(arms)]
    for _ in range(5):
        assert [d for d, _, _ in reciprocal_rank_fusion(arms)] == first
    # Tied scores break on id, so the order is stable and explainable.
    assert first == ["x", "y"]


# ---------------------------------------------------------------------------
# rank_interleave: THE regression this design exists to prevent
# ---------------------------------------------------------------------------


def test_interleave_protects_a_single_arm_champion():
    """A rank-1-in-one-arm answer must not lose to mid-pack-in-two-arms decoys.

    This is the measured failure that makes plain RRF unusable as a final
    ranker. Reproduced minimally: ``answer`` is rank 1 on the dense arm and
    absent from the lexical arm; each decoy is rank 1..3 on the lexical arm and
    mid-pack on the dense arm. Plain RRF ranks every decoy above the answer
    (0.0313 vs 0.0164 on the real fixture, final rank 17). Interleaving by rank
    depth puts the answer in the first depth, where it belongs.
    """
    arms = {
        "semantic": ["answer", "decoy1", "decoy2", "decoy3", "decoy4", "decoy5", "decoy6"],
        "lexical": ["decoy1", "decoy2", "decoy3"],
    }

    plain = [d for d, _, _ in reciprocal_rank_fusion(arms)]
    assert plain.index("answer") > plain.index("decoy1"), (
        "precondition: plain RRF is expected to bury the single-arm champion"
    )

    interleaved = [d for d, _, _ in rank_interleave(arms)]
    assert "answer" in interleaved[:2], (
        f"the single-arm champion must reach the first depth, got {interleaved[:3]}"
    )


def test_interleave_still_lets_agreement_win_its_depth():
    """Protecting champions must not throw away the multi-arm signal."""
    arms = {"semantic": ["agreed", "solo_a"], "lexical": ["agreed", "solo_b"]}
    ranked = [d for d, _, _ in rank_interleave(arms)]
    # 'agreed' is rank 1 in both arms, so it wins depth 1 outright.
    assert ranked[0] == "agreed"


def test_interleave_is_deterministic():
    arms = {"a": ["x", "y", "z"], "b": ["z", "x"]}
    first = [d for d, _, _ in rank_interleave(arms)]
    for _ in range(5):
        assert [d for d, _, _ in rank_interleave(arms)] == first


# ---------------------------------------------------------------------------
# fuse
# ---------------------------------------------------------------------------


def test_fuse_only_counts_contributing_arms():
    arms = [
        ArmOutcome(name="semantic", status="unavailable"),
        ArmOutcome(name="lexical", ids=("a", "b"), status="ok"),
        ArmOutcome(name="temporal", status="abstained"),
        ArmOutcome(name="graph", status="failed", detail="boom"),
    ]
    res = fuse(arms, limit=5)
    assert res.ids == ["a", "b"]
    assert res.contributing_arms == ["lexical"]
    assert res.failed_arms == ["graph"]


def test_fuse_with_nothing_to_fuse_returns_empty_but_keeps_the_arms():
    arms = [
        ArmOutcome(name="semantic", status="failed", detail="no embed server"),
        ArmOutcome(name="lexical", status="failed", detail="db locked"),
    ]
    res = fuse(arms, limit=5)
    assert res.ids == []
    # The caller must still be able to see WHY it got nothing. An empty result
    # with no explanation is the defect this module exists to remove.
    assert res.failed_arms == ["semantic", "lexical"]
    assert "failed" in res.summary()


def test_fuse_respects_the_limit():
    arms = [ArmOutcome(name="lexical", ids=tuple(str(i) for i in range(50)), status="ok")]
    assert len(fuse(arms, limit=7).ids) == 7


def test_fuse_records_provenance_for_what_it_returned():
    arms = [
        ArmOutcome(name="semantic", ids=("a",), status="ok"),
        ArmOutcome(name="lexical", ids=("a", "b"), status="ok"),
    ]
    res = fuse(arms, limit=5)
    assert res.provenance["a"] == {"semantic": 1, "lexical": 1}
    assert res.provenance["b"] == {"lexical": 2}


def test_fuse_summary_names_every_arm_and_its_status():
    arms = [
        ArmOutcome(name="semantic", ids=("a",), status="ok"),
        ArmOutcome(name="graph", status="unavailable"),
    ]
    summary = fuse(arms, limit=5).summary()
    assert "semantic=ok:1" in summary
    assert "graph=unavailable:0" in summary


# ---------------------------------------------------------------------------
# parse_time_window
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_time_window_named_month_defaults_to_most_recent_past():
    after, before = parse_time_window("what did we decide in June", now=_NOW)
    assert after.startswith("2026-06-01")
    assert before.startswith("2026-06-30")


def test_parse_time_window_future_month_rolls_back_a_year():
    # December has not happened yet in October 2026.
    after, _ = parse_time_window("the December outage", now=_NOW)
    assert after.startswith("2025-12-01")


def test_parse_time_window_explicit_year_wins():
    after, before = parse_time_window("the April 2026 ramp up", now=_NOW)
    assert after.startswith("2026-04-01")
    assert before.startswith("2026-04-30")


def test_parse_time_window_iso_month():
    after, before = parse_time_window("notes from 2026-03", now=_NOW)
    assert after.startswith("2026-03-01")
    assert before.startswith("2026-03-31")


def test_parse_time_window_bare_year():
    after, before = parse_time_window("everything from 2025", now=_NOW)
    assert after.startswith("2025-01-01")
    assert before.startswith("2025-12-31")


def test_parse_time_window_today_and_yesterday():
    after, _ = parse_time_window("what happened today", now=_NOW)
    assert after.startswith("2026-10-17")
    after, _ = parse_time_window("what happened yesterday", now=_NOW)
    assert after.startswith("2026-10-16")


@pytest.mark.parametrize(
    "query",
    [
        "",
        "postgres rebuild",
        "maybe we should check the index",  # 'may' inside 'maybe' must NOT match
        "marching orders for the team",  # 'mar' inside 'marching' must NOT match
    ],
)
def test_parse_time_window_abstains_without_a_real_date(query):
    """An arm that always has an opinion adds noise to every fusion.

    Substring matching here would be actively harmful: it is how a recency sort
    gets mistaken for a date filter, which is postmortem §3.11's silent-empty
    synthesis bug.
    """
    assert parse_time_window(query, now=_NOW) is None


def test_parse_time_window_bounds_are_iso_utc_and_ordered():
    after, before = parse_time_window("June 2026", now=_NOW)
    assert after.endswith("Z") and before.endswith("Z")
    assert after < before


# ---------------------------------------------------------------------------
# parse_time_window: broadened phrases (SKMEM-P0-08)
# ---------------------------------------------------------------------------

# 2026-11-06 is a Friday.
_FRI = datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)


def _win(q, now=_FRI):
    return parse_time_window(q, now=now)


@pytest.mark.parametrize(
    "query",
    [
        "the rollout may halt",
        "it may be the cache",
        "May we ship this",
        "this may fail again",
    ],
)
def test_may_modal_verb_is_not_a_month(query):
    assert _win(query) is None


@pytest.mark.parametrize(
    "query,start",
    [
        ("what shipped in May", "2026-05-01"),
        ("what shipped in may 2026", "2026-05-01"),
        ("changes in may", "2026-05-01"),
        ("May 2025 outage", "2025-05-01"),
    ],
)
def test_may_as_month_still_parses(query, start):
    after, _ = _win(query)
    assert after.startswith(start)


def test_may_with_day_number_is_that_day():
    assert _win("what happened may 5") == ("2026-05-05T00:00:00Z", "2026-05-05T23:59:59.999999Z")
    assert _win("5th of may")[0].startswith("2026-05-05")


def test_march_verb_vs_month():
    assert _win("we march on friday") is None
    assert _win("the March outage")[0].startswith("2026-03-01")


def test_iso_date():
    assert _win("notes 2026-03-14") == ("2026-03-14T00:00:00Z", "2026-03-14T23:59:59.999999Z")


def test_n_days_ago_is_that_day():
    assert _win("3 days ago") == ("2026-11-03T00:00:00Z", "2026-11-03T23:59:59.999999Z")
    assert _win("two days ago")[0].startswith("2026-11-04")


def test_n_weeks_ago_is_that_iso_week():
    # 2 weeks before Fri Nov 6 is Fri Oct 23; its Mon-Sun week is Oct 19-25.
    after, before = _win("2 weeks ago")
    assert after.startswith("2026-10-19") and before.startswith("2026-10-25")


def test_n_months_ago_is_that_calendar_month():
    after, before = _win("review queue status two months ago")
    assert after.startswith("2026-09-01") and before.startswith("2026-09-30")


def test_last_n_days_is_rolling_to_now():
    after, before = _win("what changed in the last 3 days")
    assert after.startswith("2026-11-03T15:30:00")
    assert before.startswith("2026-11-06T15:30:00")


def test_last_weekday_is_strictly_before_today():
    # Friday: last Tuesday is Nov 3, last Friday is a week ago.
    assert _win("anything from last Tuesday")[0].startswith("2026-11-03")
    assert _win("last friday")[0].startswith("2026-10-30")


def test_last_tuesday_from_sunday():
    sun = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    assert _win("anything from last Tuesday", sun)[0].startswith("2026-10-27")


def test_this_and_last_week_are_monday_to_sunday():
    a, b = _win("this week")
    assert a.startswith("2026-11-02T00:00:00") and b.startswith("2026-11-08T23:59:59")
    a, b = _win("last week")
    assert a.startswith("2026-10-26T00:00:00") and b.startswith("2026-11-01T23:59:59")
    sun = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    assert _win("this week", sun)[0].startswith("2026-10-26")


def test_last_and_this_month():
    a, b = _win("last month")
    assert a.startswith("2026-10-01") and b.startswith("2026-10-31")
    assert _win("this month")[0].startswith("2026-11-01")
    jan = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert _win("last month", jan)[0].startswith("2025-12-01")


def test_boundaries_follow_now_timezone_and_output_is_utc():
    from datetime import timedelta

    tokyo = timezone(timedelta(hours=9))
    # 2026-11-07 02:00 in Tokyo is still Nov 6 in UTC; "today" is Nov 7 locally.
    now = datetime(2026, 11, 7, 2, 0, tzinfo=tokyo)
    assert _win("today", now) == ("2026-11-06T15:00:00Z", "2026-11-07T14:59:59.999999Z")
    assert _win("yesterday", now)[0] == "2026-11-05T15:00:00Z"


def test_naive_now_is_treated_as_utc():
    assert _win("today", datetime(2026, 11, 6, 1, 0)) == _win("today")


def test_no_wall_clock_dependency():
    # Same answer regardless of when the test runs.
    assert _win("last month") == _win(
        "last month", datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)
    )


# ---------------------------------------------------------------------------
# parse_time_window: edge cases left by SKMEM-P0-08 (SKMEM-P0-17)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["step 3 may fail", "retry 2 may not help", "we saw 12 may be"])
def test_number_before_modal_may_is_not_a_date(query):
    assert _win(query) is None


@pytest.mark.parametrize(
    "query", ["5 may", "5th of may", "5 may 2026", "what happened 5th of may"]
)
def test_number_before_may_closed_off_is_a_date(query):
    after, _ = _win(query)
    assert after.startswith("2026-05-05")


def test_march_at_query_start_is_a_month():
    assert _win("March outage details")[0].startswith("2026-03-01")
    assert _win("March on Friday") is None
    assert _win("march outage details") is None


def test_past_unit_without_number_is_rolling():
    a, b = _win("the past month")
    assert a.startswith("2026-10-06T15:30:00") and b.startswith("2026-11-06T15:30:00")
    a, _ = _win("the past week")
    assert a.startswith("2026-10-30T15:30:00")
    a, _ = _win("past day")
    assert a.startswith("2026-11-05T15:30:00")


def test_last_week_of_month_is_the_month_not_the_iso_week():
    a, b = _win("last week of March")
    assert a.startswith("2026-03-01") and b.startswith("2026-03-31")
    assert _win("last week of may")[0].startswith("2026-05-01")
    assert _win("last week")[0].startswith("2026-10-26")


@pytest.mark.parametrize(
    "query",
    [
        "March madness bracket",
        "March of the Penguins",
        "March for Life",
        "chapter 4 may",
        "room 12 may",
        "scored 9 may",
        "I scored 90 may",
        "past day trading",
        "past month rent roll",
    ],
)
def test_edge_false_positives_abstain(query):
    assert _win(query) is None


def test_week_n_of_march_is_march_not_day():
    a, b = _win("week 3 of march")
    assert a.startswith("2026-03-01") and b.startswith("2026-03-31")


def test_march_event_nouns_at_start_narrow():
    assert _win("March incident review")[0].startswith("2026-03-01")
    assert _win("March 2025 notes")[0].startswith("2025-03-01")
