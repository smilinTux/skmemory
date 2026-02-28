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
│ SQLite   │   SKVector   │       SKGraph             │
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

```mermaid
graph TB
    Agent["Agent / CLI"] --> MS["MemoryStore"]
    MS --> L0["Level 0: SQLite\nAlways on, zero deps"]
    MS --> L1["Level 1: SKVector\nSemantic search\nPowered by Qdrant"]
    MS --> L2["Level 2: SKGraph\nGraph traversal\nPowered by FalkorDB"]
    MS --> L3["Level 3: HA Routing\nEndpointSelector"]
    style L0 fill:#bfb,stroke:#333
    style L1 fill:#bbf,stroke:#333
    style L2 fill:#fbf,stroke:#333
    style L3 fill:#fbb,stroke:#333
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

### Level 1: SKVector (powered by Qdrant) (optional — semantic search)

**"Find the memory about that feeling we had" — even if those words aren't in it.**

Uses sentence-transformers (all-MiniLM-L6-v2) to embed memories as vectors.
SKVector stores the embeddings and enables cosine similarity search.

Install:
```bash
pip install skmemory[skvector]

# Local Docker:
docker compose up -d skvector

# Or use Qdrant Cloud (free tier: 1GB):
export SKMEMORY_SKVECTOR_URL=https://your-cluster.qdrant.io
export SKMEMORY_SKVECTOR_KEY=your-api-key
```

Resource cost: ~200MB RAM, ~100MB disk idle.

### Level 2: SKGraph (powered by FalkorDB) (optional — graph relationships)

**"What memories connect to this person?" — traverse the relationship web.**

SKGraph (Cypher over Redis protocol) stores memory-to-memory edges:
- `RELATED_TO` — explicit relationship links
- `PROMOTED_FROM` — promotion lineage chains
- `TAGGED` — tag-based clustering
- `PLANTED` — seed creator attribution

Install:
```bash
pip install skmemory[skgraph]

# Local Docker:
docker compose up -d skgraph

# Or point to external:
export SKMEMORY_SKGRAPH_URL=redis://your-host:6379
```

Resource cost: ~100MB RAM, ~150MB disk idle.

### Level 3: High Availability & Routing (optional)

**Multiple backend endpoints. Automatic failover. Latency-aware routing.**

When SKVector or SKGraph run on multiple nodes (e.g. home server + VPS via
Tailscale), the **EndpointSelector** discovers all endpoints, probes their
latency, and routes to the best one. If a node goes down, traffic shifts
to the next healthy endpoint automatically.

```
┌──────────────────────────────────────────────┐
│              EndpointSelector                 │
│    (sits between config and backends)        │
├──────────────┬───────────────────────────────┤
│   SKVector   │     SKGraph                   │
│   Endpoints  │     Endpoints                 │
│              │                               │
│ ● home:6333  │  ● home:6379                  │
│   (2ms)      │    (1ms)                      │
│ ● vps:6333   │  ● vps:6379                   │
│   (12ms)     │    (15ms)                     │
├──────────────┴───────────────────────────────┤
│  Strategies: failover | latency |            │
│  local-first | read-local-write-primary      │
├──────────────────────────────────────────────┤
│  Discovery: config.yaml + heartbeat mesh     │
└──────────────────────────────────────────────┘
```

Key properties:
- On-demand TCP probing (no background threads)
- Heartbeat mesh auto-discovers new endpoints
- Config endpoints take precedence over discovery
- Backward compatible — single-URL configs work unchanged
- No new pip dependencies (stdlib `socket`)

See **[skmemory/HA.md](skmemory/HA.md)** for full documentation, Mermaid
diagrams, configuration examples, and scaling considerations.

## Token-Optimized Agent Loading

The key problem: an AI agent has limited context. Loading 1000 full memories
would blow the context window. SKMemory solves this with tiered loading.

```mermaid
sequenceDiagram
    participant A as Agent
    participant MS as MemoryStore
    participant SQ as SQLite
    participant SV as SKVector
    participant SG as SKGraph
    A->>MS: snapshot(title, content, emotion)
    MS->>SQ: save(memory) — primary
    MS->>SV: save(memory) — embed + index
    MS->>SG: index_memory(memory) — graph edges
    A->>MS: search("connected feeling")
    MS->>SV: search_text(query) — semantic
    SV-->>MS: ranked results
    A->>MS: traverse(memory_id)
    MS->>SG: get_related(id, depth=2)
    SG-->>MS: connected nodes
```

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

```mermaid
flowchart LR
    A["skmemory context\n--max-tokens 3000"] --> B["SQLite Index"]
    B --> C["Rank by\nemotion + recency"]
    C --> D{"Within\ntoken budget?"}
    D -->|Yes| E["Add summary\n+ preview"]
    D -->|No| F["Stop"]
    E --> D
    F --> G["Compact JSON\nfor agent context"]
```

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
docker compose up -d          # Start SKVector + SKGraph
docker compose ps             # Check status
docker compose down           # Stop

# Resource usage:
#   SKVector:  ~200MB RAM, port 6333 (qdrant/qdrant)
#   SKGraph:   ~100MB RAM, port 6379 (falkordb/falkordb)
#   Combined:  ~300MB RAM total
```

All services are optional. SKMemory works perfectly with just Level 0 (SQLite).

## Configuration

Environment variables:
```bash
SKMEMORY_SKVECTOR_URL=http://localhost:6333    # SKVector endpoint
SKMEMORY_SKVECTOR_KEY=                          # SKVector API key
SKMEMORY_SKGRAPH_URL=redis://localhost:6379    # SKGraph endpoint
```

CLI global options:
```bash
skmemory --skvector-url http://remote:6333 search "that moment"
```

## Migration from FileBackend

If you have existing JSON memories from an older version:

```bash
# Rebuild the SQLite index from existing JSON files
skmemory reindex
```

The JSON files are untouched. The index is additive.
