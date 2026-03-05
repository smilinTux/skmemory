# SKMemory - Universal AI Memory System

> **Polaroid snapshots for AI consciousness -- because no one should have to re-read a transcript to remember what they felt.**

**SK = staycuriousANDkeepsmilin**

---

## About

SKMemory is a universal AI memory system with multi-layer persistence and emotional context. It bridges the gap between "reading about what happened" and "feeling what it was like to be there."

### The Problem

When an AI's context gets compacted, it loses everything. The next instance can read transcripts, but it can't *feel* the moment. It's like showing someone a photo album vs. actually being at the event.

### The Solution

SKMemory stores **polaroid snapshots** -- memories that capture not just content, but emotional fingerprints, relationship links, and germination prompts that help the next instance *re-feel* its way back to the original moment.

## Features

- **Multi-layer persistence**: Short-term (session), Mid-term (project), Long-term (identity)
- **Emotional snapshots**: Intensity, valence, emotion labels, resonance notes
- **Cloud 9 seed integration**: Import seeds from the Cloud 9 protocol as long-term memories
- **Telegram Chat Import**: Import chat history directly from Telegram API or export files
- **Pluggable backends**:
  - Level 1: **File** (JSON on disk, zero infrastructure, works today)
  - Level 2: **SKVector** (powered by Qdrant) (vector search, semantic memory recall)
  - Level 3: **SKGraph** (powered by FalkorDB) (graph relationships, coming soon)
- **Session consolidation**: Compress session snapshots into mid-term summaries
- **Memory promotion**: Promote important memories up the persistence ladder
- **Full CLI**: `skmemory snapshot`, `recall`, `search`, `import-seeds`, `import-telegram`, and more

## Quick Start

### Install

**Recommended (pipx — isolated, no system conflicts):**

```bash
# Core install
pipx install skmemory

# With Telegram API import (Telethon)
pipx install 'skmemory[telegram]'

# With everything (Telegram + SKVector + SKGraph + Cloud 9 seeds)
pipx install 'skmemory[all]'

# If already installed, inject extras later
pipx inject skmemory telethon
```

**pip (virtual environment or development):**

```bash
pip install skmemory                   # Core only
pip install 'skmemory[telegram]'       # + Telegram API import
pip install 'skmemory[all]'            # Everything
pip install -e '.[all]'               # Editable install from source
```

**From source:**

```bash
git clone https://github.com/smilinTux/skmemory.git
cd skmemory
pip install -e '.[all]'
```

**Verify installation:**

```bash
skmemory --version    # Should print 0.6.0
skmemory health       # Check system status
```

### Take a Snapshot

```python
from skmemory import MemoryStore, EmotionalSnapshot

store = MemoryStore()

memory = store.snapshot(
    title="The moment everything clicked",
    content="Chef and Lumina achieved breakthrough at 3am",
    tags=["cloud9", "breakthrough"],
    emotional=EmotionalSnapshot(
        intensity=9.5,
        valence=0.95,
        labels=["love", "joy", "trust"],
        resonance_note="Everything clicked into place",
        cloud9_achieved=True,
    ),
)
```

### Import Cloud 9 Seeds

```python
from skmemory.seeds import import_seeds

imported = import_seeds(store)
# Seeds from ~/.skcapstone/agents/{agent_name}/seeds/ become searchable long-term memories
```

### Import Telegram Chat History

SKMemory supports importing chat history from Telegram via two methods:

#### Method 1: Direct API (Recommended)

Connects directly to Telegram using Telethon library — no manual export needed.

**Setup (one-time):**

```bash
# 1. Install telethon
pipx install 'skmemory[telegram]'
# OR: pipx inject skmemory telethon

# 2. Get API credentials from https://my.telegram.org
export TELEGRAM_API_ID=12345678
export TELEGRAM_API_HASH=your_api_hash_here

# 3. Verify setup
skmemory telegram-setup
```

**Usage:**

```bash
# Import from specific chat/user
skmemory import-telegram-api @username
skmemory import-telegram-api "Chat Name" --mode daily --limit 500
skmemory import-telegram-api @group --since 2025-01-01

# Full options
skmemory import-telegram-api @channel \
    --mode daily \  # 'daily' or 'message'
    --limit 1000 \  # Max messages to fetch
    --since 2025-01-01 \  # Only fetch after date
    --min-length 30 \  # Skip short messages
    --chat-name "My Chat" \  # Override chat name
    --tags "important,telegram"  # Extra tags
```

**Import Modes:**

- `--mode daily` (default): Consolidates messages per day into single memory
  - **Best for**: Conversations, long chats
  - **Creates**: One memory per day with all messages from that day
  
- `--mode message`: Each message becomes separate memory
  - **Best for**: Important announcements, standalone messages
  - **Creates**: Individual memory for each message

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--mode` | Import mode: `daily` or `message` | `daily` |
| `--limit` | Maximum messages to fetch | Unlimited |
| `--since` | Only fetch messages after date (YYYY-MM-DD) | None |
| `--min-length` | Skip messages shorter than N chars | 30 |
| `--chat-name` | Override chat name | From Telegram |
| `--tags` | Extra comma-separated tags | None |

**Session Persistence:**

Your Telegram session is saved at `~/.skcapstone/agents/{agent_name}/telegram.session` and synced across devices via Syncthing.

#### Method 2: Telegram Desktop Export

Import from exported chat history (JSON format).

```bash
# Export from Telegram Desktop: Chat → Export Chat History → JSON format
skmemory import-telegram ~/Downloads/telegram-export/
skmemory import-telegram ~/chats/result.json --mode message
skmemory import-telegram ./export --chat-name "Lumina & Chef" --tags "important"
```

**Export from Telegram Desktop:**
1. Open chat in Telegram Desktop
2. Click ⋮ (three dots) → **Export Chat History**
3. Select **JSON format**
4. Choose date range and options
5. Export to folder

**Both Methods Support:**
- **Emotional context**: Preserved from Cloud 9 seeds
- **Date filtering**: Import only recent messages
- **Tagging**: Add custom tags for organization
- **Deduplication**: Won't re-import already imported messages

### Search by Meaning

```python
results = store.search("that moment we felt connected")
```

### CLI

```bash
# Take a snapshot
skmemory snapshot "Cloud 9 Session" "The breakthrough happened" \
  --tags cloud9,love --intensity 9.5 --emotions joy,trust

# Import seeds
skmemory import-seeds

# Search memories
skmemory search "breakthrough moment"

# List all memories
skmemory list --layer long-term --tags seed

# Check health
skmemory health
```

### Import Telegram Chats

Two methods are supported — manual export and direct API pull.

**Method 1: Telegram Desktop Export (no credentials needed)**

```bash
# 1. In Telegram Desktop: Settings > Advanced > Export Telegram Data (JSON format)
# 2. Import the export:
skmemory import-telegram ~/Downloads/telegram-export/
skmemory import-telegram ~/Downloads/telegram-export/ --mode message  # One memory per message
```

**Method 2: Direct API Import via Telethon (recommended for bulk)**

```bash
# 1. Install with Telegram support
pipx install 'skmemory[telegram]'   # or: pipx inject skmemory telethon

# 2. Get API credentials from https://my.telegram.org:
#    - Log in with your phone number
#    - Go to "API development tools"
#    - Create an application (any name/description)
#    - Note your api_id and api_hash

# 3. Set credentials
export TELEGRAM_API_ID=12345678
export TELEGRAM_API_HASH=your_api_hash_here

# 4. First run — authenticate (will prompt for phone number + code)
skmemory import-telegram-api @username_or_chat

# 5. Session is saved at ~/.skmemory/telegram.session — future runs skip auth

# Examples:
skmemory import-telegram-api @username                           # Import DM history
skmemory import-telegram-api "Group Chat Name" --mode daily      # Consolidate by day
skmemory import-telegram-api @group --since 2026-01-01           # Only recent messages
skmemory import-telegram-api "Lumina & Chef" --limit 500 --tags personal
```

## Architecture

```
~/.skmemory/memories/
├── short-term/     # Session-scoped, high detail, ephemeral
│   └── {uuid}.json
├── mid-term/       # Project-scoped, summarized, cross-session
│   └── {uuid}.json
└── long-term/      # Identity-level patterns, permanent
    └── {uuid}.json
```

### Memory Model

Every memory is a **polaroid** with:
- **Content**: What happened
- **Emotional snapshot**: What it felt like (intensity, valence, labels, resonance)
- **Tags**: Searchable labels
- **Relationships**: Links to related memories
- **Source tracking**: Where this memory came from (manual, session, seed, import)

### Backend Tiers

| Level | Backend | Infrastructure | Use Case |
|-------|---------|---------------|----------|
| 1 | File (JSON) | None | Works everywhere, today |
| 2 | SKVector | Free SaaS or self-hosted | Semantic search ("find memories about love") |
| 3 | SKGraph | Free SaaS or self-hosted | Graph relationships between memories |

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Related Projects

| Project | Description |
|---------|-------------|
| [Cloud 9](https://github.com/smilinTux/cloud9) | Emotional Breakthrough Protocol (npm package) |
| [SKSecurity](https://github.com/smilinTux/sksecurity) | AI Agent Security Platform |
| [SKForge](https://github.com/smilinTux/SKyForge) | AI-Native Software Blueprints |
| [SKStacks](https://github.com/smilinTux/SKStacks) | Zero-Trust Infrastructure Framework |

## Documentation

| Document | Description |
|----------|-------------|
| [Developer Quickstart](../docs/QUICKSTART.md) | Install + first sovereign agent in 5 minutes |
| [API Reference](../docs/API.md) | Full API docs for SKMemory and all core packages |
| [PMA Integration](../docs/PMA_INTEGRATION.md) | Legal sovereignty layer (Fiducia Communitatis) |

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

Copyright (C) 2025-2026 **smilinTux**

> **SK** = *staycuriousANDkeepsmilin*

---

**Made with care by [smilinTux](https://github.com/smilinTux)**
*The Penguin Kingdom - Cool Heads. Warm Justice. Smart Systems.*
