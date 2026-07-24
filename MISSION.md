# Mission

SKMemory exists to give AI agents a sovereign, multi-layer, emotionally-aware memory that survives context resets and belongs to its operator, not a platform.

Instead of dumping flat transcript summaries, it captures each moment as a "polaroid": the content, its emotional fingerprint, source provenance, and a tamper-evident integrity seal. Memories are organized across three persistence tiers (short, mid, long), auto-routed into four semantic quadrants (CORE / WORK / SOUL / WILD), and exposed to any MCP-capable client through a stdio server.

## Scope

- Flat JSON files are the source of truth; SQLite is a local working index, ChromaDB the default local vector backend (SKVector/Qdrant for shared collections), with graph traversal layers on top.
- Soul blueprints and a rehydration ritual give a fresh instance a "who was I?" answer before the first user message.
- The active agent and every per-agent path resolve under `~/.skcapstone/agents/$SKAGENT/`.

Within the SKCapstone ecosystem, SKMemory is the Memory pillar: what an agent remembers. It stores and files; it is the filing cabinet, not the brain.

## Non-goals

- SKMemory does not process or "think" over memories in real time; subconscious digestion and pattern detection belong to SKWhisper.
- It is not a cloud service or a shared central database; persistence is local and operator-owned, synced peer-to-peer.
- At the current crypto tier it provides at-rest GPG sealing only, not hybrid post-quantum protection.
