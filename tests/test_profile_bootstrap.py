"""Tests for the SKPM-PROF-01 registry writer (bootstrap/repair path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.profile_registry import (
    REGISTRY_PATH,
    discover_profile_ids,
    resolve_memory_profile,
    sync_profile_registry,
)


def _agent(root: Path, name: str) -> Path:
    directory = root / "agents" / name / "config"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "skmemory.yaml").write_text("agent: " + name, encoding="utf-8")
    return root / "agents" / name


def test_discover_skips_agents_without_config_and_templates(tmp_path: Path) -> None:
    """Only configured, non-template agent directories are discoverable."""
    _agent(tmp_path, "lumina")
    _agent(tmp_path, "jarvis")
    (tmp_path / "agents" / "no-config").mkdir(parents=True)
    _agent(tmp_path, "lumina-template")

    assert discover_profile_ids(tmp_path / "agents") == ["jarvis", "lumina"]


def test_sync_writes_registry_that_resolves_healthy(tmp_path: Path) -> None:
    """A freshly written registry resolves every agent as a healthy human profile."""
    _agent(tmp_path, "lumina")
    _agent(tmp_path, "opus")

    written = sync_profile_registry(tmp_path, ["lumina", "opus"])

    assert written == ["lumina", "opus"]
    for name in ("lumina", "opus"):
        profile = resolve_memory_profile(tmp_path, name, agents_base=tmp_path / "agents")
        assert profile.healthy is True
        assert profile.state == "healthy"
        assert profile.profile_kind == "human"
        assert profile.selectable is True
        assert profile.fallback_eligible is True
        assert profile.memory_principal_id == f"memory:{name}"


def test_sync_is_idempotent(tmp_path: Path) -> None:
    """Re-running the writer leaves byte-identical documents."""
    _agent(tmp_path, "lumina")

    sync_profile_registry(tmp_path, ["lumina"])
    first = (tmp_path / REGISTRY_PATH).read_bytes()
    sync_profile_registry(tmp_path, ["lumina"])

    assert (tmp_path / REGISTRY_PATH).read_bytes() == first


def test_sync_adding_an_agent_keeps_existing_profiles_healthy(tmp_path: Path) -> None:
    """Registering a new agent does not invalidate the agents already present."""
    _agent(tmp_path, "lumina")
    sync_profile_registry(tmp_path, ["lumina"])

    _agent(tmp_path, "ava")
    sync_profile_registry(tmp_path, ["ava", "lumina"])

    for name in ("ava", "lumina"):
        assert (
            resolve_memory_profile(tmp_path, name, agents_base=tmp_path / "agents").healthy is True
        )


def test_sync_repairs_a_tampered_profile_hash(tmp_path: Path) -> None:
    """A corrupted profile.json is rewritten back to a resolvable state."""
    _agent(tmp_path, "lumina")
    sync_profile_registry(tmp_path, ["lumina"])
    profile_file = tmp_path / "agents" / "lumina" / "profile.json"
    document = json.loads(profile_file.read_text(encoding="utf-8"))
    document["profile_hash"] = "sha256:" + "0" * 64
    profile_file.write_text(json.dumps(document), encoding="utf-8")

    assert (
        resolve_memory_profile(tmp_path, "lumina", agents_base=tmp_path / "agents").healthy
        is False
    )

    sync_profile_registry(tmp_path, ["lumina"])

    assert (
        resolve_memory_profile(tmp_path, "lumina", agents_base=tmp_path / "agents").healthy is True
    )


def test_sync_rejects_an_invalid_profile_id(tmp_path: Path) -> None:
    """Identifiers outside the SKPM-PROF-01 pattern fail closed at write time."""
    with pytest.raises(ValueError, match="invalid profile_id"):
        sync_profile_registry(tmp_path, ["../escape"])
