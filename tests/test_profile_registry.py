"""Exact SKPM-PROF-01 memory ownership and fallback tests."""

from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skmemory import agents
from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.profile_registry import (
    FIXTURE_HASH,
    FIXTURE_PATH,
    conformance_pack_hash,
    fixture_contract_is_valid,
    profile_content_hash,
    registry_content_hash,
    resolve_memory_profile,
)
from skmemory.register import register_mcp
from skmemory.register_mcp import _memory_env

EXPECTED_FILE_HASH = "0d0c18d21b24e05f9cb18289c9a9e0d50b1e21c21a1046d4b0e5fc84bf29d1a6"


def _pack() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _case(name: str) -> dict:
    return copy.deepcopy(next(case for case in _pack()["cases"] if case["name"] == name))


def _write_case(root: Path, case: dict) -> None:
    registry = case["registry"]
    if registry is not None:
        path = root / "config/profile-registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(registry if isinstance(registry, str) else json.dumps(registry))
    profile = case["profile"]
    if profile is not None:
        path = root / "agents" / case["profile_id"] / "profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile if isinstance(profile, str) else json.dumps(profile))


def _configure_profile(root: Path, case: dict) -> None:
    _write_case(root, case)
    config = root / "agents" / case["profile_id"] / "config/skmemory.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"name: {case['profile_id']}\n")


def _install_cases(root: Path, *cases: dict) -> None:
    registry = copy.deepcopy(cases[0]["registry"])
    registry["profiles"] = []
    for case in cases:
        entry = next(
            item
            for item in case["registry"]["profiles"]
            if item["profile_id"] == case["profile_id"]
        )
        registry["profiles"].append(copy.deepcopy(entry))
    registry["registry_hash"] = registry_content_hash(registry)
    for case in cases:
        case["registry"] = registry
        _configure_profile(root, case)


@pytest.fixture
def agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(agents, "AGENTS_BASE_DIR", tmp_path / "agents")
    return tmp_path


def test_exact_fixture_schema_hash_and_vectors(tmp_path: Path) -> None:
    pack = _pack()
    assert fixture_contract_is_valid()
    assert pack["schema_version"] == "skcapstone.profile-conformance-fixtures.v1"
    assert pack["pack_revision"] == "1"
    assert pack["synthetic"] is True
    assert len(pack["cases"]) == 15
    assert pack["pack_hash"] == FIXTURE_HASH
    assert conformance_pack_hash(pack) == FIXTURE_HASH
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == EXPECTED_FILE_HASH

    for index, case in enumerate(pack["cases"]):
        root = tmp_path / str(index)
        _write_case(root, case)
        profile = resolve_memory_profile(root, case["profile_id"])
        assert {
            "state": profile.state,
            "healthy": profile.healthy,
            "selectable": profile.selectable,
            "fallback_eligible": profile.fallback_eligible,
            "memory_principal_id": profile.memory_principal_id,
        } == {
            key: case["expected"][key]
            for key in (
                "state",
                "healthy",
                "selectable",
                "fallback_eligible",
                "memory_principal_id",
            )
        }, case["name"]
        assert profile.state != "Unknown"


def test_service_owns_only_explicit_memory_and_is_not_selectable(
    agent_root: Path,
) -> None:
    service = _case("bounded_service")
    _configure_profile(agent_root, service)

    profile = resolve_memory_profile(agent_root, service["profile_id"])
    assert profile.memory_principal_id == "memory:synthetic-service"
    assert profile.profile_kind == "service"
    assert not profile.selectable
    assert not profile.fallback_eligible
    assert agents.get_active_agent({"SKAGENT": service["profile_id"]}) is None
    assert agents.get_active_agent({"SKCAPSTONE_AGENT": service["profile_id"]}) is None
    assert (
        agents.get_active_agent({"SKMEMORY_AGENT": service["profile_id"]}) == service["profile_id"]
    )
    assert agents.get_agent_paths(service["profile_id"])["base"].name == service["profile_id"]


def test_sole_service_has_no_default_fallback(agent_root: Path) -> None:
    service = _case("bounded_service")
    _configure_profile(agent_root, service)

    assert agents.list_agents() == []
    assert agents.get_active_agent({}) is None


def test_mixed_profiles_fallback_only_to_human(agent_root: Path) -> None:
    human = _case("healthy_human")
    service = _case("bounded_service")
    _install_cases(agent_root, human, service)

    assert agents.list_agents() == [human["profile_id"]]
    assert agents.get_active_agent({}) == human["profile_id"]
    assert agents.get_active_agent({"SKAGENT": service["profile_id"]}) is None


@pytest.mark.parametrize(
    "case_name",
    [
        "missing_registry",
        "corrupt_registry",
        "unknown_registry_version",
        "registry_hash_mismatch",
        "missing_profile",
        "corrupt_profile",
        "unknown_profile_version",
        "stale_profile",
        "profile_hash_mismatch",
        "profile_id_mismatch",
        "service_selectable_conflict",
        "service_tool_conflict",
    ],
)
def test_invalid_metadata_has_no_memory_or_fallback(agent_root: Path, case_name: str) -> None:
    case = _case(case_name)
    _configure_profile(agent_root, case)

    profile = resolve_memory_profile(agent_root, case["profile_id"])
    assert not profile.healthy
    assert profile.memory_principal_id is None
    assert agents.get_active_agent({"SKMEMORY_AGENT": case["profile_id"]}) is None
    assert agents.get_active_agent({}) is None


def test_stale_bundled_fixture_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "profile-conformance-v1.json"
    document = _pack()
    document["pack_revision"] = "2"
    stale.write_text(json.dumps(document))
    assert not fixture_contract_is_valid(stale)

    human = _case("healthy_human")
    _write_case(tmp_path, human)
    monkeypatch.setattr("skmemory.profile_registry.FIXTURE_PATH", stale)
    profile = resolve_memory_profile(tmp_path, human["profile_id"])
    assert profile.state == "fixture_contract_unavailable"
    assert profile.memory_principal_id is None


def test_concurrent_persona_selection_is_request_local(agent_root: Path) -> None:
    human = _case("healthy_human")
    service = _case("bounded_service")
    _install_cases(agent_root, human, service)
    inputs = (
        [{"SKAGENT": human["profile_id"]}] * 40
        + [{"SKAGENT": service["profile_id"]}] * 40
        + [{"SKMEMORY_AGENT": service["profile_id"]}] * 40
        + [{}] * 40
    )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(agents.get_active_agent, inputs))

    assert results == (
        [human["profile_id"]] * 40
        + [None] * 40
        + [service["profile_id"]] * 40
        + [human["profile_id"]] * 40
    )


def test_explicit_selector_conflicts_fail_closed(agent_root: Path) -> None:
    human = _case("healthy_human")
    service = _case("bounded_service")
    _install_cases(agent_root, human, service)

    assert (
        agents.get_active_agent(
            {"SKAGENT": human["profile_id"], "SKMEMORY_AGENT": service["profile_id"]}
        )
        is None
    )
    assert (
        agents.get_active_agent(
            {"SKAGENT": human["profile_id"], "SKMEMORY_AGENT": human["profile_id"]}
        )
        == human["profile_id"]
    )


def test_service_entrypoints_require_registered_profile(
    agent_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _case("bounded_service")
    _configure_profile(agent_root, service)

    assert PGVectorBackend(agent=service["profile_id"]).agent == service["profile_id"]
    assert AGEGraphBackend(agent=service["profile_id"]).agent == service["profile_id"]
    with pytest.raises(ValueError, match="Unregistered or invalid"):
        PGVectorBackend(agent="unregistered-service")
    with pytest.raises(ValueError, match="Unregistered or invalid"):
        AGEGraphBackend(agent="unregistered-service")

    monkeypatch.setattr(Path, "home", lambda: agent_root)
    with pytest.raises(ValueError, match="Unregistered or invalid"):
        register_mcp(
            "skmemory",
            "skmemory-mcp",
            [],
            env={"SKMEMORY_AGENT": "unregistered-service"},
            environments=["opencode"],
        )
    assert not (agent_root / ".config/opencode/opencode.json").exists()


def test_generated_service_mcp_env_ignores_ambient_human_selector(
    agent_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = _case("healthy_human")
    service = _case("bounded_service")
    _install_cases(agent_root, human, service)
    monkeypatch.setenv("SKAGENT", human["profile_id"])
    monkeypatch.setenv("SKMEMORY_AGENT", service["profile_id"])
    monkeypatch.setattr(Path, "home", lambda: agent_root)

    generated = _memory_env(service["profile_id"])
    assert generated == {"SKMEMORY_AGENT": service["profile_id"]}
    assert agents.get_active_agent(generated) == service["profile_id"]

    result = register_mcp(
        "skmemory",
        "skmemory-mcp",
        [],
        env={"SKAGENT": human["profile_id"], "SKMEMORY_AGENT": service["profile_id"]},
        environments=["opencode"],
    )
    assert result == {"opencode": "created"}
    entry = json.loads((agent_root / ".config/opencode/opencode.json").read_text())["mcp"][
        "skmemory"
    ]
    assert entry["env"] == {"SKMEMORY_AGENT": service["profile_id"]}


def test_sensitivity_rehashed_service_privilege_attempt_is_denied(tmp_path: Path) -> None:
    service = _case("bounded_service")
    service["profile"]["fallback_eligible"] = True
    service["profile"]["profile_hash"] = profile_content_hash(service["profile"])
    binding = next(
        item
        for item in service["registry"]["profiles"]
        if item["profile_id"] == service["profile_id"]
    )
    binding["fallback_eligible"] = True
    binding["profile_hash"] = service["profile"]["profile_hash"]
    service["registry"]["registry_hash"] = registry_content_hash(service["registry"])
    _write_case(tmp_path, service)

    profile = resolve_memory_profile(tmp_path, service["profile_id"])
    assert not profile.healthy
    assert profile.memory_principal_id is None
