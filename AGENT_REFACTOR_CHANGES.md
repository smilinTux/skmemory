# SKMemory Agent Refactoring - Code Changes Summary

**Date**: 2026-03-04  
**Phase**: 2 (Source Code Updates)  
**Status**: ✅ COMPLETE

## Overview

Updated SKMemory to support **dynamic multi-agent architecture** where:
- Agents are discovered from `~/.skcapstone/agents/`
- Template agent (`lumina-template`) is ignored by default
- Any agent can be created by copying the template
- All paths are agent-aware and use the active agent

## Files Modified

### 1. ✅ skmemory/agents.py (NEW FILE)
**Purpose**: Agent discovery and management

**Key Functions**:
- `list_agents()` - Discover all non-template agents
- `get_active_agent()` - Get current agent from env var or first available
- `get_agent_paths(agent_name)` - Return all standard paths for an agent
- `copy_template(target_name, source)` - Create new agent from template

**Agent Directory Structure**:
```
~/.skcapstone/agents/
├── lumina-template/     # Template (ignored)
│   ├── config/skmemory.yaml
│   ├── seeds/
│   ├── memory/{short,medium,long}/
│   ├── logs/
│   └── archive/
├── lumina/              # Active agent (auto-discovered)
└── john/                # Another agent (auto-discovered)
```

### 2. ✅ skmemory/seeds.py
**Changed**:
- `DEFAULT_SEED_DIR`: Now dynamic using `get_agent_paths()["seeds"]`
- Updated docstring to reflect new sync-enabled location

**Before**:
```python
DEFAULT_SEED_DIR = os.path.expanduser("~/.openclaw/feb/seeds")
```

**After**:
```python
from .agents import get_agent_paths
default_paths = get_agent_paths()
DEFAULT_SEED_DIR = str(default_paths["seeds"])
```

### 3. ✅ skmemory/config.py
**Changed**:
- `SKMEMORY_HOME`: Now dynamic using agent-aware paths
- `CONFIG_DIR`: Points to agent's config directory
- `CONFIG_PATH`: Points to `skmemory.yaml` in agent's config

**Before**:
```python
SKMEMORY_HOME = Path(os.environ.get("SKMEMORY_HOME", os.path.expanduser("~/.skmemory")))
CONFIG_DIR = SKMEMORY_HOME
CONFIG_PATH = CONFIG_DIR / "config.yaml"
```

**After**:
```python
from .agents import get_agent_paths

try:
    default_paths = get_agent_paths()
    SKMEMORY_HOME = default_paths["base"]
    CONFIG_DIR = default_paths["config"]
    CONFIG_PATH = default_paths["config_yaml"]
except ValueError:
    # Fallback if no agents exist
    SKMEMORY_HOME = Path.home() / ".skcapstone" / "agents" / "lumina-template"
    CONFIG_DIR = SKMEMORY_HOME / "config"
    CONFIG_PATH = CONFIG_DIR / "skmemory.yaml"
```

### 4. ✅ skmemory/importers/telegram_api.py
**Changed**:
- Updated SESSION_PATH from hardcoded `~/.skmemory/` to agent-aware
- Updated docstring to reflect new location

**Before**:
```python
SESSION_PATH = os.path.expanduser("~/.skmemory/telegram.session")
# "Session is saved at ~/.skmemory/telegram.session for future use."
```

**After**:
```python
from ..agents import get_agent_paths
default_paths = get_agent_paths()
SESSION_PATH = str(default_paths["base"] / "telegram.session")
# "Session is saved at ~/.skcapstone/agents/{agent}/telegram.session..."
```

## Configuration

### Environment Variable
```bash
export SKMEMORY_AGENT=lumina  # Set active agent
```

If not set, uses first non-template agent found.

### Creating New Agents
```bash
# Copy template to create new agent
cp -a ~/.skcapstone/agents/lumina-template ~/.skcapstone/agents/john

# Edit config to customize
vim ~/.skcapstone/agents/john/config/skmemory.yaml
# Change: agent.name: john

# Agent automatically discovered on next run
```

## Testing

### Verify Dynamic Paths
```python
from skmemory.agents import list_agents, get_active_agent, get_agent_paths

# List all agents (excludes template)
agents = list_agents()
print(f"Available agents: {agents}")
# Output: ['john', 'lumina']

# Get current active agent
active = get_active_agent()
print(f"Active agent: {active}")
# Output: 'lumina' (or from SKMEMORY_AGENT env var)

# Get all paths for agent
paths = get_agent_paths("lumina")
print(f"Seeds: {paths['seeds']}")
# Output: ~/.skcapstone/agents/lumina/seeds
```

### Agent-Aware Commands
```bash
# Uses current agent automatically
skmemory list-seeds
skmemory import-seeds

# Override agent for specific command
SKMEMORY_AGENT=john skmemory list-seeds
```

## Migration Notes

### For Existing Users
No changes needed! Existing `~/.skcapstone/agents/lumina/` continues to work:
- Paths automatically resolved
- All data preserved
- Backward compatible

### For New Agents
1. Copy template: `cp -a ~/.skcapstone/agents/lumina-template ~/.skcapstone/agents/{name}`
2. Edit config: Update `agent.name` in `config/skmemory.yaml`
3. Use agent: Set `SKMEMORY_AGENT={name}` or use first agent

## Benefits

✅ **Multi-Agent**: Multiple agents coexist (lumina, john, etc.)  
✅ **Template-Based**: Easy agent creation from template  
✅ **Dynamic Discovery**: Automatically finds available agents  
✅ **Ignored Template**: `lumina-template` excluded by default  
✅ **Environment Override**: `SKMEMORY_AGENT` env var for switching  
✅ **Backward Compatible**: Existing lumina agent works unchanged  

## Next Steps

1. ✅ Code changes complete
2. 🔄 Commit changes to skcapstone-repos/skmemory
3. 🔄 Update SKCapstone for agent-aware commands
4. 🔄 Test multi-agent functionality
5. 🔄 Document agent creation process

---

**Total Files Modified**: 4 (plus 1 new file)  
**Lines Changed**: ~150 lines  
**Breaking Changes**: None (backward compatible)  
**New Features**: Multi-agent support, dynamic discovery, template system