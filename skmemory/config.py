"""
SKMemory configuration persistence.

Manages ``~/.skmemory/config.yaml`` so backend URLs and setup state
persist across CLI invocations.  Resolution order:

    CLI args  >  env vars  >  config file  >  None
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

SKMEMORY_HOME = Path(
    os.environ.get("SKMEMORY_HOME", os.path.expanduser("~/.skmemory"))
)
CONFIG_DIR = SKMEMORY_HOME
CONFIG_PATH = CONFIG_DIR / "config.yaml"


class EndpointConfig(BaseModel):
    """A single backend endpoint with role and optional Tailscale IP."""

    url: str
    role: str = "primary"       # primary | replica
    tailscale_ip: str = ""      # optional, for display


class SKMemoryConfig(BaseModel):
    """Persistent configuration for SKMemory backends."""

    skvector_url: Optional[str] = None
    skvector_key: Optional[str] = None
    skgraph_url: Optional[str] = None
    backends_enabled: list[str] = Field(default_factory=list)
    docker_compose_file: Optional[str] = None
    setup_completed_at: Optional[str] = None

    # Multi-endpoint HA support
    skvector_endpoints: list[EndpointConfig] = Field(default_factory=list)
    skgraph_endpoints: list[EndpointConfig] = Field(default_factory=list)
    routing_strategy: str = "failover"
    heartbeat_discovery: bool = False


def load_config(path: Path = CONFIG_PATH) -> Optional[SKMemoryConfig]:
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
    except Exception:
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
    cli_skvector_url: Optional[str] = None,
    cli_skvector_key: Optional[str] = None,
    cli_skgraph_url: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve backend URLs with precedence: CLI > env > config > None.

    Args:
        cli_skvector_url: URL passed via ``--skvector-url``.
        cli_skvector_key: Key passed via ``--skvector-key``.
        cli_skgraph_url: URL passed via ``--skgraph-url`` (future).

    Returns:
        Tuple of (skvector_url, skvector_key, skgraph_url).
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

    return skvector_url, skvector_key, skgraph_url


def build_endpoint_list(
    single_url: Optional[str],
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
