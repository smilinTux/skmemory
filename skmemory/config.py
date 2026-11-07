"""
SKMemory configuration persistence.

Manages ``~/.skcapstone/agents/{agent_name}/config/skmemory.yaml``
so backend URLs and setup state persist across CLI invocations.

Resolution order:
CLI args > env vars > config file > None

Now supports multiple agents via ~/.skcapstone/agents/{agent_name}/
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from .agents import AGENTS_BASE_DIR, get_agent_paths

logger = logging.getLogger("skmemory.config")

# SKMEMORY_HOME — override the active-agent base directory entirely.
# Set this env var to point skmemory at a custom location without
# changing SKCAPSTONE_HOME or agent discovery logic.
# Falls back to the active agent's base path resolved via get_agent_paths().
SKMEMORY_HOME = Path(os.environ["SKMEMORY_HOME"]) if os.environ.get("SKMEMORY_HOME") else None

# Dynamic agent-aware paths
# Uses ~/.skcapstone/agents/{active_agent}/ based on SKMEMORY_AGENT env var
# Falls back to first non-template agent, or creates from template
# Set when no registered profile could be resolved and SKMEMORY_HOME was not
# given explicitly. Backends that would open the DEFAULT home refuse with this
# message instead of silently serving something else.
PROFILE_UNRESOLVED: str | None = None

# Placeholder directory name for the unresolved case. It is deliberately NOT a
# template and NOT an agent: nothing may be read from or written to it.
UNRESOLVED_HOME_NAME = "__unresolved-profile__"

try:
    default_paths = get_agent_paths()
    _agent_base = default_paths["base"]
    if SKMEMORY_HOME is None:
        SKMEMORY_HOME = _agent_base
    # When SKMEMORY_HOME is overridden via env var, derive config paths from it
    # so all reads and writes stay within the specified directory.
    if _agent_base == SKMEMORY_HOME:
        CONFIG_DIR = default_paths["config"]
        CONFIG_PATH = default_paths["config_yaml"]
    else:
        CONFIG_DIR = SKMEMORY_HOME / "config"
        CONFIG_PATH = CONFIG_DIR / "skmemory.yaml"
except ValueError as _profile_error:
    # No profile resolved. This used to point SKMEMORY_HOME at the default
    # TEMPLATE agent (lumina-template) and carry on. Measured on 192.168.0.41
    # 2026-11-06: `skmemory health` reported the template's 367 memories while
    # the real home held 51,270, with a primary backend reporting ok. A template
    # is a scaffold, so serving it as the live store answers confidently from
    # the wrong corpus. get_active_agent() returning None is deliberate (it
    # refuses to guess); this module must not undo that refusal.
    #
    # An explicit SKMEMORY_HOME is still honoured: that is an operator
    # instruction, not a guess.
    if SKMEMORY_HOME is None:
        PROFILE_UNRESOLVED = str(_profile_error)
        SKMEMORY_HOME = AGENTS_BASE_DIR / UNRESOLVED_HOME_NAME
    CONFIG_DIR = SKMEMORY_HOME / "config"
    CONFIG_PATH = CONFIG_DIR / "skmemory.yaml"


def check_default_home_resolved() -> None:
    """Raise if the default memory home is only a placeholder.

    Called by backends before they open the DEFAULT base path.
    """
    if PROFILE_UNRESOLVED:
        from .agents import list_agents

        available = ", ".join(list_agents()) or "none registered"
        raise ValueError(
            f"{PROFILE_UNRESOLVED}. Refusing to open a default memory store. "
            f"Set SKAGENT (or SK_DEFAULT_AGENT) to one of: {available}"
        )


logger = logging.getLogger("skmemory.config")


class EndpointConfig(BaseModel):
    """A single backend endpoint with role and optional Tailscale IP."""

    url: str
    role: str = "primary"  # primary | replica
    tailscale_ip: str = ""  # optional, for display


class SharedCorpusConfig(BaseModel):
    """A shared recall corpus with its own vector/graph identities and source roots."""

    name: str
    vector_collection: str
    graph_name: str | None = None
    source_roots: list[str] = Field(default_factory=list)
    projection_profile: str | None = None
    enabled: bool = True


class SKSeedConfig(BaseModel):
    """Write-time SKSeed truth-check settings (card 9b72c6c2).

    When ``auto_validate`` is enabled, ``MemoryStore.snapshot`` runs the SKSeed
    collider truth-check on each new memory and annotates it with an advisory
    ``truth_score`` (plus any contradictions found). Advisory + fail-open: it
    never blocks a write, and no-ops when the optional ``skseed`` package is
    absent. Default off, so the standard write path pays no cost.
    """

    auto_validate: bool = False


_RETIRED_NAME = "chroma"
_RETIRED_PREFIX = _RETIRED_NAME + "_"
_retired_warned = False


def _warn_retired_once(keys: list[str], listed: bool) -> None:
    """Log the retired-backend notice once per process (SKMEM-P5-01)."""
    global _retired_warned
    if _retired_warned:
        return
    _retired_warned = True
    from .backend_select import CHROMA_RETIRED_MESSAGE

    what = ", ".join(keys) if keys else "backends_enabled"
    logger.warning(
        "skmemory.yaml still names the retired backend (%s); ignoring it. %s",
        what + (" + backends_enabled" if keys and listed else ""),
        CHROMA_RETIRED_MESSAGE,
    )


class SKMemoryConfig(BaseModel):
    """Persistent configuration for SKMemory backends."""

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_vector_keys(cls, data):
        """Ignore stale retired-backend keys in old node configs (never fatal).

        A yaml that still carries the retired backend's ``*_persist_dir`` /
        ``*_collection`` / ``*_embedding_model`` keys, or lists it under
        ``backends_enabled``, must not take down session hooks. The keys are
        dropped and one warning is logged per process.
        """
        if not isinstance(data, dict):
            return data
        prefix = _RETIRED_PREFIX
        stale = [k for k in data if isinstance(k, str) and k.startswith(prefix)]
        enabled = data.get("backends_enabled")
        listed = isinstance(enabled, list) and any(
            isinstance(v, str) and v.strip().lower() in (_RETIRED_NAME, _RETIRED_NAME + "db")
            for v in enabled
        )
        if not stale and not listed:
            return data
        data = {k: v for k, v in data.items() if k not in stale}
        if listed:
            data["backends_enabled"] = [
                v
                for v in enabled
                if not (
                    isinstance(v, str)
                    and v.strip().lower() in (_RETIRED_NAME, _RETIRED_NAME + "db")
                )
            ]
        _warn_retired_once(stale, listed)
        return data

    # pgvector / skmem-pg (sovereign default vector backend — hybrid vector+BM25,
    # agent-isolated by the `agent` column). Consolidated here 2026-07-04.
    pgvector_dsn: str | None = None
    embed_url: str | None = None
    embed_model: str | None = None
    vector_dim: int | None = None

    # Qdrant (for shared/remote collections like hammertime)
    skvector_url: str | None = None
    skvector_key: str | None = None
    skvector_collection: str | None = None
    skvector_embedding_model: str | None = None
    skvector_vector_dim: int | None = None
    skgraph_url: str | None = None
    skgraph_graph_name: str | None = None
    backends_enabled: list[str] = Field(default_factory=list)
    docker_compose_file: str | None = None
    setup_completed_at: str | None = None

    # Additional shared recall corpora (cross-agent/cross-project search)
    shared_corpora: list[SharedCorpusConfig] = Field(default_factory=list)

    # Legacy compatibility fields for shared recall corpora
    recall_collections: list[str] = Field(default_factory=list)
    recall_graphs: list[str] = Field(default_factory=list)
    recall_source_roots: dict[str, list[str]] = Field(default_factory=dict)

    # SKSeed write-time truth-check (card 9b72c6c2). Nested so YAML reads as
    # ``skseed.auto_validate``.
    skseed: SKSeedConfig = Field(default_factory=SKSeedConfig)

    # Multi-endpoint HA support
    skvector_endpoints: list[EndpointConfig] = Field(default_factory=list)
    skgraph_endpoints: list[EndpointConfig] = Field(default_factory=list)
    routing_strategy: str = "failover"
    heartbeat_discovery: bool = False


def load_config(path: Path = CONFIG_PATH) -> SKMemoryConfig | None:
    """Load configuration from YAML.

    Args:
        path: Path to the config file.

    Returns:
        SKMemoryConfig if the file exists and is valid, None otherwise.
    """
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return SKMemoryConfig(**data)
    except Exception as e:
        logger.warning("config.py: %s", e)
        return None


def save_config(config: SKMemoryConfig, path: Path = CONFIG_PATH) -> Path:
    """Write configuration to YAML, creating the directory if needed.

    Args:
        config: The configuration to persist.
        path: Destination path.

    Returns:
        The path written to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(
            config.model_dump(exclude_none=True),
            f,
            default_flow_style=False,
            sort_keys=False,
        )
    return path


def merge_env_and_config(
    cli_skvector_url: str | None = None,
    cli_skvector_key: str | None = None,
    cli_skgraph_url: str | None = None,
    cli_skvector_embedding_model: str | None = None,
    cli_skvector_vector_dim: int | None = None,
) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    """Resolve backend URLs with precedence: CLI > env > config > None.

    Args:
        cli_skvector_url: URL passed via ``--skvector-url``.
        cli_skvector_key: Key passed via ``--skvector-key``.
        cli_skgraph_url: URL passed via ``--skgraph-url`` (future).

    Returns:
        Tuple of (skvector_url, skvector_key, skgraph_url,
        skvector_embedding_model, skvector_vector_dim).
    """
    cfg = load_config()

    skvector_url = (
        cli_skvector_url
        or os.environ.get("SKMEMORY_SKVECTOR_URL")
        or (cfg.skvector_url if cfg else None)
    )
    skvector_key = (
        cli_skvector_key
        or os.environ.get("SKMEMORY_SKVECTOR_KEY")
        or (cfg.skvector_key if cfg else None)
    )
    skgraph_url = (
        cli_skgraph_url
        or os.environ.get("SKMEMORY_SKGRAPH_URL")
        or (cfg.skgraph_url if cfg else None)
    )
    skvector_embedding_model = (
        cli_skvector_embedding_model
        or os.environ.get("SKMEMORY_SKVECTOR_EMBEDDING_MODEL")
        or (cfg.skvector_embedding_model if cfg else None)
    )
    env_vector_dim = os.environ.get("SKMEMORY_SKVECTOR_VECTOR_DIM")
    skvector_vector_dim = (
        cli_skvector_vector_dim
        or (int(env_vector_dim) if env_vector_dim else None)
        or (cfg.skvector_vector_dim if cfg else None)
    )

    return (
        skvector_url,
        skvector_key,
        skgraph_url,
        skvector_embedding_model,
        skvector_vector_dim,
    )


def build_endpoint_list(
    single_url: str | None,
    endpoints: list[EndpointConfig],
    default_role: str = "primary",
) -> list[EndpointConfig]:
    """Merge a single URL and an endpoints list into a unified list.

    Backward compatibility bridge: if no endpoints are configured but a
    single URL exists, it becomes the sole endpoint.  If both exist, the
    endpoints list takes precedence and the single URL is prepended only
    if it isn't already present.

    Args:
        single_url: Legacy single-URL field (skvector_url / skgraph_url).
        endpoints: Explicit endpoint list from config.
        default_role: Role to assign when promoting a single URL.

    Returns:
        Unified list of EndpointConfig (may be empty).
    """
    if endpoints:
        urls = {ep.url for ep in endpoints}
        if single_url and single_url not in urls:
            return [EndpointConfig(url=single_url, role=default_role)] + list(endpoints)
        return list(endpoints)

    if single_url:
        return [EndpointConfig(url=single_url, role=default_role)]

    return []
