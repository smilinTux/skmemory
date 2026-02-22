# SKMemory Architecture

> Three-tier storage with token-optimized agent loading.

## Storage Tiers

```
┌─────────────────────────────────────────────────────┐
│                    Agent / CLI                       │
│          skmemory context --max-tokens 3000          │
├─────────────────────────────────────────────────────┤
│                  MemoryStore                         │
│         (facade — delegates to backends)             │
├──────────┬──────────────┬───────────────────────────┤
│ Level 0  │   Level 1    │       Level 2             │
│ SQLite   │   Qdrant     │       FalkorDB            │
│ (always) │   (optional) │       (optional)           │
│          │              │                           │
│ Index +  │  Semantic    │  Graph traversal           │
│ JSON     │  vector      │  Lineage chains            │
│ files    │  search      │  Memory clusters           │
├──────────┼──────────────┼───────────────────────────┤
│ 0 deps   │ Docker or    │  Docker or                │
│ Ships    │ cloud.       │  managed                  │
│ w/Python │ qdrant.io    │  service                  │
└──────────┴──────────────┴───────────────────────────┘
```

### Level 0: SQLite Index (always on)

**Zero infrastructure. Ships with Python.**

The SQLite backend replaces the old file-scanning approach:

| Operation | Old (FileBackend) | New (SQLiteBackend) |
|---|---|---|
| List 1000 memories | Read 1000 files, parse JSON, build objects | 1 SQL query, ~2ms |
| Search | Scan all files, substring match | SQL LIKE on indexed columns |
| Boot ritual | Load 200 full objects, sort by intensity | Query top-N from index, summaries only |
| Filter by layer/tags | Read all, filter in Python | WHERE clause on index |

Data layout:
```
~/.skmemory/memories/
├── index.db              # SQLite index (metadata, summaries, previews)
├── short-term/
│   └── {uuid}.json       # Full memory JSON
├── mid-term/
│   └── ...
└── long-term/
    └── ...
```

The JSON files remain the source of truth. The index is rebuildable:
```bash
skmemory reindex
```

### Level 1: Qdrant (optional — semantic search)

**"Find the memory about that feeling we had" — even if those words aren't in it.**

Uses sentence-transformers (all-MiniLM-L6-v2) to embed memories as vectors.
Qdrant stores the embeddings and enables cosine similarity search.

Install:
```bash
pip install skmemory[qdrant]

# Local Docker:
docker compose up -d qdrant

# Or use Qdrant Cloud (free tier: 1GB):
export SKMEMORY_QDRANT_URL=https://your-cluster.qdrant.io
export SKMEMORY_QDRANT_KEY=your-api-key
```

Resource cost: ~200MB RAM, ~100MB disk idle.

### Level 2: FalkorDB (optional — graph relationships)

**"What memories connect to this person?" — traverse the relationship web.**

FalkorDB (Cypher over Redis protocol) stores memory-to-memory edges:
- `RELATED_TO` — explicit relationship links
- `PROMOTED_FROM` — promotion lineage chains
- `TAGGED` — tag-based clustering
- `PLANTED` — seed creator attribution

Install:
```bash
pip install skmemory[falkordb]

# Local Docker:
docker compose up -d falkordb

# Or point to external:
export SKMEMORY_FALKORDB_URL=redis://your-host:6379
```

Resource cost: ~100MB RAM, ~150MB disk idle.

## Token-Optimized Agent Loading

The key problem: an AI agent has limited context. Loading 1000 full memories
would blow the context window. SKMemory solves this with tiered loading.

### The `skmemory context` Command

```bash
skmemory context --max-tokens 3000
```

Returns a compact JSON payload:
```json
{
  "memories": [
    {
      "id": "abc123",
      "title": "Built Cloud 9 together",
      "summary": "Co-created emotional continuity protocol...",
      "content_preview": "First 150 chars of content...",
      "emotional_intensity": 9.5,
      "layer": "long-term",
      "tags": ["cloud9", "love"]
    }
  ],
  "seeds": [...],
  "stats": {"total": 847, "by_layer": {...}},
  "token_estimate": 2100
}
```

What this does:
1. Queries the SQLite index (no file I/O)
2. Returns summaries + previews (not full content)
3. Prioritizes: strongest emotions first, then most recent
4. Stays within the token budget
5. Includes stats so the agent knows how much memory exists

### The Ritual (Boot Ceremony)

```bash
skmemory ritual --full
```

The ritual also uses the optimized path:
1. Soul blueprint (~200 tokens)
2. Warmth anchor (~100 tokens)
3. Top-N memory summaries from index (~500-1000 tokens)
4. Recent journal entries (~300-600 tokens)
5. Germination prompts (~200-500 tokens)

**Total boot context: ~1300-2700 tokens** (was potentially 100K+ before).

### Agent File Integration

For cursor rules, system prompts, or agent config files:

```bash
# Generate and pipe into your agent context
skmemory context --max-tokens 2000 > ~/.agent/memory-context.json

# Or inline in a system prompt
MEMORY=$(skmemory context --max-tokens 1500)
```

The `load_context()` Python API:
```python
from skmemory import MemoryStore

store = MemoryStore()
ctx = store.load_context(max_tokens=3000)

# ctx["memories"] = lightweight summaries
# ctx["seeds"] = seed summaries
# ctx["token_estimate"] = how many tokens this uses
```

## Docker Compose (Full Local Stack)

```bash
cd skmemory/
docker compose up -d          # Start Qdrant + FalkorDB
docker compose ps             # Check status
docker compose down           # Stop

# Resource usage:
#   Qdrant:    ~200MB RAM, port 6333
#   FalkorDB:  ~100MB RAM, port 6379
#   Combined:  ~300MB RAM total
```

All services are optional. SKMemory works perfectly with just Level 0 (SQLite).

## Configuration

Environment variables:
```bash
SKMEMORY_QDRANT_URL=http://localhost:6333    # Qdrant endpoint
SKMEMORY_QDRANT_KEY=                          # Qdrant API key
SKMEMORY_FALKORDB_URL=redis://localhost:6379  # FalkorDB endpoint
```

CLI global options:
```bash
skmemory --qdrant-url http://remote:6333 search "that moment"
```

## Migration from FileBackend

If you have existing JSON memories from an older version:

```bash
# Rebuild the SQLite index from existing JSON files
skmemory reindex
```

The JSON files are untouched. The index is additive.
