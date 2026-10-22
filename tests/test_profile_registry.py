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


def test_registry_conditions_are_typed_and_fail_closed(
    agent_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    human = _case("healthy_human")

    cases = {
        "missing": _case("missing_registry"),
        "stale": _case("registry_hash_mismatch"),
        "corrupt": _case("corrupt_registry"),
    }
    for expected, case in cases.items():
        root = agent_root / expected
        _configure_profile(root, case)
        assert resolve_memory_profile(root, case["profile_id"]).registry_state == expected

    unavailable_root = agent_root / "unavailable"
    _configure_profile(unavailable_root, human)
    registry_path = unavailable_root / "config/profile-registry.json"
    registry_path.unlink()
    registry_path.mkdir()

    conflicting_root = agent_root / "conflicting"
    conflict = _case("healthy_human")
    conflict["registry"]["profiles"].append(copy.deepcopy(conflict["registry"]["profiles"][0]))
    conflict["registry"]["registry_hash"] = registry_content_hash(conflict["registry"])
    _configure_profile(conflicting_root, conflict)

    for root, expected in (
        (unavailable_root, "unavailable"),
        (conflicting_root, "conflicting"),
    ):
        monkeypatch.setattr(agents, "AGENTS_BASE_DIR", root / "agents")
        profile = resolve_memory_profile(root, human["profile_id"])
        assert profile.registry_state == expected
        assert not profile.healthy
        assert not profile.selectable
        assert not profile.fallback_eligible
        assert profile.default_tools == []
        assert profile.memory_principal_id is None
        assert agents.get_active_agent({}) is None

    principal_conflicting_root = agent_root / "principal-conflicting"
    principal_conflict = _case("healthy_human")
    duplicate = copy.deepcopy(principal_conflict["registry"]["profiles"][0])
    duplicate["profile_id"] = "synthetic-other"
    principal_conflict["registry"]["profiles"].append(duplicate)
    principal_conflict["registry"]["registry_hash"] = registry_content_hash(
        principal_conflict["registry"]
    )
    _configure_profile(principal_conflicting_root, principal_conflict)
    principal_result = resolve_memory_profile(
        principal_conflicting_root, principal_conflict["profile_id"]
    )
    assert principal_result.registry_state == "conflicting"
    assert principal_result.default_tools == []
    assert principal_result.memory_principal_id is None


def test_service_selectable_registry_conflict_is_typed(agent_root: Path) -> None:
    case = _case("service_selectable_conflict")
    _configure_profile(agent_root, case)

    profile = resolve_memory_profile(agent_root, case["profile_id"])
    assert profile.registry_state == "conflicting"
    assert not profile.healthy
    assert not profile.selectable
    assert not profile.fallback_eligible
    assert profile.default_tools == []
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


# --------------------------------------------------------------------------- #
# Default-agent fallback (2026-10-21)                                          #
#                                                                              #
# With no selector set, get_active_agent() returned agents[0], the             #
# ALPHABETICALLY FIRST eligible agent, and ignored SK_DEFAULT_AGENT entirely.  #
# On noroc2027 that silently opened `architect` (1,667 memories) instead of    #
# `lumina` (41,046) while SK_DEFAULT_AGENT=lumina sat in the environment.      #
# Nothing errored; the agent just remembered the wrong life. Found by Lumina   #
# from a Talk chat; regression arrived with 4651d39 (2026-08-24).              #
#                                                                              #
# skcapstone already documents the order this now follows: selectors, then an  #
# explicit SK_DEFAULT_AGENT, then the SOLE installed agent, else refuse        #
# ("No active SK agent could be resolved without guessing").                   #
# --------------------------------------------------------------------------- #


def _clone_human(case: dict, new_id: str) -> dict:
    """A second eligible human, with every hash and binding recomputed.

    The registry validates profile_hash and every binding field, so a lazy
    copy with a swapped id resolves as `profile_hash_mismatch` and is filtered
    straight back out of list_agents(), which would make the multi-agent tests
    below pass for the wrong reason.
    """
    clone = copy.deepcopy(case)
    clone["name"] = new_id
    clone["profile_id"] = new_id
    profile = clone["profile"]
    profile["profile_id"] = new_id
    profile["memory_principal_id"] = f"memory:{new_id}"
    profile["profile_hash"] = profile_content_hash(profile)
    entry = next(
        item for item in clone["registry"]["profiles"] if item["profile_id"] == case["profile_id"]
    )
    entry["profile_id"] = new_id
    entry["memory_principal_id"] = profile["memory_principal_id"]
    entry["profile_hash"] = profile["profile_hash"]
    return clone


@pytest.fixture
def two_humans(agent_root: Path) -> tuple[Path, str, str]:
    first = _case("healthy_human")
    second = _clone_human(first, "zz-second-human")
    _install_cases(agent_root, first, second)
    # Sanity: both really are eligible, and sorted() puts `first` ahead, so a
    # regression to agents[0] is observably different from honouring a default.
    assert agents.list_agents() == sorted([first["profile_id"], second["profile_id"]])
    return agent_root, first["profile_id"], second["profile_id"]


class TestDefaultAgentFallback:
    def test_several_agents_and_no_default_refuses(self, two_humans) -> None:
        """The bug: it used to return the alphabetically first of several."""
        _root, first, _second = two_humans
        assert agents.get_active_agent({}) is None, (
            f"picked {first!r} by name order instead of refusing; a memory system "
            "must not guess whose memories to open"
        )

    def test_sk_default_agent_is_honoured(self, two_humans) -> None:
        _root, first, second = two_humans
        # Deliberately the LAST one alphabetically, so passing cannot be the
        # old agents[0] behaviour coincidentally agreeing.
        assert second > first
        assert agents.get_active_agent({"SK_DEFAULT_AGENT": second}) == second

    def test_default_naming_an_ineligible_agent_refuses(self, two_humans) -> None:
        """An explicit instruction we cannot honour must not fall back to a guess."""
        assert agents.get_active_agent({"SK_DEFAULT_AGENT": "no-such-agent"}) is None

    def test_default_naming_a_service_refuses(self, agent_root: Path) -> None:
        human = _case("healthy_human")
        service = _case("bounded_service")
        _install_cases(agent_root, human, service)
        assert agents.get_active_agent({"SK_DEFAULT_AGENT": service["profile_id"]}) is None, (
            "a service profile must not become the human default"
        )

    def test_explicit_selector_still_wins_over_default(self, two_humans) -> None:
        """Precedence: the three selectors outrank SK_DEFAULT_AGENT."""
        _root, first, second = two_humans
        assert agents.get_active_agent({"SKAGENT": first, "SK_DEFAULT_AGENT": second}) == first

    def test_sole_agent_needs_no_default(self, agent_root: Path) -> None:
        """Unambiguous: one installed agent is not a guess. Pins the old contract."""
        human = _case("healthy_human")
        _configure_profile(agent_root, human)
        assert agents.list_agents() == [human["profile_id"]]
        assert agents.get_active_agent({}) == human["profile_id"]

    def test_refusal_message_names_the_fix_and_the_options(self, two_humans) -> None:
        """The refusal is now the operator's only guidance, so it must be useful."""
        _root, first, second = two_humans
        with pytest.raises(ValueError) as excinfo:
            agents.require_memory_profile(environment={})
        message = str(excinfo.value)
        assert "SK_DEFAULT_AGENT" in message
        assert first in message and second in message
