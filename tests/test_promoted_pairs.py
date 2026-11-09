"""A promoted memory and its source are one search hit, not two. Tmp store only."""

from __future__ import annotations

import pytest

from skmemory.audience import READER_ENV
from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.promoted_pairs import collapse_promoted_pairs
from skmemory.store import MemoryStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)


def _mem(mid, layer=MemoryLayer.SHORT, parent=None):
    meta = {"parent_relation": "promoted_from"} if parent else {}
    return Memory(id=mid, title=mid, content="c", layer=layer, parent_id=parent, metadata=meta)


def test_pair_collapses_to_the_copy_at_the_better_rank():
    src, other, copy = _mem("s"), _mem("o"), _mem("m", MemoryLayer.MID, parent="s")
    assert [m.id for m in collapse_promoted_pairs([src, other, copy])] == ["m", "o"]
    assert [m.id for m in collapse_promoted_pairs([other, copy, src])] == ["o", "m"]


def test_chain_shows_its_highest_copy_once():
    s = _mem("s")
    m = _mem("m", MemoryLayer.MID, parent="s")
    lo = _mem("l", MemoryLayer.LONG, parent="m")
    assert [x.id for x in collapse_promoted_pairs([m, s, lo])] == ["l"]


def test_source_without_its_copy_on_the_page_is_kept():
    s, o = _mem("s"), _mem("o")
    assert collapse_promoted_pairs([s, o]) == [s, o]
    orphan_copy = _mem("m", MemoryLayer.MID, parent="gone")
    assert [x.id for x in collapse_promoted_pairs([orphan_copy, o])] == ["m", "o"]


def test_a_plain_parent_link_is_not_a_promotion():
    """Chunks and other children carry parent_id too; only promoted_from pairs."""
    parent = _mem("p")
    chunk = Memory(id="c1", title="c1", content="c", parent_id="p", metadata={})
    assert collapse_promoted_pairs([parent, chunk]) == [parent, chunk]


def test_store_search_returns_one_hit_for_a_promoted_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    original = store.snapshot("Microtubule digest", "microtubules tubulin quantum claims check")
    copy = store.promote(original.id, MemoryLayer.MID, summary="microtubules digest")
    assert copy is not None and store.primary.load(original.id) is not None

    hits = store.search("microtubules tubulin", limit=5)

    assert [h.id for h in hits] == [copy.id]
