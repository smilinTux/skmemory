"""The seal must survive the model's own title normalisation.

``Memory.title`` is stripped by a validator when a memory is built or loaded,
but assigning ``memory.title = "x "`` bypasses it. A seal computed over the
unstripped value then never matches the value every later load sees, so
``fortress verify`` flags the memory as tampered. Found on lumina 2026-11-13
(8015d990, an observation whose title is ``statement[:80]`` and ended in a space).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.observations import sweep_observations
from skmemory.store import MemoryStore

# statement[:80] of "A recurring pattern connects 2 memories around '<key>'." ends in a
# space for this key, which is the shape that produced the 2026-11-13 victim.
KEY = "a7 cym caring corp adrenochrome hoax"


class TestSealCoversTheNormalisedTitle:
    def test_assigned_title_with_trailing_space_verifies_after_reload(self):
        m = Memory(title="plain", content="body text")
        m.title = "plain "  # bypasses the validator, as refresh_observation did
        m.seal()
        reloaded = Memory.from_stored(m.model_dump(mode="json"))
        assert reloaded.title == "plain"
        assert reloaded.verify_integrity()

    def test_assigned_title_with_leading_space_verifies_after_reload(self):
        m = Memory(title="plain", content="body text")
        m.title = "\n plain"
        m.seal()
        assert Memory.from_stored(m.model_dump(mode="json")).verify_integrity()

    def test_content_change_is_still_detected(self):
        m = Memory(title="plain", content="body text")
        m.seal()
        m.content = "body text, edited"
        assert not m.verify_integrity()

    def test_title_change_is_still_detected(self):
        m = Memory(title="plain", content="body text")
        m.seal()
        m.title = "other"
        assert not m.verify_integrity()

    def test_unstripped_title_hashes_like_the_stripped_one(self):
        a = Memory(title="plain", content="body text")
        b = a.model_copy(deep=True)
        b.title = "  plain  "
        assert a.compute_integrity_hash() == b.compute_integrity_hash()


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(primary=SQLiteBackend(base_path=tmp_path / "memories"))


def _evidence(store: MemoryStore, suffix: str) -> Memory:
    m = store.snapshot(
        title=f"digest {suffix}",
        content=f"alpha beta gamma delta epsilon zeta eta theta {suffix}",
        tags=[KEY],
        layer=MemoryLayer.SHORT,
    )
    m.context_tag = "@public"
    store.primary.save(m)
    return m


class TestObservationRefreshKeepsTheSeal:
    def test_title_really_ends_in_a_space(self):
        statement = f"A recurring pattern connects 2 memories around '{KEY}'."
        assert statement[:80].endswith(" ")

    def test_refreshed_observation_verifies_and_does_not_churn(self, store):
        _evidence(store, "one")
        _evidence(store, "two")
        assert sweep_observations(store)["written"] == 1

        _evidence(store, "three")
        out = sweep_observations(store)
        assert out["updated"] == 1

        observed = store.list_memories(tags=["observation"], limit=10)
        assert len(observed) == 1
        assert observed[0].verify_integrity(), "refresh left a seal that never matches a load"

        # Nothing changed since: a second sweep must not rewrite the file again.
        again = sweep_observations(store)
        assert again["updated"] == 0

    def test_whole_store_verifies_after_the_sweeps(self, store):
        for s in ("one", "two", "three"):
            _evidence(store, s)
        sweep_observations(store)
        _evidence(store, "four")
        sweep_observations(store)
        bad = [m.id for m in store.primary.list_memories(limit=100) if not m.verify_integrity()]
        assert bad == []

    def test_observation_title_is_stored_stripped(self, store):
        _evidence(store, "one")
        _evidence(store, "two")
        sweep_observations(store)
        _evidence(store, "three")
        sweep_observations(store)
        (obs,) = store.list_memories(tags=["observation"], limit=10)
        raw = store.primary._find_file(obs.id).read_text()
        assert f'"title": "{obs.title}"' in raw
        assert obs.title == obs.title.strip()
