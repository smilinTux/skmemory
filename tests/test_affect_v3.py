"""SKMEM-P1-17: affect-v3 felt_answerable class and the RERANK_GAMMA sweep (bench only)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skmemory import decay
from skmemory.eval import fixtures as fx
from skmemory.eval import fusion_bench as fb

FIXDIR = Path(fx.FIXTURE_DIR)
GEN = FIXDIR / "gen_affect_v3.py"
SWEEP = (0.15, 0.3, 0.5, 1.0)


@pytest.fixture(scope="module")
def corpus():
    return fx.load_affect_v3_corpus()


@pytest.fixture(scope="module")
def queries():
    return fx.load_affect_v3_queries()


@pytest.fixture(scope="module")
def sweep():
    return fb.run_ablation(fixture="affect-v3", gammas=SWEEP, channels=["none"])


def test_generator_deterministic_and_matches_committed(tmp_path):
    subprocess.run(
        [sys.executable, "-I", str(GEN), str(tmp_path)], check=True, capture_output=True
    )
    for name in ("affect-v3-corpus.jsonl", "affect-v3-queries.jsonl"):
        assert (tmp_path / name).read_text() == (FIXDIR / name).read_text(), name


def test_v3_extends_v2_without_changing_it():
    for kind in ("corpus", "queries"):
        v2 = (FIXDIR / f"affect-v2-{kind}.jsonl").read_text()
        assert (FIXDIR / f"affect-v3-{kind}.jsonl").read_text().startswith(v2)


def test_blind_sheet_hides_gold_and_key_resolves(tmp_path, queries):
    sheet, key = tmp_path / "s.txt", tmp_path / "k.json"
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(GEN),
            str(tmp_path),
            "--blind-sheet",
            str(sheet),
            "--blind-key",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    text = sheet.read_text()
    answers = json.loads(key.read_text())
    fa = [q for q in queries if q.query_class == "felt_answerable"]
    assert set(answers) == {q.id for q in fa}
    assert "Gold" not in text and all(q.gold_ids[0] not in text for q in fa)


def test_felt_answerable_shape(corpus, queries):
    fa = [q for q in queries if q.query_class == "felt_answerable"]
    assert len(fa) == 20
    by = {m.id: m for m in corpus}
    notes = [m for m in corpus if " felt note " in m.title]
    assert len(notes) == 60 and len({m.id for m in corpus}) == len(corpus)
    for q in fa:
        gold = by[q.gold_ids[0]]
        topic = gold.title.split(" felt note ")[0]
        group = [m for m in notes if m.title.startswith(topic + " felt note ")]
        assert len(group) == 6
        # the felt sentence of the gold is unique in its group, and the query is a paraphrase
        felt = {m.id: m.content for m in group}
        assert len(set(felt.values())) == 6
        assert q.query.lower() not in gold.content.lower()
        assert topic.split()[0].lower() not in q.query.lower(), q.id


def test_intensity_is_not_a_tie_break_oracle(corpus, queries):
    by = {m.id: m for m in corpus}
    notes = [m for m in corpus if " felt note " in m.title]
    ranks = []
    for q in queries:
        if q.query_class != "felt_answerable":
            continue
        gold = by[q.gold_ids[0]]
        topic = gold.title.split(" felt note ")[0]
        group = [m for m in notes if m.title.startswith(topic + " felt note ")]
        ranks.append(
            sorted((m.emotional.intensity for m in group), reverse=True).index(
                gold.emotional.intensity
            )
        )
    assert set(ranks) == {0, 1, 2, 3, 4, 5}, "gold must rotate over the whole intensity ladder"
    assert ranks.count(0) <= 5, "gold must not usually be the most intense member"
    created = [m.created_at for m in notes]
    assert len(set(created)) > 30


def test_felt_answerable_has_headroom(sweep):
    base = next(c for c in sweep["cells"] if c["gamma"] == 0.15 and c["affect_weight"] == 0.0)
    fa = base["by_class"]["felt_answerable"]
    assert fa["queries"] == 20 and 0.0 < fa["mrr"] < 1.0


def test_v2_classes_carried_into_v3(sweep):
    assert {
        "felt_peak",
        "felt_quiet",
        "factual_guard",
        "valence_pair",
        "reinforce",
        "felt_answerable",
    } == set(sweep["cells"][0]["by_class"])


def test_sweep_grid_and_gamma_references(sweep):
    assert sweep["gammas"] == list(SWEEP)
    assert len(sweep["cells"]) == len(SWEEP) * len(fb.ABLATION_WEIGHTS)
    for c in sweep["cells"]:
        assert ("vs_gamma_default" in c) == (c["gamma"] != decay.RERANK_GAMMA)
        assert ("vs_w0" in c) == (c["affect_weight"] != 0.0)


def test_default_gamma_cell_equals_unswept_run(sweep):
    plain = fb.run_ablation(fixture="affect-v3", channels=["none"])
    assert all(c["gamma"] == decay.RERANK_GAMMA for c in plain["cells"])
    mine = {c["affect_weight"]: c["by_class"] for c in sweep["cells"] if c["gamma"] == 0.15}
    assert all(mine[c["affect_weight"]] == c["by_class"] for c in plain["cells"])


def test_gamma_one_lifts_reinforce_as_the_review_claimed(sweep):
    cell = {(c["gamma"], c["affect_weight"]): c for c in sweep["cells"]}
    # the P1-13 review: gamma 1.0 lifts reinforce MRR; the reinforcement channel is the one that reads it
    full = fb.run_ablation(fixture="affect-v3", gammas=(0.15, 1.0), channels=["reinforcement"])
    r = {
        c["gamma"]: c["by_class"]["reinforce"]["mrr"]
        for c in full["cells"]
        if c["affect_weight"] == 0.0
    }
    assert r[1.0] > r[0.15]
    assert cell[(0.15, 0.0)]["by_class"]["reinforce"]["mrr"] > 0


def test_gamma_verdict_rules():
    guard_ok = {"d_mrr": 0.0, "beats_noise": False}
    res = {
        "cells": [
            {
                "channel": "x",
                "gamma": 1.0,
                "affect_weight": 0.0,
                "vs_gamma_default": {
                    "reinforce": {"d_mrr": 0.4, "beats_noise": True},
                    "factual_guard": guard_ok,
                },
            },
            {
                "channel": "x",
                "gamma": 0.5,
                "affect_weight": 0.0,
                "vs_gamma_default": {
                    "reinforce": {"d_mrr": 0.4, "beats_noise": True},
                    "factual_guard": {"d_mrr": -0.2, "beats_noise": False},
                },
            },
            {
                "channel": "x",
                "gamma": 0.3,
                "affect_weight": 0.0,
                "vs_gamma_default": {
                    "reinforce": {"d_mrr": 0.0, "beats_noise": False},
                    "factual_guard": guard_ok,
                },
            },
            {"channel": "x", "gamma": 0.15, "affect_weight": 0.0},
        ]
    }
    got = {r["gamma"]: r["wins"] for r in fb.gamma_verdict(res)}
    assert got == {1.0: True, 0.5: False, 0.3: False}


def test_unknown_fixture_still_rejected():
    with pytest.raises(ValueError):
        fb.run_ablation(fixture="affect-v9")
