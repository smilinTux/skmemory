"""Unit tests for the multi-agent ("all agents") reconcile mode.

Card f9543206: the production reconcile cron ran for agent ``lumina`` only, so
other agents (opus, jarvis, swarm specialists) writing via MCP could drift in
skmem-pg with no self-heal. ``reconcile.reconcile_all()`` extends the engine to
enumerate every provisioned agent (or an explicit list) and reconcile each, with
a per-agent result summary and failure isolation.

These tests are fully mocked: ``reconcile.reconcile`` (the per-agent engine that
talks to the live skmem-pg via ``docker exec``) is monkeypatched, so nothing
here touches the real store, docker, or the embed endpoint. Discovery is
exercised against a throwaway agent tree under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from skmemory import reconcile as reconcile_mod


def _hash(document, field):
    payload = {key: value for key, value in document.items() if key != field}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _make_agent_tree(base, names, *, with_memory=True, templates=()):
    """Create a fake registered agent tree under `base`."""
    entries = []
    for name in names:
        d = base / name
        if with_memory:
            (d / "memory" / "short-term").mkdir(parents=True, exist_ok=True)
        else:
            d.mkdir(parents=True, exist_ok=True)
        profile = {
            "schema_version": "skcapstone.agent-profile.v1",
            "schema_revision": "1",
            "profile_id": name,
            "profile_kind": "human",
            "selectable": True,
            "fallback_eligible": True,
            "memory_principal_id": f"memory:{name}",
            "default_tools": [],
            "capability_policy_ref": "synthetic-test-policy.v1",
            "profile_revision": "1",
            "profile_hash": "",
        }
        profile["profile_hash"] = _hash(profile, "profile_hash")
        (d / "profile.json").write_text(json.dumps(profile))
        entries.append(
            {
                key: profile[key]
                for key in (
                    "profile_id",
                    "profile_kind",
                    "selectable",
                    "fallback_eligible",
                    "memory_principal_id",
                    "schema_revision",
                    "profile_revision",
                    "profile_hash",
                )
            }
        )
    registry = {
        "schema_version": "skcapstone.profile-registry.v1",
        "schema_revision": "1",
        "registry_revision": "synthetic-test-1",
        "profiles": entries,
        "registry_hash": "",
    }
    registry["registry_hash"] = _hash(registry, "registry_hash")
    (base.parent / "config").mkdir(exist_ok=True)
    (base.parent / "config/profile-registry.json").write_text(json.dumps(registry))
    for name in templates:
        (base / name / "memory").mkdir(parents=True, exist_ok=True)
    return base


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_discover_agents_finds_all_with_memory_dir(tmp_path):
    base = _make_agent_tree(tmp_path, ["lumina", "opus", "jarvis"])
    assert reconcile_mod.discover_agents(str(base)) == ["jarvis", "lumina", "opus"]


def test_discover_agents_excludes_templates(tmp_path):
    base = _make_agent_tree(tmp_path, ["lumina", "opus"], templates=["lumina-template"])
    found = reconcile_mod.discover_agents(str(base))
    assert "lumina-template" not in found
    assert found == ["lumina", "opus"]


def test_discover_agents_skips_dirs_without_memory(tmp_path):
    base = _make_agent_tree(tmp_path, ["lumina", "not-an-agent"])
    (base / "not-an-agent" / "memory").rename(base / "not-an-agent-memory")
    assert reconcile_mod.discover_agents(str(base)) == ["lumina"]


def test_discover_agents_missing_base_is_empty(tmp_path):
    assert reconcile_mod.discover_agents(str(tmp_path / "nope")) == []


def test_discover_agents_honours_skmemory_home(tmp_path, monkeypatch):
    base = _make_agent_tree(tmp_path, ["lumina", "opus"])
    monkeypatch.setenv("SKMEMORY_HOME", str(base))
    monkeypatch.delenv("SKCAPSTONE_HOME", raising=False)
    assert reconcile_mod.discover_agents() == ["lumina", "opus"]


# --------------------------------------------------------------------------- #
# reconcile_all: per-agent invocation + aggregation
# --------------------------------------------------------------------------- #
def test_reconcile_all_calls_reconcile_per_discovered_agent(tmp_path, monkeypatch):
    base = _make_agent_tree(tmp_path, ["lumina", "opus", "jarvis"])
    calls = []

    def fake_reconcile(agent=None, **kwargs):
        calls.append(agent)
        return {"agent": agent, "flat": 1, "backfilled": 0, "pruned": 0, "total": 1}

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)

    summary = reconcile_mod.reconcile_all(agents_base=str(base), verbose=False)

    assert sorted(calls) == ["jarvis", "lumina", "opus"]
    assert summary["ok"] is True
    assert summary["succeeded"] == 3
    assert summary["failed"] == 0
    assert [a["agent"] for a in summary["agents"]] == ["jarvis", "lumina", "opus"]
    assert all(a["ok"] for a in summary["agents"])


def test_reconcile_all_explicit_list_overrides_discovery(monkeypatch):
    calls = []

    def fake_reconcile(agent=None, **kwargs):
        calls.append(agent)
        return {"agent": agent}

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    # discovery must NOT be consulted when an explicit list is passed
    monkeypatch.setattr(
        reconcile_mod,
        "discover_agents",
        lambda *a, **k: pytest.fail("discover_agents must not be called"),
    )

    summary = reconcile_mod.reconcile_all(["opus", "jarvis"], verbose=False)
    assert calls == ["opus", "jarvis"]
    assert summary["ok"] is True


def test_reconcile_all_forwards_kwargs(monkeypatch):
    seen = {}

    def fake_reconcile(agent=None, **kwargs):
        seen[agent] = kwargs
        return {"agent": agent}

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)
    reconcile_mod.reconcile_all(
        ["lumina"], verbose=False, embed_url="http://x/embed", psql_cmd=["true"]
    )
    assert seen["lumina"]["embed_url"] == "http://x/embed"
    assert seen["lumina"]["psql_cmd"] == ["true"]
    assert seen["lumina"]["verbose"] is False


# --------------------------------------------------------------------------- #
# failure isolation
# --------------------------------------------------------------------------- #
def test_reconcile_all_isolates_one_agent_failure(monkeypatch):
    def fake_reconcile(agent=None, **kwargs):
        if agent == "jarvis":
            raise RuntimeError("embed failed")
        return {"agent": agent, "backfilled": 0}

    monkeypatch.setattr(reconcile_mod, "reconcile", fake_reconcile)

    summary = reconcile_mod.reconcile_all(["lumina", "jarvis", "opus"], verbose=False)

    # the other two still ran
    ran = {a["agent"]: a for a in summary["agents"]}
    assert ran["lumina"]["ok"] is True
    assert ran["opus"]["ok"] is True
    # the failing one is captured, not raised
    assert ran["jarvis"]["ok"] is False
    assert "embed failed" in ran["jarvis"]["error"]
    # reflected in the rollup / exit status
    assert summary["ok"] is False
    assert summary["failed"] == 1
    assert summary["succeeded"] == 2


def test_reconcile_all_empty_when_no_agents(tmp_path):
    summary = reconcile_mod.reconcile_all(agents_base=str(tmp_path / "empty"), verbose=False)
    assert summary == {"ok": True, "succeeded": 0, "failed": 0, "agents": []}


# --------------------------------------------------------------------------- #
# CLI main(): single-agent path intact + all-agents path + exit status
# --------------------------------------------------------------------------- #
def test_main_single_agent_still_works(monkeypatch):
    calls = []
    monkeypatch.setattr(reconcile_mod, "reconcile", lambda agent=None, **k: calls.append(agent))
    # explicit positional agent
    reconcile_mod.main(["opus"])
    assert calls == ["opus"]


def test_main_defaults_to_default_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(reconcile_mod, "reconcile", lambda agent=None, **k: calls.append(agent))
    monkeypatch.setattr(reconcile_mod, "default_agent", lambda: "lumina")
    reconcile_mod.main([])
    assert calls == ["lumina"]


def test_main_all_flag_runs_reconcile_all(monkeypatch):
    called = {}

    def fake_all(agents=None, **kwargs):
        called["agents"] = agents
        return {"ok": True, "succeeded": 2, "failed": 0, "agents": []}

    monkeypatch.setattr(reconcile_mod, "reconcile_all", fake_all)
    monkeypatch.setattr(
        reconcile_mod, "reconcile", lambda *a, **k: pytest.fail("single path used")
    )
    reconcile_mod.main(["--all"])
    assert called["agents"] is None  # discovery


def test_main_agents_flag_parses_explicit_list(monkeypatch):
    called = {}

    def fake_all(agents=None, **kwargs):
        called["agents"] = agents
        return {"ok": True, "succeeded": 0, "failed": 0, "agents": []}

    monkeypatch.setattr(reconcile_mod, "reconcile_all", fake_all)
    reconcile_mod.main(["--agents", "opus, jarvis ,lumina"])
    assert called["agents"] == ["opus", "jarvis", "lumina"]


def test_main_exits_nonzero_when_any_agent_fails(monkeypatch):
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_all",
        lambda *a, **k: {"ok": False, "succeeded": 1, "failed": 1, "agents": []},
    )
    with pytest.raises(SystemExit) as ei:
        reconcile_mod.main(["--all"])
    assert ei.value.code == 1
