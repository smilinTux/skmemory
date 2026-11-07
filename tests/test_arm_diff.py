"""SKMEM-P1-10 arm_diff: hermetic checks of the diagnostic (no live store)."""

from __future__ import annotations

import json

import pytest

from skmemory.eval import arm_diff
from skmemory.eval import live_bench as lb
from skmemory.store import MemoryStore


class FakeVector:
    def __init__(self, mems):
        self.mems = list(mems)

    def search_semantic(self, query, limit=10, **filters):
        return self.mems[:limit]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    monkeypatch.setenv("SKMEMORY_FUSION_ROUTE", "vector")
    monkeypatch.setenv("SKMEMORY_FUSION_VECTOR_PIN", "5")
    monkeypatch.setenv("SKMEMORY_FUSION_STRATEGY", "pinned_rrf")
    for k in ("SKMEMORY_SEARCH_RERANK", "SKMEMORY_TIER_CAP", "SKMEMORY_DECAY"):
        monkeypatch.delenv(k, raising=False)
    s = MemoryStore()
    vec = [
        s.snapshot(title=f"SECRETTITLE {n}", content=f"SECRETBODY {n} replication", tags=["t"])
        for n in range(1, 15)
    ]
    lex = s.snapshot(
        title="lex", content="sync keep falling behind after the reconfiguration", tags=["t"]
    )
    s.vector = FakeVector(vec)
    s._test_vec, s._test_lex = vec, lex
    return s


def _run(store, q):
    vec_ids = [m.id for m in store._test_vec]
    return arm_diff.trace_query(store, lambda _q: vec_ids[:10], q)


def test_trace_names_the_reranker_as_the_stage_that_removed_a_hit(store):
    row = _run(store, "why does the sync keep falling behind after the reconfiguration")
    assert row["route"] == "natural"
    assert row["rerank"] == "applied"
    assert row["arm_raw_top5"] == row["vector_only"]
    assert row["fused_top5"] == row["vector_only"]  # the pin held inside fuse()
    assert not row["page_top5_equals_vector_top5"]
    assert row["replica_matches_page"] is True
    removed = {h["removed_by"] for h in row["hits"].values()} - {None}
    assert removed == {"reranked"}


def test_trace_clean_when_nothing_downstream_acts(store):
    row = _run(store, "why does the sync keep falling behind")
    assert row["page_top5_equals_vector_top5"]
    assert all(h["removed_by"] is None for h in row["hits"].values())


def test_output_is_ids_only(store):
    row = _run(store, "why does the sync keep falling behind after the reconfiguration")
    blob = json.dumps(row)
    assert "SECRET" not in blob and "reconfiguration" not in blob


def test_blame_arm_raw_miss():
    out = arm_diff.blame(["a", "b", "c", "d", "e"], {"arm_raw": ["a", "b", "c", "d", "x", "e"]})
    assert out["e"]["removed_by"] == "arm_raw"


def test_summarise_counts():
    rows = [
        {
            "hits": {"a": {"removed_by": "reranked", "moved_by": None}},
            "route": "natural",
            "rerank": "applied",
            "replica_matches_page": True,
        }
    ]
    s = arm_diff.summarise(rows)
    assert s["vector_hits_removed_by"] == {"reranked": 1}


def test_requires_live_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("SKMEMORY_EVAL_LIVE", raising=False)
    with pytest.raises(lb.LiveNotEnabled):
        arm_diff.run(out_dir=str(tmp_path / "o"), paraphrase=str(tmp_path))


def test_refuses_output_inside_repo(monkeypatch):
    monkeypatch.setenv("SKMEMORY_EVAL_LIVE", "1")
    with pytest.raises(lb.ReadOnlyViolation):
        arm_diff.run(
            out_dir=str(lb.Path(lb.__file__).resolve().parents[2] / "x"), paraphrase="/nope"
        )
