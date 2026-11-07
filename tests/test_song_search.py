"""SKMEM-P1-14: ranked song-anchor search and the synthetic benchmark.

Hermetic: the fixture is written under tmp_path, no agent store is read, and
the dense arm is only ever a fake callable.
"""

import pytest

from skmemory import song_search
from skmemory.eval import song_bench
from skmemory.songs import match_anchors_for_feb, search_anchors


@pytest.fixture(scope="module")
def anchors(tmp_path_factory):
    return song_bench.write_fixture(tmp_path_factory.mktemp("songs"))


@pytest.fixture(scope="module")
def res(anchors):
    return song_bench.run(anchors)


def test_substring_search_is_unchanged_and_misses_felt_queries(anchors):
    assert search_anchors("sounds like coming home", anchors=anchors) == []
    assert [a.title for a in search_anchors("Lantern Road", anchors=anchors)] == ["Lantern Road"]


def test_ranked_finds_felt_query_the_substring_misses(anchors):
    top = song_search.rank_anchors("the song that sounds like coming home", anchors, limit=3)
    assert "Lantern Road" in [a.title for a, _ in top]


def test_empty_query_returns_head_like_substring(anchors):
    assert len(song_search.rank_anchors("  ", anchors, limit=4)) == 4


def test_unrelated_query_returns_nothing(anchors):
    assert song_search.rank_anchors("quarterly tax filing", anchors) == []


def test_bench_floors_and_delta(res):
    for cls in ("mood", "moment"):
        assert res[cls]["substring"]["recall_at_3"] == 0.0
        assert res[cls]["ranked"]["recall_at_3"] >= 0.8
        assert res[cls]["ranked"]["mrr"] >= 0.7
    assert res["feb"]["synonyms"]["recall_at_3"] > res["feb"]["baseline"]["recall_at_3"]
    assert res["feb"]["synonyms"]["recall_at_3"] >= 0.8


def test_ranked_does_not_regress_literal_queries(res):
    assert res["literal"]["ranked"]["recall_at_3"] >= res["literal"]["substring"]["recall_at_3"]
    assert res["literal"]["ranked"]["mrr"] >= res["literal"]["substring"]["mrr"]


def test_feb_synonyms_off_by_default(anchors):
    feb = {"emotional_payload": {"emotional_topology": {"homecoming": 0.9, "warmth": 0.6}}}
    assert match_anchors_for_feb(feb, anchors=anchors) == []
    got = match_anchors_for_feb(feb, anchors=anchors, synonyms=True)
    assert got and got[0][0].title == "Lantern Road"


def test_dense_arm_is_fused_when_given(anchors):
    # a fake embedder that puts everything on one axis except the target text
    def embed(text: str) -> list[float]:
        return [1.0, 0.0] if "Midnight Ferry" in text or "pier" in text else [0.0, 1.0]

    q = "sitting on the pier before anyone else was up"
    lex = [a.title for a, _ in song_search.rank_anchors(q, anchors)]
    assert "Midnight Ferry" not in lex[:1]
    fused = [a.title for a, _ in song_search.rank_anchors(q, anchors, embed_fn=embed)]
    assert "Midnight Ferry" in fused[:3]


def test_embed_failure_falls_back_to_lexical(anchors):
    def boom(text: str) -> list[float]:
        raise ConnectionError("no endpoint")

    q = "the song that sounds like coming home"
    assert song_search.rank_anchors(q, anchors, embed_fn=boom) == song_search.rank_anchors(
        q, anchors
    )


def test_endpoint_embedder_is_none_without_env(monkeypatch):
    monkeypatch.delenv("SKMEMORY_EMBED_URL", raising=False)
    monkeypatch.delenv("SKMEMORY_EMBED_URLS", raising=False)
    assert song_search.endpoint_embedder() is None


def test_fixture_is_synthetic_and_deterministic(tmp_path):
    a = song_bench.write_fixture(tmp_path / "a")
    b = song_bench.write_fixture(tmp_path / "b")
    assert [x.title for x in a] == [x.title for x in b]
    assert all(str(x.path).startswith(str(tmp_path)) for x in a)
