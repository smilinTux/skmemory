"""
Dynamic agent discovery and management for SKMemory.

Scans ~/.skcapstone/agents/ to discover all configured agents,
excludes templates, and provides agent-aware path resolution.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import yaml


def _agents_base() -> Path:
    """Platform-aware base directory for all agents."""
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
    """Discover all non-template agents in ~/.skcapstone/agents/

    Scans the agents directory and returns all agent names
    except template directories.

    Returns:
        list[str]: Sorted list of agent names (e.g., ['lumina', 'john'])
    """
    if not AGENTS_BASE_DIR.exists():
        return []

    agents = []
    for entry in AGENTS_BASE_DIR.iterdir():
        if entry.is_dir() and not is_template_agent(entry.name):
            # Check if it has a valid config
            config_file = entry / "config" / "skmemory.yaml"
            if config_file.exists():
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
    except Exception:
        return None


def is_template_agent(agent_name: str) -> bool:
    """Check if an agent is a template (should be ignored).

    Args:
        agent_name: Name of the agent

    Returns:
        bool: True if this is a template agent directory
    """
    return agent_name.endswith("-template")


def get_active_agent() -> str | None:
    """Get the currently active agent from environment or default to first non-template.

    Checks in order:
    1. SKAGENT environment variable (primary source of truth)
    2. SKCAPSTONE_AGENT environment variable (authoritative agent selector)
    3. SKMEMORY_AGENT environment variable (legacy/override)
    4. First non-template agent in the directory

    Returns:
        str: Agent name, or None if no agents found
    """
    # Check environment variables (SKAGENT > SKCAPSTONE_AGENT > SKMEMORY_AGENT)
    env_agent = os.environ.get("SKAGENT") or os.environ.get("SKCAPSTONE_AGENT") or os.environ.get("SKMEMORY_AGENT")
    if env_agent and not is_template_agent(env_agent):
        agent_dir = get_agent_dir(env_agent)
        if agent_dir.exists():
            return env_agent

    # Fall back to first non-template agent
    agents = list_agents()
    if agents:
        return agents[0]

    return None


def get_agent_paths(agent_name: str | None = None) -> dict[str, Path]:
    """Get all standard paths for an agent.

    Args:
        agent_name: Name of the agent, or None to use active agent

    Returns:
        dict with keys: base, config, seeds, memory_short, memory_medium, memory_long, logs, index_db
    """
    if agent_name is None:
        agent_name = get_active_agent()

    if agent_name is None:
        raise ValueError(
            "No agent configured. Create one by copying an agent template under "
            "~/.skcapstone/agents/*-template"
        )

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
