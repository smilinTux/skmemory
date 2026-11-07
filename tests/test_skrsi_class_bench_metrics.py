"""The SKRSI adoption flattener turns real class_bench output into honest metrics (SKRSI-02)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "class_bench_metrics", ROOT / "skrsi" / "class_bench_metrics.py"
)
cbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbm)


def raw(leaks=0, recalls=(1.0, 0.5, None)):
    return {
        "leak_count": leaks,
        "by_class": {
            f"c{i}": {"queries": 4, "strategies": {"candidate": {"recall@5": r}}}
            for i, r in enumerate(recalls)
        },
    }


def test_flatten_takes_worst_class_and_skips_null_classes():
    out = cbm.flatten(raw())
    assert out["metrics"]["class_recall_at_5_per_class"]["value"] == 0.5
    assert out["cohort"]["sample_size"] == 12
    assert out["instrument"]["runs_production_code"] is True


def test_no_measured_class_is_missing_not_zero():
    assert (
        cbm.flatten(raw(recalls=(None,)))["metrics"]["class_recall_at_5_per_class"]["value"]
        is None
    )


def test_leak_is_bounded_and_asserted_false():
    out = cbm.flatten(raw(leaks=2))
    assert out["metrics"]["leak_count"]["value"] == 2 and out["metrics"]["leak_count"]["max"] == 0
    assert out["invariants"] == {"leak_count stays 0 for a declared non-owner reader": False}


def test_metric_names_and_invariants_exist_in_the_target():
    target = json.loads((ROOT / "skrsi" / "targets" / "skmemory-retrieval.json").read_text())
    out = cbm.flatten(raw())
    assert set(out["metrics"]) <= set(target["metrics"])
    assert set(out["invariants"]) <= set(target["invariants"])
    assert out["metrics"]["leak_count"]["stop_rule"] in {r["id"] for r in target["stop_rules"]}


def test_cli_roundtrip(tmp_path):
    src, dst = tmp_path / "raw.json", tmp_path / "m.json"
    src.write_text(json.dumps(raw()))
    subprocess.run(
        [sys.executable, str(ROOT / "skrsi" / "class_bench_metrics.py"), str(src), str(dst)],
        check=True,
    )
    assert json.loads(dst.read_text())["metrics"]["leak_count"]["value"] == 0
