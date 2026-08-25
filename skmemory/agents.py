"""
Dynamic agent discovery and management for SKMemory.

Scans ~/.skcapstone/agents/ to discover all configured agents,
excludes templates, and provides agent-aware path resolution.
"""

from __future__ import annotations

import logging
import os
import platform
from collections.abc import Mapping
from pathlib import Path

import yaml

logger = logging.getLogger("skmemory.agents")


def _agents_base() -> Path:
    """Platform-aware base directory for all agents.

    Resolution order:
    1. SKMEMORY_HOME env var, direct override (points at the agent base dir)
    2. SKCAPSTONE_HOME env var — skcapstone-wide override
    3. Windows LOCALAPPDATA convention
    4. Default: ~/.skcapstone/agents/
    """
    skmemory_home = os.environ.get("SKMEMORY_HOME", "")
    if skmemory_home:
        return Path(skmemory_home)
    skcap_home = os.environ.get("SKCAPSTONE_HOME", "")
    if skcap_home:
        return Path(skcap_home) / "agents"
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "skcapstone" / "agents"
    return Path.home() / ".skcapstone" / "agents"


# Base directory for all agents
AGENTS_BASE_DIR = _agents_base()

# Default template directory name used when a specific source is not provided.
DEFAULT_TEMPLATE_AGENT = "lumina-template"


def list_template_agents() -> list[str]:
    """Discover all template agent directories."""
    if not AGENTS_BASE_DIR.exists():
        return []

    return sorted(
        entry.name
        for entry in AGENTS_BASE_DIR.iterdir()
        if entry.is_dir() and is_template_agent(entry.name)
    )


def get_default_template_agent() -> str | None:
    """Return the preferred template agent name, if one exists."""
    preferred = get_agent_dir(DEFAULT_TEMPLATE_AGENT)
    if preferred.exists():
        return DEFAULT_TEMPLATE_AGENT

    templates = list_template_agents()
    if templates:
        return templates[0]

    return None


def list_agents() -> list[str]:
    """Discover registered human profiles eligible for default fallback."""
    if not AGENTS_BASE_DIR.exists():
        return []

    from .profile_registry import resolve_memory_profile

    root = AGENTS_BASE_DIR.parent
    agents = []
    for entry in AGENTS_BASE_DIR.iterdir():
        if entry.is_dir() and not is_template_agent(entry.name):
            profile = resolve_memory_profile(root, entry.name, agents_base=AGENTS_BASE_DIR)
            config_file = entry / "config" / "skmemory.yaml"
            if (
                config_file.exists()
                and profile.healthy
                and profile.profile_kind == "human"
                and profile.selectable
                and profile.fallback_eligible
            ):
                agents.append(entry.name)

    return sorted(agents)


def get_agent_dir(agent_name: str) -> Path:
    """Get the base directory for a specific agent.

    Args:
        agent_name: Name of the agent (e.g., 'lumina', 'john')

    Returns:
        Path: Agent's base directory
    """
    return AGENTS_BASE_DIR / agent_name


def get_agent_config(agent_name: str) -> dict | None:
    """Load agent configuration from YAML.

    Args:
        agent_name: Name of the agent

    Returns:
        dict with agent config, or None if not found/invalid
    """
    config_path = get_agent_dir(agent_name) / "config" / "skmemory.yaml"

    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("agents.py: %s", e)
        return None


def is_template_agent(agent_name: str) -> bool:
    """Check if an agent is a template (should be ignored).

    Args:
        agent_name: Name of the agent

    Returns:
        bool: True if this is a template agent directory
    """
    return agent_name.endswith("-template")


def get_active_agent(environment: Mapping[str, str] | None = None) -> str | None:
    """Resolve an explicit profile or an eligible human fallback.

    A valid service profile may own its explicit memory principal when named only
    by ``SKMEMORY_AGENT``. Conflicting selectors, invalid profiles, and services
    named by human selectors fail closed instead of borrowing another identity.
    """
    from .profile_registry import resolve_memory_profile

    source = os.environ if environment is None else environment
    selected = {
        variable: source.get(variable, "").strip()
        for variable in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT")
        if source.get(variable, "").strip()
    }
    if len(set(selected.values())) > 1:
        return None
    if selected:
        profile_id = next(iter(selected.values()))
        profile = resolve_memory_profile(
            AGENTS_BASE_DIR.parent, profile_id, agents_base=AGENTS_BASE_DIR
        )
        if not profile.healthy:
            return None
        if profile.profile_kind == "service":
            return profile_id if set(selected) == {"SKMEMORY_AGENT"} else None
        return profile_id if profile.selectable else None

    agents = list_agents()
    return agents[0] if agents else None


def require_memory_profile(
    agent_name: str | None = None, environment: Mapping[str, str] | None = None
):
    """Return a validated explicit memory owner, or raise without fallback."""
    from .profile_registry import resolve_memory_profile

    selected = agent_name if agent_name is not None else get_active_agent(environment)
    if not selected:
        raise ValueError("No valid registered memory profile is available")
    profile = resolve_memory_profile(AGENTS_BASE_DIR.parent, selected, agents_base=AGENTS_BASE_DIR)
    if not profile.healthy:
        raise ValueError(f"Unregistered or invalid memory profile: {selected}")
    return profile


def get_agent_paths(agent_name: str | None = None) -> dict[str, Path]:
    """Get all standard paths for an agent.

    Args:
        agent_name: Name of the agent, or None to use active agent

    Returns:
        dict with keys: base, config, seeds, memory_short, memory_medium, memory_long, logs, index_db
    """
    if agent_name is None:
        agent_name = get_active_agent()
    else:
        from .profile_registry import resolve_memory_profile

        profile = resolve_memory_profile(
            AGENTS_BASE_DIR.parent, agent_name, agents_base=AGENTS_BASE_DIR
        )
        if not profile.healthy:
            agent_name = None

    if agent_name is None:
        raise ValueError("No valid registered memory profile is available")

    base = get_agent_dir(agent_name)

    return {
        "base": base,
        "config": base / "config",
        "seeds": base / "seeds",
        "memory_short": base / "memory" / "short-term",
        "memory_medium": base / "memory" / "mid-term",
        "memory_long": base / "memory" / "long-term",
        "logs": base / "logs",
        "archive": base / "archive",
        "index_db": base / "memory" / "index.db",
        "config_yaml": base / "config" / "skmemory.yaml",
    }


def ensure_agent_dirs(agent_name: str) -> Path:
    """Create all standard directories for an agent if they don't exist.

    Args:
        agent_name: Name of the agent

    Returns:
        Path: Agent's base directory
    """
    paths = get_agent_paths(agent_name)

    # Create all directories
    for key, path in paths.items():
        if key != "config_yaml":
            path.mkdir(parents=True, exist_ok=True)

    return paths["base"]


def copy_template(target_name: str, source: str | None = None) -> Path:
    """Create a new agent by copying the template.

    Args:
        target_name: Name for the new agent
        source: Template to copy from. Defaults to the preferred available
            template.

    Returns:
        Path: New agent's base directory
    """
    import shutil

    if source is None:
        source = get_default_template_agent()
    if source is None:
        raise ValueError("No agent template found under ~/.skcapstone/agents/*-template")

    source_dir = get_agent_dir(source)
    target_dir = get_agent_dir(target_name)

    if not source_dir.exists():
        raise ValueError(f"Template '{source}' not found at {source_dir}")

    if target_dir.exists():
        raise ValueError(f"Agent '{target_name}' already exists at {target_dir}")

    # Copy template
    shutil.copytree(source_dir, target_dir)

    # Update agent name in config
    config_path = target_dir / "config" / "skmemory.yaml"
    if config_path.exists():
        with open(config_path) as f:
            content = f.read()

        # Replace template agent name with new name
        content = content.replace(f"name: {source}", f"name: {target_name}")
        # Use platform-aware base dir for config path values.
        # Always use forward slashes in YAML for cross-platform consistency.
        base = AGENTS_BASE_DIR.as_posix()
        content = content.replace(
            f"sync_root: ~/.skcapstone/agents/{source}",
            f"sync_root: {base}/{target_name}",
        )
        content = content.replace(
            f"seeds_dir: ~/.skcapstone/agents/{source}/seeds",
            f"seeds_dir: {base}/{target_name}/seeds",
        )
        content = content.replace(
            f"local_db: ~/.skcapstone/agents/{source}/index.db",
            f"local_db: {base}/{target_name}/index.db",
        )

        with open(config_path, "w") as f:
            f.write(content)

    return target_dir
