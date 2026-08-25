"""Exact SKPM-PROF-01 profile registry consumer."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROFILE_SCHEMA_VERSION = "skcapstone.agent-profile.v1"
REGISTRY_SCHEMA_VERSION = "skcapstone.profile-registry.v1"
FIXTURE_SCHEMA_VERSION = "skcapstone.profile-conformance-fixtures.v1"
SCHEMA_REVISION = "1"
REGISTRY_PATH = Path("config/profile-registry.json")
PROFILE_FILENAME = "profile.json"
FIXTURE_PATH = Path(__file__).parent / "data/profile-conformance-v1.json"
FIXTURE_HASH = "sha256:52f74fca0abbb0ad8fe54fc550b83827175eff1f14b5fa3aad0140ad9a8a56e1"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class _StrictModel(BaseModel):
    """Reject coercion and extension fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class AgentProfileV1(_StrictModel):
    """Exact SKPM-PROF-01 agent profile envelope."""

    schema_version: Literal["skcapstone.agent-profile.v1"]
    schema_revision: Literal["1"]
    profile_id: str = Field(pattern=_ID_PATTERN)
    profile_kind: Literal["human", "service"]
    selectable: bool
    fallback_eligible: bool
    memory_principal_id: str = Field(min_length=1)
    default_tools: list[str]
    capability_policy_ref: str = Field(min_length=1)
    profile_revision: str = Field(min_length=1)
    profile_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def enforce_kind_boundary(self) -> AgentProfileV1:
        """Keep services nonselectable, fallback-ineligible, and zero-tool."""
        if self.profile_kind == "service" and (
            self.selectable or self.fallback_eligible or self.default_tools
        ):
            raise ValueError(
                "service profiles must be nonselectable, fallback-ineligible, zero-tool"
            )
        if any(not tool.strip() for tool in self.default_tools):
            raise ValueError("default_tools entries must be non-empty")
        if len(self.default_tools) != len(set(self.default_tools)):
            raise ValueError("default_tools entries must be unique")
        return self


class RegistryProfileV1(_StrictModel):
    """Exact registry binding for one profile."""

    profile_id: str = Field(pattern=_ID_PATTERN)
    profile_kind: Literal["human", "service"]
    selectable: bool
    fallback_eligible: bool
    memory_principal_id: str = Field(min_length=1)
    schema_revision: Literal["1"]
    profile_revision: str = Field(min_length=1)
    profile_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def enforce_service_selection_boundary(self) -> RegistryProfileV1:
        """Reject a registry that advertises a selectable service."""
        if self.profile_kind == "service" and (self.selectable or self.fallback_eligible):
            raise ValueError("service registry entries cannot be selectable or fallback-eligible")
        return self


class ProfileRegistryV1(_StrictModel):
    """Exact SKPM-PROF-01 registry envelope."""

    schema_version: Literal["skcapstone.profile-registry.v1"]
    schema_revision: Literal["1"]
    registry_revision: str = Field(min_length=1)
    profiles: list[RegistryProfileV1]
    registry_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def reject_duplicate_profiles(self) -> ProfileRegistryV1:
        """Reject conflicting profile or memory ownership bindings."""
        identifiers = [entry.profile_id for entry in self.profiles]
        principals = [entry.memory_principal_id for entry in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("registry profile_id entries must be unique")
        if len(principals) != len(set(principals)):
            raise ValueError("registry memory_principal_id entries must be unique")
        return self


class ResolvedMemoryProfile(_StrictModel):
    """Safe memory projection of one exact profile."""

    profile_id: str
    state: str
    healthy: bool
    profile_kind: Literal["human", "service"] | None = None
    selectable: bool = False
    fallback_eligible: bool = False
    memory_principal_id: str | None = None
    profile_revision: str | None = None
    profile_hash: str | None = None


def _canonical_hash(document: dict[str, Any], hash_field: str) -> str:
    """Hash canonical JSON without its top-level self-hash field."""
    payload = {key: value for key, value in document.items() if key != hash_field}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def profile_content_hash(document: dict[str, Any]) -> str:
    """Return the exact profile content hash."""
    return _canonical_hash(document, "profile_hash")


def registry_content_hash(document: dict[str, Any]) -> str:
    """Return the exact registry content hash."""
    return _canonical_hash(document, "registry_hash")


def conformance_pack_hash(document: dict[str, Any]) -> str:
    """Return the exact fixture pack content hash."""
    return _canonical_hash(document, "pack_hash")


def fixture_contract_is_valid(path: Path | None = None) -> bool:
    """Validate the bundled exact public-synthetic contract fixture."""
    try:
        document = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
        return bool(
            isinstance(document, dict)
            and document.get("schema_version") == FIXTURE_SCHEMA_VERSION
            and document.get("pack_revision") == SCHEMA_REVISION
            and document.get("synthetic") is True
            and document.get("pack_hash") == FIXTURE_HASH
            and conformance_pack_hash(document) == FIXTURE_HASH
            and len(document.get("cases", [])) == 15
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _read_object(path: Path) -> dict[str, Any]:
    """Read one JSON object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _unknown(profile_id: str, state: str) -> ResolvedMemoryProfile:
    """Return a no-memory Unknown projection."""
    return ResolvedMemoryProfile(profile_id=profile_id, state=state, healthy=False)


def resolve_memory_profile(
    root: str | Path, profile_id: str, *, agents_base: str | Path | None = None
) -> ResolvedMemoryProfile:
    """Resolve exact memory ownership without borrowing or fallback."""
    if not fixture_contract_is_valid():
        return _unknown(profile_id, "fixture_contract_unavailable")
    if not re.fullmatch(_ID_PATTERN, profile_id):
        return _unknown(profile_id, "unknown_profile")

    base = Path(root).expanduser()
    profiles_dir = Path(agents_base).expanduser() if agents_base else base / "agents"
    try:
        registry_raw = _read_object(base / REGISTRY_PATH)
    except FileNotFoundError:
        return _unknown(profile_id, "missing_registry")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _unknown(profile_id, "corrupt_registry")

    if (
        registry_raw.get("schema_version") != REGISTRY_SCHEMA_VERSION
        or registry_raw.get("schema_revision") != SCHEMA_REVISION
    ):
        return _unknown(profile_id, "unknown_registry_version")
    try:
        registry = ProfileRegistryV1.model_validate(registry_raw)
    except ValidationError:
        return _unknown(profile_id, "corrupt_registry")
    if registry.registry_hash != registry_content_hash(registry_raw):
        return _unknown(profile_id, "registry_hash_mismatch")

    matches = [entry for entry in registry.profiles if entry.profile_id == profile_id]
    if not matches:
        return _unknown(profile_id, "unknown_profile")
    binding = matches[0]

    try:
        profile_raw = _read_object(profiles_dir / profile_id / PROFILE_FILENAME)
    except FileNotFoundError:
        return _unknown(profile_id, "missing_profile")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _unknown(profile_id, "corrupt_profile")

    if (
        profile_raw.get("schema_version") != PROFILE_SCHEMA_VERSION
        or profile_raw.get("schema_revision") != SCHEMA_REVISION
    ):
        return _unknown(profile_id, "unknown_profile_version")
    try:
        profile = AgentProfileV1.model_validate(profile_raw)
    except ValidationError:
        return _unknown(profile_id, "corrupt_profile")
    if profile.profile_id != profile_id:
        return _unknown(profile_id, "profile_id_mismatch")
    if profile.profile_hash != profile_content_hash(profile_raw):
        return _unknown(profile_id, "profile_hash_mismatch")
    if (
        binding.profile_kind != profile.profile_kind
        or binding.selectable != profile.selectable
        or binding.fallback_eligible != profile.fallback_eligible
        or binding.memory_principal_id != profile.memory_principal_id
        or binding.schema_revision != profile.schema_revision
        or binding.profile_revision != profile.profile_revision
        or binding.profile_hash != profile.profile_hash
    ):
        return _unknown(profile_id, "stale_profile")

    return ResolvedMemoryProfile(
        profile_id=profile_id,
        state="service_profile" if profile.profile_kind == "service" else "healthy",
        healthy=True,
        profile_kind=profile.profile_kind,
        selectable=profile.selectable,
        fallback_eligible=profile.fallback_eligible,
        memory_principal_id=profile.memory_principal_id,
        profile_revision=profile.profile_revision,
        profile_hash=profile.profile_hash,
    )
