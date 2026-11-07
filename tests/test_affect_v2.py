"""SKMEM-P1-13: affect-v2 fixture integrity and the emotional-weight benchmark."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from skmemory import decay
from skmemory.eval import fixtures as fx
from skmemory.eval import fusion_bench as fb

GEN = Path(fx.FIXTURE_DIR) / "gen_affect_v2.py"
IMPROVEMENT = ("felt_peak", "reinforce", "valence_pair")
GUARDS = ("felt_quiet", "factual_guard")


@pytest.fixture(scope="module")
def corpus():
    return fx.load_affect_corpus()


@pytest.fixture(scope="module")
def queries():
    return fx.load_affect_queries()


@pytest.fixture(scope="module")
def grid():
    return fb.run_ablation(fixture="affect-v2")


def test_generator_is_deterministic_and_matches_committed(tmp_path):
    subprocess.run(
        [sys.executable, "-I", str(GEN), str(tmp_path)], check=True, capture_output=True
    )
    for name in ("affect-v2-corpus.jsonl", "affect-v2-queries.jsonl"):
        assert (tmp_path / name).read_text() == (Path(fx.FIXTURE_DIR) / name).read_text(), name


def test_ids_unique_and_references_resolve(corpus, queries):
    ids = [m.id for m in corpus]
    assert len(ids) == len(set(ids))
    for q in queries:
        assert q.gold_ids and set(q.gold_ids) <= set(ids)


def test_intensity_actually_spans_the_range(corpus):
    vals = sorted({m.emotional.intensity for m in corpus})
    assert vals[0] <= 0.1 and vals[-1] >= 0.95
    assert len(vals) >= 8, "v1 was 0.3 almost everywhere; v2 must not repeat that"
    assert {m.emotional.valence > 0 for m in corpus} == {True, False}


def test_felt_sense_queries_never_name_the_topic(corpus, queries):
    topics = {m.title.split(" entry ")[0].lower() for m in corpus if " entry " in m.title}
    for q in queries:
        if q.query_class != "felt_peak":
            continue
        for t in topics:
            assert t not in q.query.lower(), (q.id, t)


def test_reinforce_gold_has_real_access_state(corpus, queries):
    by = {m.id: m for m in corpus}
    for q in queries:
        if q.query_class != "reinforce":
            continue
        g = by[q.gold_ids[0]]
        assert g.access_count >= 10 and g.last_accessed
        assert g.created_at < min(m.created_at for m in corpus if m.title.endswith(" v1"))


def test_reinforcement_channel_no_longer_duplicates_recency(corpus, queries):
    as_of = datetime.fromisoformat(fb.ABLATION_AS_OF)
    by = {m.id: m for m in corpus}
    for q in queries:
        if q.query_class != "reinforce":
            continue
        g = by[q.gold_ids[0]]
        decoy = next(m for m in corpus if m.title == g.title.replace(" gold", " v1"))

        def score(ch, m):
            return decay.decay_score(
                ch,
                as_of=as_of,
                created_at=m.created_at,
                last_accessed=m.last_accessed,
                access_count=m.access_count,
                intensity=m.emotional.intensity,
                kind="fact",
            )

        assert score("recency", g) < score("recency", decoy)
        assert score("reinforcement", g) > score("reinforcement", decoy)


def test_no_private_shapes(corpus):
    blob = " ".join(m.title + " " + m.content for m in corpus)
    assert not re.search(r"postgres(ql)?://", blob, re.I)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", blob, re.I)
    assert not re.search(r"(?<![\d.])(?:100|192\.168)\.\d+\.\d+(?:\.\d+)?", blob)


def test_grid_shape(grid):
    assert grid["fixture"] == "affect-v2"
    assert len(grid["cells"]) == 16
    classes = set(grid["cells"][0]["by_class"])
    assert classes == {"felt_peak", "felt_quiet", "factual_guard", "valence_pair", "reinforce"}


def test_improvement_classes_have_headroom_at_baseline(grid):
    base = next(c for c in grid["cells"] if c["channel"] == "none" and c["affect_weight"] == 0.0)
    for cls in IMPROVEMENT:
        assert base["by_class"][cls]["mrr"] < 1.0, f"{cls} saturated at baseline"
    assert base["by_class"]["felt_peak"]["recall@5"] < 1.0
    assert base["by_class"]["felt_quiet"]["recall@1"] < 1.0


def test_guard_class_baselines_are_gated_not_improvement(queries):
    for q in queries:
        if q.query_class == "factual_guard":
            assert q.gate_recall_at_5 == 1.0


def test_valence_is_a_declared_limit(grid):
    mrrs = {c["by_class"]["valence_pair"]["mrr"] for c in grid["cells"]}
    assert len(mrrs) == 1, "no ranking signal reads valence, so no cell may move it"


def test_vs_w0_present_for_nonzero_weights_only(grid):
    for c in grid["cells"]:
        assert ("vs_w0" in c) == (c["affect_weight"] != 0.0)


def test_paired_delta_noise_bar():
    assert fb.paired_delta([0.0] * 10)["beats_noise"] is False
    assert fb.paired_delta([0.01] * 10)["beats_noise"] is False  # real but below the floor
    assert fb.paired_delta([0.2] * 10)["beats_noise"] is True
    assert fb.paired_delta([0.2, -0.2, 0.2, -0.2])["beats_noise"] is False
    assert fb.paired_delta([])["n"] == 0


def test_deterministic(grid):
    import json

    again = fb.run_ablation(fixture="affect-v2")
    assert json.dumps(again, sort_keys=True) == json.dumps(grid, sort_keys=True)


def test_unknown_fixture_rejected():
    with pytest.raises(ValueError):
        fb.run_ablation(fixture="nope")


def test_v1_loader_still_reads_without_access_fields():
    assert all(m.access_count == 0 and m.last_accessed is None for m in fx.load_corpus())
