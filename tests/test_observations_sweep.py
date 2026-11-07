"""Observation sweep: citation gate, context-tag inheritance, caps, kill switch."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import MemoryLayer
from skmemory.observations import (
    Observation,
    ObservationEvidence,
    citation_gate,
    inherited_context_tag,
    sweep_observations,
    write_observation,
)
from skmemory.promotion import PromotionCriteria, PromotionEngine
from skmemory.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(primary=FileBackend(base_path=tmp_path / "memories"))


def _mem(store, title, content, tag, ctx="@public"):
    m = store.snapshot(title=title, content=content, tags=[tag], layer=MemoryLayer.SHORT)
    m.context_tag = ctx
    store.primary.save(m)
    return m


def _ev(m, quote=None):
    return ObservationEvidence(
        memory_id=m.id, quote=quote or m.content, timestamp=datetime.now(timezone.utc)
    )


def _one(quote="the quick brown fox"):
    return [ObservationEvidence(memory_id="m1", quote=quote, timestamp=datetime.now(timezone.utc))]


class TestInheritedTag:
    def test_most_restrictive_wins(self):
        assert inherited_context_tag(["@public", "@inner-circle", "@community"]) == "@inner-circle"

    def test_distinct_scoped_tags_escalate(self):
        assert inherited_context_tag(["@work:a", "@work:b"]) == "@chef-only"

    def test_same_scoped_tag_kept(self):
        assert inherited_context_tag(["@work:a", "@public"]) == "@work:a"

    def test_empty_and_unknown_are_chef_only(self):
        assert inherited_context_tag([]) == "@chef-only"
        assert inherited_context_tag(["@public", "@weird"]) == "@chef-only"


class TestCitationGate:
    def test_passes_with_verbatim_quote(self):
        assert (
            citation_gate('says "the quick brown fox"', _one(), {"m1": "x the quick brown fox"})
            == []
        )

    def test_missing_memory_refused(self):
        assert citation_gate("the quick brown fox", _one(), {})

    def test_fabricated_quote_refused(self):
        assert citation_gate("the quick brown fox", _one(), {"m1": "real content only"})

    def test_text_without_quote_refused(self):
        assert citation_gate("a summary with no quote", _one(), {"m1": "the quick brown fox"})

    def test_no_evidence_refused(self):
        assert citation_gate("anything", [], {})


class TestWriteObservation:
    def test_private_evidence_makes_private_observation(self, store):
        a = _mem(store, "a", "public chatter about the garden", "garden", "@public")
        b = _mem(store, "b", "private worry about the garden bed", "garden", "@chef-only")
        obs = Observation(statement="Garden recurs.", evidence=[_ev(a), _ev(b)])
        mem = write_observation(store, obs, key="garden")
        assert mem is not None
        assert mem.context_tag == "@chef-only"
        loaded = store.primary.load(mem.id)
        assert loaded.context_tag == "@chef-only"
        assert loaded.kind == "fact"
        assert loaded.source_tier == "self-generated"
        assert loaded.metadata["observation"]["proof_count"] == 2

    def test_summary_not_visible_to_public_reader(self, store):
        a = _mem(store, "a", "public chatter about the garden", "garden", "@public")
        b = _mem(store, "b", "private worry about the garden bed", "garden", "@chef-only")
        mem = write_observation(store, Observation(statement="G.", evidence=[_ev(a), _ev(b)]))
        from skmemory.audience import AudienceLevel, tag_to_level

        assert tag_to_level(mem.context_tag) > AudienceLevel.PUBLIC

    def test_all_public_stays_public(self, store):
        a = _mem(store, "a", "public chatter about the garden", "garden")
        b = _mem(store, "b", "more public garden talk", "garden")
        mem = write_observation(store, Observation(statement="G.", evidence=[_ev(a), _ev(b)]))
        assert mem.context_tag == "@public"

    def test_fabricated_evidence_not_written(self, store):
        a = _mem(store, "a", "public chatter about the garden", "garden")
        bad = _ev(a, quote="never said this phrase")
        assert write_observation(store, Observation(statement="G.", evidence=[bad])) is None
        assert store.list_memories(tags=["observation"]) == []

    def test_raw_memories_untouched(self, store):
        a = _mem(store, "a", "public chatter about the garden", "garden")
        before = store.primary.load(a.id).model_dump_json()
        write_observation(store, Observation(statement="G.", evidence=[_ev(a)]))
        assert store.primary.load(a.id).model_dump_json() == before


class TestSweep:
    def _seed(self, store, n_groups=3):
        for g in range(n_groups):
            for i in range(2):
                _mem(store, f"t{g}{i}", f"memory number {i} about topic{g}", f"topic{g}")

    def test_cap_limits_new_observations(self, store):
        self._seed(store, 3)
        assert sweep_observations(store, max_new=2)["written"] == 2

    def test_rerun_does_not_duplicate(self, store):
        self._seed(store, 1)
        sweep_observations(store)
        assert sweep_observations(store)["written"] == 0
        assert len(store.list_memories(tags=["observation"])) == 1


class TestRefresh:
    def _setup(self, store, n=3):
        ms = [_mem(store, f"t{i}", f"memory number {i} about topicz", "topicz") for i in range(n)]
        sweep_observations(store)
        (obs,) = store.list_memories(tags=["observation"])
        return ms, obs

    def test_proof_count_not_truncated_and_grows(self, store):
        ms, obs = self._setup(store, 7)
        assert obs.metadata["observation"]["proof_count"] == 7
        assert "connects 7 memories" in obs.title
        _mem(store, "t8", "memory number 8 about topicz", "topicz")
        assert sweep_observations(store)["updated"] == 1
        (obs,) = store.list_memories(tags=["observation"])
        assert obs.metadata["observation"]["proof_count"] == 8
        assert obs.verify_integrity()

    def test_forgotten_evidence_drops_count_and_stale_quote(self, store):
        ms, obs = self._setup(store, 3)
        store.primary.delete(ms[2].id)
        assert sweep_observations(store)["updated"] == 1
        (obs,) = store.list_memories(tags=["observation"])
        assert obs.metadata["observation"]["proof_count"] == 2
        assert ms[2].content not in obs.content

    def test_below_minimum_closes_not_deletes(self, store):
        ms, obs = self._setup(store, 2)
        store.primary.delete(ms[1].id)
        assert sweep_observations(store)["closed"] == 1
        (obs,) = store.list_memories(tags=["observation"])
        assert obs.valid_to is not None
        # closed stays closed, no recreate
        assert sweep_observations(store)["written"] == 0

    def test_idempotent_and_no_rewrite_when_unchanged(self, store):
        ms, obs = self._setup(store, 3)
        path = next(Path(store.primary.base_path).rglob(f"{obs.id}.json"))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        out = sweep_observations(store)
        assert out["updated"] == 0 and out["closed"] == 0
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_tag_relaxes_when_private_evidence_forgotten(self, store):
        _mem(store, "a", "public thing about topicq", "topicq")
        _mem(store, "b", "another public topicq note", "topicq")
        c = _mem(store, "c", "private topicq worry here", "topicq", "@chef-only")
        sweep_observations(store)
        (obs,) = store.list_memories(tags=["observation"])
        assert obs.context_tag == "@chef-only"
        store.primary.delete(c.id)
        sweep_observations(store)
        (obs,) = store.list_memories(tags=["observation"])
        assert obs.context_tag == "@public"


class TestEngineIntegration:
    def _two(self, store):
        for i in range(2):
            _mem(store, f"t{i}", f"memory {i} about topicx", "topicx")

    def test_sweep_writes_and_reports(self, store, monkeypatch):
        monkeypatch.delenv("SKMEMORY_OBSERVATIONS", raising=False)
        self._two(store)
        assert PromotionEngine(store).sweep().observations_written == 1

    def test_kill_switch(self, store, monkeypatch):
        monkeypatch.setenv("SKMEMORY_OBSERVATIONS", "0")
        self._two(store)
        assert PromotionEngine(store).sweep().observations_written == 0
        assert store.list_memories(tags=["observation"]) == []

    def test_zero_cap_disables(self, store, monkeypatch):
        monkeypatch.delenv("SKMEMORY_OBSERVATIONS", raising=False)
        self._two(store)
        eng = PromotionEngine(store, PromotionCriteria(max_observations_per_sweep=0))
        assert eng.sweep().observations_written == 0

    def test_failure_does_not_fail_sweep(self, store, monkeypatch):
        monkeypatch.delenv("SKMEMORY_OBSERVATIONS", raising=False)

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr("skmemory.observations.sweep_observations", boom)
        self._two(store)
        assert PromotionEngine(store).sweep().observations_written == 0

    def test_observation_never_promoted(self, store):
        m = _mem(store, "a", "memory about topicy here", "topicy")
        obs = write_observation(store, Observation(statement="Y.", evidence=[_ev(m)]))
        obs.emotional.intensity = 10.0
        obs.tags.append("milestone")
        assert PromotionEngine(store).evaluate(obs) is None
