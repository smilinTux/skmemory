"""Tests for the observations layer: evidence-grounded, consolidated beliefs.

Covers:
  - Trend classification for every value in TREND_VALUES, plus empty/single
    evidence edge cases.
  - proof_count / time_span properties.
  - Quote verification (including a deliberately unverifiable quote).
  - Idempotent merge, including upsert_observations across statements.
  - Deterministic serialization (byte-identical JSON for the same input).
  - Durable persistence via atomic_write_text, with a full round trip.
  - The deterministic memory-grouping builder.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skmemory.models import Memory, MemoryLayer
from skmemory.observations import (
    DEFAULT_TREND_WINDOW,
    TREND_VALUES,
    Observation,
    ObservationEvidence,
    build_observations,
    compute_trend,
    content_lookup_from_store,
    find_unverifiable_evidence,
    from_json,
    load_all_observations,
    load_observation,
    merge_evidence,
    observation_path,
    save_observation,
    to_json,
    upsert_observations,
    validate_observation,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def days_ago(n: float, *, now: datetime = NOW) -> datetime:
    return now - timedelta(days=n)


def make_evidence(memory_id: str, quote: str, ts: datetime, relevance: str | None = None):
    return ObservationEvidence(memory_id=memory_id, quote=quote, timestamp=ts, relevance=relevance)


# --------------------------------------------------------------------------- #
# ObservationEvidence validation
# --------------------------------------------------------------------------- #


class TestObservationEvidenceValidation:
    def test_rejects_empty_memory_id(self):
        with pytest.raises(ValueError):
            ObservationEvidence(memory_id="", quote="x", timestamp=NOW)

    def test_rejects_empty_quote(self):
        with pytest.raises(ValueError):
            ObservationEvidence(memory_id="m1", quote="", timestamp=NOW)

    def test_rejects_naive_timestamp(self):
        with pytest.raises(ValueError):
            ObservationEvidence(memory_id="m1", quote="x", timestamp=datetime(2026, 1, 1))

    def test_accepts_non_utc_aware_timestamp_and_normalizes(self):
        # +05:00 is aware but not UTC; the validator must convert, not reject.
        from datetime import timezone as tz

        eastish = tz(timedelta(hours=5))
        ev = ObservationEvidence(
            memory_id="m1", quote="x", timestamp=datetime(2026, 1, 1, tzinfo=eastish)
        )
        assert ev.timestamp.tzinfo == UTC

    def test_relevance_optional(self):
        ev = ObservationEvidence(memory_id="m1", quote="x", timestamp=NOW)
        assert ev.relevance is None


# --------------------------------------------------------------------------- #
# Observation basics: statement validation, proof_count, time_span
# --------------------------------------------------------------------------- #


class TestObservationBasics:
    def test_rejects_empty_statement(self):
        with pytest.raises(ValueError):
            Observation(statement="   ")

    def test_statement_is_stripped(self):
        obs = Observation(statement="  hello  ")
        assert obs.statement == "hello"

    def test_empty_evidence_proof_count_zero(self):
        obs = Observation(statement="nothing yet")
        assert obs.proof_count == 0

    def test_empty_evidence_time_span_none(self):
        obs = Observation(statement="nothing yet")
        assert obs.time_span is None

    def test_single_evidence_proof_count_one(self):
        obs = Observation(
            statement="one fact",
            evidence=[make_evidence("m1", "the exact quote", days_ago(1))],
        )
        assert obs.proof_count == 1

    def test_single_evidence_time_span_equal_bounds(self):
        ts = days_ago(1)
        obs = Observation(statement="one fact", evidence=[make_evidence("m1", "q", ts)])
        lo, hi = obs.time_span
        assert lo == hi == ts

    def test_time_span_spans_earliest_to_latest(self):
        obs = Observation(
            statement="spread out",
            evidence=[
                make_evidence("m1", "q1", days_ago(30)),
                make_evidence("m2", "q2", days_ago(1)),
                make_evidence("m3", "q3", days_ago(15)),
            ],
        )
        lo, hi = obs.time_span
        assert lo == days_ago(30)
        assert hi == days_ago(1)


# --------------------------------------------------------------------------- #
# Trend classification: every value in TREND_VALUES, plus edge cases
# --------------------------------------------------------------------------- #


class TestTrendClassification:
    def test_all_five_values_are_reachable(self):
        # Sanity: this is the exact set compute_trend may return.
        assert {"stable", "strengthening", "weakening", "new", "stale"} == TREND_VALUES

    def test_empty_evidence_is_new(self):
        assert compute_trend([], now=NOW) == "new"

    def test_everything_in_recent_window_is_new(self):
        timestamps = [days_ago(1), days_ago(3), days_ago(5)]
        assert compute_trend(timestamps, now=NOW) == "new"

    def test_single_recent_point_is_new(self):
        assert compute_trend([days_ago(2)], now=NOW) == "new"

    def test_nothing_in_recent_window_is_stale(self):
        timestamps = [days_ago(60), days_ago(75), days_ago(90)]
        assert compute_trend(timestamps, now=NOW) == "stale"

    def test_single_old_point_is_stale(self):
        assert compute_trend([days_ago(100)], now=NOW) == "stale"

    def test_recent_burst_after_sparse_history_is_strengthening(self):
        prior = [days_ago(d) for d in range(100, 40, -10)]  # 6 pts / 60 days
        recent = [days_ago(d) for d in range(1, 10)]  # 9 pts / 14 days
        assert compute_trend(prior + recent, now=NOW) == "strengthening"

    def test_dense_history_gone_quiet_is_weakening(self):
        prior = [days_ago(d) for d in range(20, 100)]  # 80 pts / ~86 days
        recent = [days_ago(1)]  # 1 pt / 14 days
        assert compute_trend(prior + recent, now=NOW) == "weakening"

    def test_matched_rates_is_stable(self):
        prior = [days_ago(d) for d in range(20, 100, 15)]
        recent = [days_ago(1)]
        assert compute_trend(prior + recent, now=NOW) == "stable"

    def test_observation_trend_property_matches_compute_trend(self):
        # The property can't take an explicit `now`, so build timestamps
        # relative to the real current time and assert against the same
        # pure function called the same way.
        real_now = datetime.now(UTC)
        timestamps = [real_now - timedelta(days=d) for d in (1, 2, 3)]
        obs = Observation(
            statement="fresh",
            evidence=[
                make_evidence("m1", "q1", timestamps[0]),
                make_evidence("m2", "q2", timestamps[1]),
            ],
        )
        assert obs.trend == "new"

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError):
            compute_trend([days_ago(1)], now=datetime(2026, 1, 1))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError):
            compute_trend([datetime(2026, 1, 1)], now=NOW)

    def test_non_positive_window_rejected(self):
        with pytest.raises(ValueError):
            compute_trend([days_ago(1)], now=NOW, window=timedelta(0))

    def test_ratio_must_exceed_one(self):
        with pytest.raises(ValueError):
            compute_trend([days_ago(1), days_ago(50)], now=NOW, strengthen_ratio=1.0)

    def test_default_window_is_documented_value(self):
        assert timedelta(days=14) == DEFAULT_TREND_WINDOW


# --------------------------------------------------------------------------- #
# Quote verification
# --------------------------------------------------------------------------- #


class TestQuoteVerification:
    def test_verified_quote_passes(self):
        content = {"m1": "The pool ceiling was raised to 32 workers after review."}
        obs = Observation(
            statement="capacity increased",
            evidence=[make_evidence("m1", "pool ceiling was raised to 32 workers", days_ago(1))],
        )
        assert find_unverifiable_evidence(obs, content) == []
        assert validate_observation(obs, content) is True

    def test_fabricated_quote_is_caught(self):
        content = {"m1": "The pool ceiling was raised to 32 workers after review."}
        bad = make_evidence("m1", "the pool ceiling was raised to 64 workers", days_ago(1))
        obs = Observation(statement="capacity increased", evidence=[bad])
        unverifiable = find_unverifiable_evidence(obs, content)
        assert unverifiable == [bad]
        assert validate_observation(obs, content) is False

    def test_missing_memory_is_unverifiable(self):
        content: dict[str, str] = {}
        ev = make_evidence("ghost", "anything", days_ago(1))
        obs = Observation(statement="claim about a ghost memory", evidence=[ev])
        assert find_unverifiable_evidence(obs, content) == [ev]

    def test_mixed_verified_and_fabricated_catches_only_the_fabricated_one(self):
        content = {
            "m1": "chiap08 hosts the beelama endpoint.",
            "m2": "The nightly backup job runs at 2am.",
        }
        genuine = make_evidence("m1", "chiap08 hosts the beelama endpoint", days_ago(2))
        fabricated = make_evidence(
            "m2", "the nightly backup runs at 3am and takes an hour", days_ago(1)
        )
        obs = Observation(statement="multi-part claim", evidence=[genuine, fabricated])
        unverifiable = find_unverifiable_evidence(obs, content)
        assert unverifiable == [fabricated]

    def test_accepts_callable_lookup(self):
        def lookup(memory_id: str) -> str | None:
            return {"m1": "exact content here"}.get(memory_id)

        ev = make_evidence("m1", "exact content", days_ago(1))
        obs = Observation(statement="callable-backed claim", evidence=[ev])
        assert find_unverifiable_evidence(obs, lookup) == []

    def test_content_lookup_from_store_adapts_recall(self):
        class FakeMemory:
            def __init__(self, content: str) -> None:
                self.content = content

        class FakeStore:
            def recall(self, memory_id: str):
                if memory_id == "m1":
                    return FakeMemory("the real content of memory one")
                return None

        lookup = content_lookup_from_store(FakeStore())
        assert lookup("m1") == "the real content of memory one"
        assert lookup("missing") is None

        ev_good = make_evidence("m1", "real content of memory one", days_ago(1))
        ev_bad = make_evidence("missing", "whatever", days_ago(1))
        obs = Observation(statement="store-backed claim", evidence=[ev_good, ev_bad])
        unverifiable = find_unverifiable_evidence(obs, lookup)
        assert unverifiable == [ev_bad]

    def test_empty_evidence_is_vacuously_valid(self):
        obs = Observation(statement="no evidence yet")
        assert find_unverifiable_evidence(obs, {}) == []
        assert validate_observation(obs, {}) is True


# --------------------------------------------------------------------------- #
# Merge idempotency and refinement
# --------------------------------------------------------------------------- #


class TestMergeEvidence:
    def test_merge_adds_new_evidence(self):
        base = Observation(statement="s")
        batch = [make_evidence("m1", "q1", days_ago(1))]
        merged = merge_evidence(base, batch)
        assert merged.proof_count == 1
        assert merged.statement == base.statement

    def test_merge_does_not_mutate_original(self):
        base = Observation(statement="s")
        batch = [make_evidence("m1", "q1", days_ago(1))]
        merge_evidence(base, batch)
        assert base.proof_count == 0

    def test_merge_is_idempotent_applying_same_batch_twice(self):
        base = Observation(statement="s")
        batch = [
            make_evidence("m1", "q1", days_ago(5)),
            make_evidence("m2", "q2", days_ago(2)),
        ]
        once = merge_evidence(base, batch)
        twice = merge_evidence(once, batch)
        assert once.proof_count == 2
        assert twice.proof_count == once.proof_count == 2

    def test_merge_dedupes_on_memory_id_and_quote_not_identity(self):
        base = Observation(statement="s")
        ev_a = make_evidence("m1", "same quote text", days_ago(5))
        # A distinct object, but same (memory_id, quote) key.
        ev_b = make_evidence("m1", "same quote text", days_ago(5))
        merged = merge_evidence(merge_evidence(base, [ev_a]), [ev_b])
        assert merged.proof_count == 1

    def test_merge_keeps_evidence_with_same_memory_different_quote(self):
        base = Observation(statement="s")
        ev_a = make_evidence("m1", "first quote", days_ago(5))
        ev_b = make_evidence("m1", "second quote", days_ago(4))
        merged = merge_evidence(base, [ev_a, ev_b])
        assert merged.proof_count == 2

    def test_merge_result_is_sorted_deterministically(self):
        base = Observation(statement="s")
        ev_late = make_evidence("m2", "late", days_ago(1))
        ev_early = make_evidence("m1", "early", days_ago(10))
        merged = merge_evidence(base, [ev_late, ev_early])
        assert [e.memory_id for e in merged.evidence] == ["m1", "m2"]


class TestUpsertObservations:
    def test_new_statement_is_added(self):
        existing: list[Observation] = []
        update = Observation(
            statement="brand new", evidence=[make_evidence("m1", "q", days_ago(1))]
        )
        result = upsert_observations(existing, [update])
        assert len(result) == 1
        assert result[0].statement == "brand new"

    def test_existing_statement_is_refined_not_replaced(self):
        original = Observation(
            statement="shared belief", evidence=[make_evidence("m1", "q1", days_ago(10))]
        )
        update = Observation(
            statement="shared belief", evidence=[make_evidence("m2", "q2", days_ago(1))]
        )
        result = upsert_observations([original], [update])
        assert len(result) == 1
        assert result[0].proof_count == 2

    def test_upsert_is_idempotent(self):
        original = Observation(
            statement="shared belief", evidence=[make_evidence("m1", "q1", days_ago(10))]
        )
        update = Observation(
            statement="shared belief", evidence=[make_evidence("m2", "q2", days_ago(1))]
        )
        once = upsert_observations([original], [update])
        twice = upsert_observations(once, [update])
        assert once[0].proof_count == twice[0].proof_count == 2

    def test_result_sorted_by_statement(self):
        a = Observation(statement="bbb")
        b = Observation(statement="aaa")
        result = upsert_observations([], [a, b])
        assert [o.statement for o in result] == ["aaa", "bbb"]


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


class TestSerialization:
    def test_to_json_is_byte_identical_across_calls(self):
        obs = Observation(
            statement="determinism check",
            evidence=[
                make_evidence("m2", "second", days_ago(1)),
                make_evidence("m1", "first", days_ago(10)),
            ],
        )
        assert to_json(obs) == to_json(obs)

    def test_to_json_independent_of_evidence_insertion_order(self):
        ev1 = make_evidence("m1", "first", days_ago(10))
        ev2 = make_evidence("m2", "second", days_ago(1))
        obs_a = Observation(statement="order check", evidence=[ev1, ev2])
        obs_b = Observation(statement="order check", evidence=[ev2, ev1])
        assert to_json(obs_a) == to_json(obs_b)

    def test_json_keys_sorted(self):
        obs = Observation(statement="keys", evidence=[make_evidence("m1", "q", days_ago(1))])
        payload = json.loads(to_json(obs))
        assert list(payload.keys()) == sorted(payload.keys())
        assert list(payload["evidence"][0].keys()) == sorted(payload["evidence"][0].keys())

    def test_round_trip_preserves_data(self):
        obs = Observation(
            statement="round trip",
            evidence=[make_evidence("m1", "q1", days_ago(5), relevance="context")],
        )
        restored = from_json(to_json(obs))
        assert restored.statement == obs.statement
        assert restored.proof_count == obs.proof_count
        assert restored.evidence[0].memory_id == "m1"
        assert restored.evidence[0].quote == "q1"
        assert restored.evidence[0].relevance == "context"
        assert restored.evidence[0].timestamp == obs.evidence[0].timestamp

    def test_trend_and_proof_count_not_persisted_as_fields(self):
        # Only statement + evidence are stored; trend/proof_count/time_span
        # are derived, not frozen into the file.
        obs = Observation(
            statement="derived fields", evidence=[make_evidence("m1", "q", days_ago(1))]
        )
        payload = json.loads(to_json(obs))
        assert set(payload.keys()) == {"statement", "evidence"}


# --------------------------------------------------------------------------- #
# Durable persistence
# --------------------------------------------------------------------------- #


class TestPersistence:
    def test_save_writes_file_that_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        obs = Observation(
            statement="persisted belief", evidence=[make_evidence("m1", "q1", days_ago(1))]
        )
        path = save_observation(obs)
        assert path.exists()
        assert path.parent == tmp_path / "memory" / "observations"

    def test_save_then_load_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        obs = Observation(
            statement="round trip through disk",
            evidence=[
                make_evidence("m1", "q1", days_ago(3)),
                make_evidence("m2", "q2", days_ago(1)),
            ],
        )
        path = save_observation(obs)
        loaded = load_observation(path)
        assert loaded.statement == obs.statement
        assert loaded.proof_count == obs.proof_count
        assert {e.memory_id for e in loaded.evidence} == {"m1", "m2"}

    def test_save_uses_atomic_write_no_leftover_tmp_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        obs = Observation(
            statement="no tmp leftovers", evidence=[make_evidence("m1", "q", days_ago(1))]
        )
        path = save_observation(obs)
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_observation_path_is_deterministic_for_same_statement(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        p1 = observation_path("same statement")
        p2 = observation_path("same statement")
        assert p1 == p2

    def test_observation_path_differs_for_different_statements(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        assert observation_path("statement one") != observation_path("statement two")

    def test_load_all_observations_empty_when_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        assert load_all_observations() == []

    def test_load_all_observations_sorted_by_statement(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        save_observation(
            Observation(statement="zzz last", evidence=[make_evidence("m1", "q", days_ago(1))])
        )
        save_observation(
            Observation(statement="aaa first", evidence=[make_evidence("m2", "q", days_ago(1))])
        )
        loaded = load_all_observations()
        assert [o.statement for o in loaded] == ["aaa first", "zzz last"]

    def test_save_is_idempotent_on_disk_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        obs = Observation(
            statement="save twice", evidence=[make_evidence("m1", "q1", days_ago(1))]
        )
        path1 = save_observation(obs)
        path2 = save_observation(obs)
        assert path1 == path2
        assert load_observation(path1).proof_count == 1


# --------------------------------------------------------------------------- #
# Building observations from memories (deterministic grouping)
# --------------------------------------------------------------------------- #


def make_memory(
    content: str, tags: list[str], created_at: str, memory_id: str | None = None
) -> Memory:
    kwargs = {}
    if memory_id:
        kwargs["id"] = memory_id
    return Memory(
        title="t",
        content=content,
        tags=tags,
        layer=MemoryLayer.LONG,
        created_at=created_at,
        **kwargs,
    )


class TestBuildObservations:
    def test_groups_by_shared_tag(self):
        memories = [
            make_memory("first beelama note", ["beelama"], "2026-09-01T00:00:00+00:00"),
            make_memory("second beelama note", ["beelama"], "2026-09-02T00:00:00+00:00"),
        ]
        observations = build_observations(memories)
        assert len(observations) == 1
        assert "beelama" in observations[0].statement
        assert observations[0].proof_count == 2

    def test_below_min_group_size_is_excluded(self):
        memories = [make_memory("lone note", ["unique-tag"], "2026-09-01T00:00:00+00:00")]
        observations = build_observations(memories, min_group_size=2)
        assert observations == []

    def test_min_group_size_one_includes_singletons(self):
        memories = [make_memory("lone note", ["unique-tag"], "2026-09-01T00:00:00+00:00")]
        observations = build_observations(memories, min_group_size=1)
        assert len(observations) == 1
        assert observations[0].proof_count == 1

    def test_evidence_quotes_are_verifiable_against_source_memories(self):
        memories = [
            make_memory("alpha shared content", ["shared"], "2026-09-01T00:00:00+00:00"),
            make_memory("beta shared content", ["shared"], "2026-09-02T00:00:00+00:00"),
        ]
        observations = build_observations(memories)
        content_lookup = {m.id: m.content for m in memories}
        for obs in observations:
            assert find_unverifiable_evidence(obs, content_lookup) == []

    def test_output_sorted_by_statement(self):
        memories = [
            make_memory("x", ["zzz-tag"], "2026-09-01T00:00:00+00:00"),
            make_memory("y", ["zzz-tag"], "2026-09-02T00:00:00+00:00"),
            make_memory("p", ["aaa-tag"], "2026-09-01T00:00:00+00:00"),
            make_memory("q", ["aaa-tag"], "2026-09-02T00:00:00+00:00"),
        ]
        observations = build_observations(memories)
        statements = [o.statement for o in observations]
        assert statements == sorted(statements)

    def test_groups_by_shared_entity_in_metadata(self):
        memories = [
            Memory(
                title="t1",
                content="chiap08 hosts the beelama endpoint",
                tags=[],
                metadata={"decomposition": {"entities": ["chiap08"]}},
                created_at="2026-09-01T00:00:00+00:00",
            ),
            Memory(
                title="t2",
                content="chiap08 degraded overnight",
                tags=[],
                metadata={"decomposition": {"entities": ["chiap08"]}},
                created_at="2026-09-02T00:00:00+00:00",
            ),
        ]
        observations = build_observations(memories)
        assert len(observations) == 1
        assert "chiap08" in observations[0].statement

    def test_rejects_invalid_min_group_size(self):
        with pytest.raises(ValueError):
            build_observations([], min_group_size=0)

    def test_empty_memories_produces_empty_observations(self):
        assert build_observations([]) == []

    def test_build_then_persist_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skmemory.observations.get_agent_paths",
            lambda agent=None: {"base": tmp_path},
        )
        memories = [
            make_memory("alpha shared content", ["shared"], "2026-09-01T00:00:00+00:00"),
            make_memory("beta shared content", ["shared"], "2026-09-02T00:00:00+00:00"),
        ]
        observations = build_observations(memories)
        for obs in observations:
            save_observation(obs)
        loaded = load_all_observations()
        assert len(loaded) == len(observations)
        assert loaded[0].proof_count == observations[0].proof_count
