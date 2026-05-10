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
from pydantic import BaseModel, Field

from .agents import AGENTS_BASE_DIR, get_agent_paths, get_default_template_agent

logger = logging.getLogger("skmemory.config")

# SKMEMORY_HOME — override the active-agent base directory entirely.
# Set this env var to point skmemory at a custom location without
# changing SKCAPSTONE_HOME or agent discovery logic.
# Falls back to the active agent's base path resolved via get_agent_paths().
SKMEMORY_HOME = Path(os.environ["SKMEMORY_HOME"]) if os.environ.get("SKMEMORY_HOME") else None

# Dynamic agent-aware paths
# Uses ~/.skcapstone/agents/{active_agent}/ based on SKMEMORY_AGENT env var
# Falls back to first non-template agent, or creates from template
try:
    default_paths = get_agent_paths()
    _agent_base = default_paths["base"]
    if SKMEMORY_HOME is None:
        SKMEMORY_HOME = _agent_base
    # When SKMEMORY_HOME is overridden via env var, derive config paths from it
    # so all reads and writes stay within the specified directory.
    if SKMEMORY_HOME == _agent_base:
        CONFIG_DIR = default_paths["config"]
        CONFIG_PATH = default_paths["config_yaml"]
    else:
        CONFIG_DIR = SKMEMORY_HOME / "config"
        CONFIG_PATH = CONFIG_DIR / "skmemory.yaml"
except ValueError:
    # Fallback if no agents exist — use platform-aware AGENTS_BASE_DIR.
    # Preserve SKMEMORY_HOME if it was set via env var.
    if SKMEMORY_HOME is None:
        default_template = get_default_template_agent() or "template"
        SKMEMORY_HOME = AGENTS_BASE_DIR / default_template
    CONFIG_DIR = SKMEMORY_HOME / "config"
    CONFIG_PATH = CONFIG_DIR / "skmemory.yaml"


class EndpointConfig(BaseModel):
    """A single backend endpoint with role and optional Tailscale IP."""

    url: str
    role: str = "primary"  # primary | replica
    tailscale_ip: str = ""  # optional, for display


class SKMemoryConfig(BaseModel):
    """Persistent configuration for SKMemory backends."""

    # ChromaDB (default local vector backend)
    chroma_persist_dir: str | None = None
    chroma_collection: str | None = None
    chroma_embedding_model: str | None = None

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

    # Additional read-only recall collections (for cross-project search)
    recall_collections: list[str] = Field(default_factory=list)

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
