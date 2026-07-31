"""Gate for the skmemory operator facet: explain / observe / act.

Hermetic: the health probe and the act runner are injected, so no test touches a
live skmemory, a real embedding backend, skmem-pg, or systemd. The contract shape
is asserted against Atlas's skmemory adapter
(`skcapstone/src/skcapstone/operator_seat/skmemory_adapter.py`), the shared
source of truth.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skmemory import operator_probe as op
from skmemory.cli import cli

# --- explain: shape matches the contract -------------------------------------


def test_explain_shape_matches_contract():
    c = op.explain()
    assert set(c.keys()) == {"kinds", "conditions", "actions"}
    assert c["kinds"] == ["embed", "reconcile"]
    assert c["conditions"] == ["EmbedServing", "ReconcileFresh"]
    names = [a["name"] for a in c["actions"]]
    assert names == ["restart_service", "reindex"]
    for a in c["actions"]:
        assert set(a.keys()) == {
            "name",
            "standard",
            "reversible",
            "blast_radius",
            "runbook",
            "kedb_refs",
        }


def test_explain_action_metadata_standard():
    actions = {a["name"]: a for a in op.explain()["actions"]}
    restart = actions["restart_service"]
    assert restart["standard"] is True
    assert restart["reversible"] is True
    assert restart["blast_radius"] == "low"
    reindex = actions["reindex"]
    assert reindex["standard"] is False
    assert reindex["reversible"] is True
    assert reindex["blast_radius"] == "medium"


def test_explain_matches_atlas_adapter_when_available():
    """When the skcapstone adapter is importable, explain is byte-shape identical.

    Skipped where skcapstone is not installed (the CLI must not depend on it)."""
    try:
        from skcapstone.operator_seat import skmemory_adapter as adapter
    except Exception:
        pytest.skip("skcapstone adapter not importable in this environment")
    ours = op.explain()
    theirs = adapter.skmemory_explain()
    assert ours["kinds"] == theirs["kinds"]
    assert ours["conditions"] == theirs["conditions"]
    assert ours["actions"] == theirs["actions"]


# --- observe: healthy + each condition firing via injected probe -------------


def test_observe_healthy_shape():
    out = op.observe(probe=lambda: {"embed_serving": True, "reconcile_fresh": True})
    assert list(out.keys()) == ["conditions"]
    conds = out["conditions"]
    assert [c["type"] for c in conds] == ["EmbedServing", "ReconcileFresh"]
    assert [c["object"] for c in conds] == ["embed-service", "reconciler"]
    assert all(c["status"] == "True" for c in conds)


def test_observe_matches_adapter_object_names():
    try:
        from skcapstone.operator_seat import skmemory_adapter as adapter
    except Exception:
        pytest.skip("skcapstone adapter not importable in this environment")

    def probe():
        return {"embed_serving": True, "reconcile_fresh": True}

    assert op.observe(probe=probe) == adapter.skmemory_observe(probe=probe)


def test_observe_embed_serving_fires():
    out = op.observe(probe=lambda: {"embed_serving": False, "reconcile_fresh": True})
    by = {c["type"]: c for c in out["conditions"]}
    assert by["EmbedServing"]["status"] == "False"
    assert by["ReconcileFresh"]["status"] == "True"


def test_observe_reconcile_fresh_fires():
    out = op.observe(probe=lambda: {"embed_serving": True, "reconcile_fresh": False})
    by = {c["type"]: c for c in out["conditions"]}
    assert by["EmbedServing"]["status"] == "True"
    assert by["ReconcileFresh"]["status"] == "False"


def test_default_probe_fails_safe_healthy(monkeypatch):
    """An unreachable embed backend and a missing index read as healthy."""
    monkeypatch.setenv("SKMEMORY_EMBED_HEALTH", "http://127.0.0.1:1/does-not-exist")
    monkeypatch.setenv("SKMEMORY_INDEX_DB", "/nonexistent/index.db")
    st = op._default_probe()
    assert st == {"embed_serving": True, "reconcile_fresh": True}


def test_reconcile_fresh_rule():
    assert op._reconcile_fresh(None) is True  # no index yet -> fresh
    assert op._reconcile_fresh(10.0) is True  # brand new
    assert op._reconcile_fresh(op._RECONCILE_MAX_AGE_S + 1) is False  # stale


# --- act: standard action via runner; non-standard refused; unknown refused ---


def test_act_restart_service_runs_via_runner():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    out = op.act("restart_service", runner=runner, agent="lumina")
    assert out["performed"] is True
    assert out["action"] == "restart_service"
    assert out["unit"] == "skmemory-sync@lumina.service"
    assert out["command"] == [
        "systemctl",
        "--user",
        "restart",
        "skmemory-sync@lumina.service",
    ]
    assert out["result"]["ok"] is True
    assert calls == [["systemctl", "--user", "restart", "skmemory-sync@lumina.service"]]


def test_act_restart_service_unit_override():
    out = op.act(
        "restart_service",
        runner=lambda cmd: {"ok": True},
        unit="skmemory.service",
    )
    assert out["unit"] == "skmemory.service"
    assert out["command"][-1] == "skmemory.service"


def test_act_reindex_refuses_and_escalates():
    calls = []
    out = op.act("reindex", runner=lambda cmd: calls.append(cmd))
    assert out["performed"] is False
    assert out["escalate"] == "MAJOR"
    assert "human-approval-only" in out["reason"]
    assert calls == []  # never actuates


def test_act_unknown_action_refused():
    with pytest.raises(ValueError):
        op.act("nuke-everything", runner=lambda cmd: None)


# --- CLI wiring (store-free, hermetic) ---------------------------------------


def test_cli_explain_is_wired():
    runner = CliRunner()
    result = runner.invoke(cli, ["operator", "explain"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conditions"] == ["EmbedServing", "ReconcileFresh"]
    assert [a["name"] for a in payload["actions"]] == ["restart_service", "reindex"]


def test_cli_observe_is_wired(monkeypatch):
    # Fail-safe: with nothing reachable, observe reports healthy and exits 0.
    monkeypatch.setenv("SKMEMORY_EMBED_HEALTH", "http://127.0.0.1:1/nope")
    monkeypatch.setenv("SKMEMORY_INDEX_DB", "/nonexistent/index.db")
    runner = CliRunner()
    result = runner.invoke(cli, ["operator", "observe"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [c["type"] for c in payload["conditions"]] == [
        "EmbedServing",
        "ReconcileFresh",
    ]
    assert all(c["status"] == "True" for c in payload["conditions"])


def test_cli_act_reindex_refuses():
    runner = CliRunner()
    result = runner.invoke(cli, ["operator", "act", "reindex"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["performed"] is False
    assert payload["escalate"] == "MAJOR"


def test_cli_act_unknown_refused():
    runner = CliRunner()
    result = runner.invoke(cli, ["operator", "act", "bogus-action"])
    assert result.exit_code != 0
