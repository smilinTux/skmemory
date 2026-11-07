"""SKMEM-P1-05: affect-weight x decay-channel ablation in fusion_bench."""

import json

import pytest

from skmemory import decay
from skmemory.eval import class_bench
from skmemory.eval import fusion_bench as fb


@pytest.fixture(scope="module")
def grid():
    return fb.run_ablation()


def test_default_grid_is_full_cross_product(grid):
    assert grid["affect_weights"] == [0.0, 0.25, 0.5]
    assert grid["channels"] == list(decay.CHANNELS)
    assert len(grid["cells"]) == 12
    for cell in grid["cells"]:
        assert "affect" in cell["by_class"]
        assert set(cell["overall"]) >= {"recall@5", "mrr"}


def test_clock_matches_class_bench():
    assert fb.ABLATION_AS_OF == class_bench.FIXTURE_NOW


def test_baseline_cell_has_no_reorder(grid):
    base = next(c for c in grid["cells"] if c["affect_weight"] == 0.0 and c["channel"] == "none")
    assert base["reordered"] == 0


def test_affect_weight_can_move_ranking(grid):
    moved = [c for c in grid["cells"] if c["affect_weight"] > 0 or c["channel"] != "none"]
    assert any(c["reordered"] > 0 for c in moved)


def test_deterministic():
    assert json.dumps(fb.run_ablation(), sort_keys=True) == json.dumps(
        fb.run_ablation(), sort_keys=True
    )


def test_subset_flags():
    g = fb.run_ablation(affect_weights=[0.5], channels=["recency"])
    assert [(c["affect_weight"], c["channel"]) for c in g["cells"]] == [(0.5, "recency")]


def test_rejects_unknown_channel():
    with pytest.raises(ValueError):
        fb.run_ablation(channels=["bogus"])


def test_cli_ablation_json(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["fusion_bench", "--affect-weight", "0,0.5", "--json"])
    fb.main()
    out = json.loads(capsys.readouterr().out)
    assert [(c["affect_weight"], c["channel"]) for c in out["cells"]] == [
        (0.0, "none"),
        (0.5, "none"),
    ]


def test_default_main_unchanged(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["fusion_bench", "--json"])
    fb.main()
    assert "strategies" in json.loads(capsys.readouterr().out)
