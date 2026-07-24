"""
SKMemory MCP Server — memory tools for AI agents via Model Context Protocol.

Tool-agnostic: works with Cursor, Claude Code CLI, Claude Desktop,
Windsurf, Aider, Cline, or any MCP client that speaks stdio.

Tools:
    memory_store       — Store a new memory (snapshot with title + content)
    memory_search      — Full-text search across memories
    memory_check_duplicate — Advisory semantic dedup check before writing
    memory_recall      — Recall a specific memory by ID
    memory_list        — List memories with optional layer/tag filters
    memory_forget      — Delete a memory by ID
    memory_promote     — Promote a memory to a higher persistence tier
    memory_consolidate — Compress a session's memories into one mid-term memory
    memory_context     — Load token-efficient context for agent injection
    memory_export      — Export all memories to a JSON backup
    memory_import      — Restore memories from a JSON backup
    memory_health      — Full health check across all backends
    memory_graph       — Graph traversal, lineage, and cluster discovery
    memory_extract     — Extract decisions/preferences/milestones from text (no storage)
    memory_save_session — Auto-extract + save memories from a conversation transcript
    memory_pre_compact — Emergency snapshot of current context before compression

Invocation:
    python -m skmemory.mcp_server
    skmemory-mcp

Client configuration (Cursor / Claude Desktop / Claude Code CLI):
    {"mcpServers": {"skmemory": {
        "command": "skmemory-mcp"}}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .models import MemoryLayer
from .store import MemoryStore

logger = logging.getLogger("skmemory.mcp")

server = Server("skmemory")

# ---------------------------------------------------------------------------
# Shared store instance
# ---------------------------------------------------------------------------

_store: MemoryStore | None = None


def _get_store() -> MemoryStore:
    global _store
    if _store is None:
        vector = None
        # PGVector (Postgres) is the default sovereign vector store. It works OOTB on
        # any host running the local skmem-pg container + mxbai embedder; if the DB is
        # unreachable we health-gate and fall back to Chroma. Set
        # SKMEMORY_VECTOR_BACKEND to anything other than "pgvector" to skip it.
        if os.environ.get("SKMEMORY_VECTOR_BACKEND", "pgvector").lower() == "pgvector":
            try:
                from .backends.pgvector_backend import PGVectorBackend

                pg = PGVectorBackend()
                health = pg.health_check()
                if health.get("ok"):
                    vector = pg
                    logger.info("mcp_server.py: vector backend = PGVectorBackend")
                else:
                    logger.warning("mcp_server.py pgvector unhealthy, falling back: %s", health)
            except Exception as e:
                logger.warning("mcp_server.py pgvector: %s", e)
        # NOTE (prb-6f069c5e, local-per-node rebuild model): reconsider this Chroma
        # fallback. Chroma is RETIRED, and skmem-pg is now a LOCAL, per-node, writable
        # Postgres on localhost that any node rebuilds from its Syncthing-synced flat
        # JSON via skmemory/reconcile.py (embeddings are a deterministic function of
        # flat content + mxbai on .100). An unreachable local pg is an operational
        # fault that should surface loudly (or degrade to the always-on SQLite/BM25
        # recency read path), NOT silently init a retired, empty Chroma vector store
        # that masks the outage and answers recall from nothing. Left in place for now
        # to avoid a behavior change; slated to become fail-loud + SQLite-recency.
        if vector is None:
            try:
                from .agents import get_agent_paths
                from .backends.chroma_backend import SKChromaBackend

                agent_paths = get_agent_paths()
                persist_dir = str(agent_paths["memory"] / "chroma")
                state_path = agent_paths["memory"] / "chroma-state.json"
                vector = SKChromaBackend(persist_dir=persist_dir, state_path=state_path)
            except Exception as e:
                logger.warning("mcp_server.py: %s", e)
        _store = MemoryStore(vector=vector)
    return _store


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error_response(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}))]


def _memory_dict(m: Any) -> dict:
    return m.model_dump() if hasattr(m, "model_dump") else vars(m)


_LAYER_MAP = {
    "short-term": MemoryLayer.SHORT,
    "mid-term": MemoryLayer.MID,
    "long-term": MemoryLayer.LONG,
}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="memory_store",
            description="Store a new memory in SKMemory (polaroid snapshot).",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short label for this memory.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full memory content.",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["short-term", "mid-term", "long-term"],
                        "description": "Memory layer (default: short-term).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorisation.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this memory came from (default: mcp).",
                    },
                },
                "required": ["title", "content"],
            },
        ),
        Tool(
            name="memory_search",
            description="Full-text search across all SKMemory layers with optional metadata filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter results to memories matching ALL specified tags.",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["short-term", "mid-term", "long-term"],
                        "description": "Filter results to a specific memory layer.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Filter results by source (e.g. 'manual', 'mcp', 'claude-code-hook').",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_check_duplicate",
            description=(
                "Advisory pre-write duplicate check: semantically search for existing "
                "memories similar to a piece of candidate content, BEFORE storing it. "
                "Read-only — does not write, merge, or modify anything. Complements "
                "memory_store's automatic exact-hash dedup by catching near-duplicates "
                "(paraphrases, reworded notes) via embedding similarity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Candidate content to check for near-duplicates.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum similarity 0.0-1.0 to count as a match (default: 0.73, tuned for mxbai-embed-large).",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Max candidates to consider before threshold filtering (default: 5).",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memory_recall",
            description="Recall a specific memory by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The memory's unique ID.",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_list",
            description="List memories with optional layer and tag filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "layer": {
                        "type": "string",
                        "enum": ["short-term", "mid-term", "long-term"],
                        "description": "Filter by memory layer.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (all must match).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 50).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_forget",
            description="Delete (forget) a memory by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The memory's unique ID.",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_promote",
            description="Promote a memory to a higher persistence tier (short→mid→long).",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "ID of the memory to promote.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["mid-term", "long-term"],
                        "description": "Target layer to promote to.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional compressed summary for the promoted memory.",
                    },
                },
                "required": ["memory_id", "target"],
            },
        ),
        Tool(
            name="memory_consolidate",
            description=("Compress a session's short-term memories into one mid-term memory."),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier to consolidate.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Human/AI-written summary of the session.",
                    },
                },
                "required": ["session_id", "summary"],
            },
        ),
        Tool(
            name="memory_context",
            description=(
                "Load token-efficient memory context for agent system prompt injection. "
                "Uses tiered lazy loading: today's memories (full), yesterday (summaries), "
                "older (reference counts only). Deep details available via memory_search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "token_budget": {
                        "type": "integer",
                        "description": (
                            "Max tokens for context (default: 4000). "
                            "Uses word_count * 1.3 approximation."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_export",
            description="Export all memories to a dated JSON backup file.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="memory_import",
            description="Restore memories from a JSON backup file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "backup_path": {
                        "type": "string",
                        "description": "Absolute path to the backup JSON file.",
                    },
                },
                "required": ["backup_path"],
            },
        ),
        Tool(
            name="memory_health",
            description=("Full health check across all backends (primary, vector, graph)."),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="memory_graph",
            description=(
                "Graph operations: traverse connections, get lineage, find clusters, "
                "and pivot through entities or citations to related claims. Requires "
                "SKGraph backend (FalkorDB)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["traverse", "around", "lineage", "clusters", "related_claims"],
                        "description": "Graph operation to perform.",
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "Memory ID (required for traverse/around/lineage).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Traversal depth (default: 2, for traverse/around only).",
                    },
                    "pivot_type": {
                        "type": "string",
                        "enum": ["entity", "citation"],
                        "description": "Pivot type for related_claims.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Entity or citation text for related_claims.",
                    },
                },
                "required": ["action"],
            },
        ),
        # ── Synthesis & Auto-Context ──────────────────────────────
        Tool(
            name="memory_synthesize_daily",
            description=(
                "Synthesize today's (or a given date's) memories into a single "
                "narrative entry stored in mid-term. No LLM — uses tag frequency, "
                "emotional arc, and template-based narrative."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to synthesize (YYYY-MM-DD). Defaults to today.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_synthesize_dreams",
            description=(
                "Process dream-engine memories into curated narrative memories "
                "grouped by theme. Creates one mid-term memory per theme cluster."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "Only process dreams after this date (YYYY-MM-DD). "
                            "Defaults to 7 days ago."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_auto_context",
            description=(
                "Search all memory layers for context related to keywords. "
                "Deduplicates results and ranks by relevance + emotional intensity + importance. "
                "Returns results within a token budget. Use this for contextual auto-search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "Space-separated keywords to search for.",
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Max tokens for results (default: 2000).",
                    },
                },
                "required": ["keywords"],
            },
        ),
        # ── Telegram ───────────────────────────────────────────────
        Tool(
            name="telegram_import",
            description=(
                "Import a Telegram Desktop chat export into memories. "
                "Point to the export directory containing result.json."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "export_path": {
                        "type": "string",
                        "description": "Path to Telegram export directory or result.json file.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["daily", "message"],
                        "description": "Import mode (default: daily).",
                    },
                    "min_length": {
                        "type": "integer",
                        "description": "Skip messages shorter than this (default: 30).",
                    },
                    "chat_name": {
                        "type": "string",
                        "description": "Override the chat name from the export.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Extra comma-separated tags.",
                    },
                },
                "required": ["export_path"],
            },
        ),
        Tool(
            name="telegram_import_api",
            description=(
                "Import messages directly from Telegram API using Telethon. "
                "Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {
                        "type": "string",
                        "description": "Chat username, title, or numeric ID.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["daily", "message"],
                        "description": "Import mode (default: daily).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to fetch.",
                    },
                    "since": {
                        "type": "string",
                        "description": "Only fetch messages after this date (YYYY-MM-DD).",
                    },
                    "min_length": {
                        "type": "integer",
                        "description": "Skip messages shorter than this (default: 30).",
                    },
                    "chat_name": {
                        "type": "string",
                        "description": "Override the chat name.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Extra comma-separated tags.",
                    },
                },
                "required": ["chat"],
            },
        ),
        Tool(
            name="telegram_setup",
            description=(
                "Check Telegram API setup status. Reports whether Telethon is "
                "installed, API credentials are set, and a session file exists."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="telegram_catchup",
            description=(
                "Full catch-up import from a Telegram group into ALL memory tiers. "
                "Downloads chat via Telethon and distributes: last 24h → short-term, "
                "last 7 days → mid-term, older → long-term."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {
                        "type": "string",
                        "description": "Chat username, title, or numeric ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to fetch (default: 2000)",
                        "default": 2000,
                    },
                    "since": {
                        "type": "string",
                        "description": "Only messages after this date (YYYY-MM-DD)",
                    },
                    "min_length": {
                        "type": "integer",
                        "description": "Skip messages shorter than this (default: 20)",
                        "default": 20,
                    },
                    "tags": {
                        "type": "string",
                        "description": "Extra comma-separated tags",
                    },
                },
                "required": ["chat"],
            },
        ),
        # ── Memory Integrity ──────────────────────────────────────
        Tool(
            name="memory_verify",
            description=(
                "Verify integrity hashes for all stored memories. "
                "Returns a report of passed, tampered, and unsealed memories. "
                "Tampered memories are flagged with CRITICAL severity."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="memory_audit",
            description=(
                "Show the most recent audit trail entries. "
                "The audit trail is a chain-hashed JSONL log of every "
                "store/recall/delete/tamper operation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "last": {
                        "type": "integer",
                        "description": "Number of recent entries to return (default: 20).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_extract",
            description=(
                "Extract decisions, preferences, milestones, problems, and emotional moments "
                "from arbitrary text using regex pattern matching. Returns the matches without "
                "storing anything — useful for previewing what auto-save would capture."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to extract memory-worthy moments from.",
                    },
                    "min_length": {
                        "type": "integer",
                        "description": "Minimum extracted-segment length (default: 20).",
                        "default": 20,
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="memory_save_session",
            description=(
                "Auto-extract memories from a conversation transcript and store each as a "
                "short-term memory. Mirrors the Stop hook used by Claude Code's settings.json "
                "but invoked directly via MCP. Returns counts + the IDs of saved memories."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation": {
                        "type": "string",
                        "description": "Full conversation transcript text.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier (default: auto from CLAUDE_SESSION_ID env or 'mcp-session').",
                    },
                    "min_length": {
                        "type": "integer",
                        "description": "Minimum extracted-segment length (default: 100 — same as the Stop hook).",
                        "default": 100,
                    },
                },
                "required": ["conversation"],
            },
        ),
        Tool(
            name="memory_pre_compact",
            description=(
                "Emergency snapshot of current conversation context before context compression. "
                "Stores the last N characters as a single short-term memory tagged 'pre-compact'. "
                "Mirrors the PreCompact hook used by Claude Code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "description": "Current conversation context to snapshot.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier (default: auto).",
                    },
                    "tail_chars": {
                        "type": "integer",
                        "description": "How many trailing characters to save (default: 4000).",
                        "default": 4000,
                    },
                },
                "required": ["context"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        store = _get_store()

        if name == "memory_store":
            title = arguments["title"]
            content = arguments["content"]
            layer_str = arguments.get("layer", "short-term")
            tags = arguments.get("tags", [])
            source = arguments.get("source", "mcp")
            layer = _LAYER_MAP.get(layer_str, MemoryLayer.SHORT)
            memory = store.snapshot(
                title=title,
                content=content,
                layer=layer,
                tags=tags,
                source=source,
            )
            return _json_response({"memory_id": memory.id, "stored": True})

        elif name == "memory_search":
            query = arguments["query"]
            limit = int(arguments.get("limit", 10))
            tags = arguments.get("tags") or None
            layer = arguments.get("layer") or None
            source = arguments.get("source") or None
            memories = store.search(query, limit=limit, tags=tags, layer=layer, source=source)
            return _json_response([_memory_dict(m) for m in memories])

        elif name == "memory_check_duplicate":
            content = arguments["content"]
            threshold = float(arguments.get("threshold", 0.73))
            k = int(arguments.get("k", 5))
            matches = store.check_duplicate(content, threshold=threshold, k=k)
            return _json_response(
                {
                    "duplicate_candidates": matches,
                    "possible_duplicate": len(matches) > 0,
                }
            )

        elif name == "memory_recall":
            memory_id = arguments["memory_id"]
            memory = store.recall(memory_id)
            if memory is None:
                return _error_response(f"Memory not found: {memory_id}")
            return _json_response(_memory_dict(memory))

        elif name == "memory_list":
            layer_str = arguments.get("layer")
            tags = arguments.get("tags")
            limit = int(arguments.get("limit", 50))
            layer = _LAYER_MAP.get(layer_str) if layer_str else None
            memories = store.list_memories(layer=layer, tags=tags, limit=limit)
            return _json_response([_memory_dict(m) for m in memories])

        elif name == "memory_forget":
            memory_id = arguments["memory_id"]
            deleted = store.forget(memory_id)
            return _json_response({"memory_id": memory_id, "deleted": deleted})

        elif name == "memory_promote":
            memory_id = arguments["memory_id"]
            target_str = arguments["target"]
            summary = arguments.get("summary", "")
            target = _LAYER_MAP.get(target_str)
            if target is None:
                return _error_response(f"Invalid target layer: {target_str}")
            promoted = store.promote(memory_id, target, summary=summary)
            if promoted is None:
                return _error_response(f"Memory not found: {memory_id}")
            return _json_response(
                {
                    "promoted_id": promoted.id,
                    "source_id": memory_id,
                    "target_layer": target_str,
                }
            )

        elif name == "memory_consolidate":
            session_id = arguments["session_id"]
            summary = arguments["summary"]
            consolidated = store.consolidate_session(session_id, summary)
            return _json_response(
                {
                    "memory_id": consolidated.id,
                    "session_id": session_id,
                    "consolidated": True,
                }
            )

        elif name == "memory_context":
            token_budget = int(arguments.get("token_budget", 4000))
            context = store.load_context(max_tokens=token_budget)
            return _json_response(context)

        elif name == "memory_export":
            path = store.export_backup()
            return _json_response({"exported": True, "path": path})

        elif name == "memory_import":
            backup_path = arguments["backup_path"]
            count = store.import_backup(backup_path)
            return _json_response({"imported": count, "path": backup_path})

        elif name == "memory_health":
            health = store.health()
            return _json_response(health)

        elif name == "memory_graph":
            action = arguments["action"]
            if store.graph is None:
                return _error_response(
                    "SKGraph backend not configured. "
                    "Install falkordb and configure the graph backend."
                )
            if action in {"traverse", "around"}:
                mid = arguments.get("memory_id")
                if not mid:
                    return _error_response("memory_id required for traverse/around")
                depth = int(arguments.get("depth", 2))
                results = store.graph.get_related(mid, depth=depth)
                return _json_response(results)
            elif action == "lineage":
                mid = arguments.get("memory_id")
                if not mid:
                    return _error_response("memory_id required for lineage")
                chain = store.graph.get_lineage(mid)
                return _json_response(chain)
            elif action == "clusters":
                clusters = store.graph.find_clusters()
                return _json_response(clusters)
            elif action == "related_claims":
                pivot_type = arguments.get("pivot_type")
                query = arguments.get("query", "")
                if pivot_type not in {"entity", "citation"}:
                    return _error_response("pivot_type must be 'entity' or 'citation'")
                if not query:
                    return _error_response("query required for related_claims")
                if pivot_type == "entity":
                    results = store.graph.related_claims_by_entity(query)
                else:
                    results = store.graph.related_claims_by_citation(query)
                return _json_response(results)
            else:
                return _error_response(f"Unknown graph action: {action}")

        elif name == "memory_stats":
            health = store.health()
            return _json_response(health)

        elif name == "memory_verify":
            from .config import SKMEMORY_HOME
            from .fortress import FortifiedMemoryStore

            fortress = FortifiedMemoryStore(
                primary=store.primary,
                use_sqlite=False,
                audit_path=SKMEMORY_HOME / "audit.jsonl",
            )
            result = fortress.verify_all()
            return _json_response(result)

        elif name == "memory_audit":
            from .config import SKMEMORY_HOME
            from .fortress import AuditLog

            n = int(arguments.get("last", 20))
            audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")
            records = audit.tail(n)
            return _json_response(records)

        # ── Synthesis & Auto-Context tools ────────────────────
        elif name == "memory_synthesize_daily":
            from .journal import Journal
            from .synthesis import JournalSynthesizer

            date = arguments.get("date")
            synthesizer = JournalSynthesizer(store, Journal())
            memory = synthesizer.synthesize_daily(date)
            return _json_response(
                {
                    "memory_id": memory.id,
                    "title": memory.title,
                    "themes": memory.metadata.get("themes", []),
                    "memory_count": memory.metadata.get("memory_count", 0),
                }
            )

        elif name == "memory_synthesize_dreams":
            from .journal import Journal
            from .synthesis import JournalSynthesizer

            since = arguments.get("since")
            synthesizer = JournalSynthesizer(store, Journal())
            memories = synthesizer.synthesize_dreams(since)
            return _json_response(
                {
                    "synthesized": len(memories),
                    "clusters": [
                        {
                            "memory_id": m.id,
                            "title": m.title,
                            "theme": m.metadata.get("theme", ""),
                            "dream_count": m.metadata.get("dream_count", 0),
                        }
                        for m in memories
                    ],
                }
            )

        elif name == "memory_auto_context":
            keywords_str = arguments["keywords"]
            token_budget = int(arguments.get("token_budget", 2000))
            keywords = keywords_str.split()

            # Search for each keyword and collect results
            seen_ids: set[str] = set()
            all_results: list[dict] = []

            for kw in keywords[:10]:  # cap at 10 keywords
                results = store.search(kw, limit=10)
                for m in results:
                    if m.id not in seen_ids:
                        seen_ids.add(m.id)
                        all_results.append(
                            {
                                "id": m.id,
                                "title": m.title,
                                "summary": m.summary or m.content[:200],
                                "layer": m.layer.value,
                                "intensity": m.emotional.intensity,
                                "tags": m.tags[:5],
                                "source": m.source,
                            }
                        )

            # Rank by intensity (higher = more relevant emotional context)
            all_results.sort(key=lambda r: r["intensity"], reverse=True)

            # Trim to token budget (estimate: title + summary per entry)
            trimmed: list[dict] = []
            used_tokens = 0
            for entry in all_results:
                text = entry["title"] + " " + entry["summary"]
                est = int(len(text.split()) * 1.3)
                if used_tokens + est > token_budget:
                    break
                used_tokens += est
                trimmed.append(entry)

            return _json_response(
                {
                    "results": trimmed,
                    "total_found": len(all_results),
                    "returned": len(trimmed),
                    "token_estimate": used_tokens,
                }
            )

        # ── Telegram tools ────────────────────────────────────
        elif name == "telegram_import":
            from .importers.telegram import import_telegram

            export_path = arguments["export_path"]
            mode = arguments.get("mode", "daily")
            min_length = arguments.get("min_length", 30)
            chat_name = arguments.get("chat_name")
            tags_str = arguments.get("tags", "")
            tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

            stats = import_telegram(
                store,
                export_path,
                mode=mode,
                min_message_length=min_length,
                chat_name=chat_name,
                tags=tags,
            )
            return _json_response(stats)

        elif name == "telegram_import_api":
            from .importers.telegram_api import import_telegram_api

            chat = arguments["chat"]
            mode = arguments.get("mode", "daily")
            limit = arguments.get("limit")
            since = arguments.get("since")
            min_length = arguments.get("min_length", 30)
            chat_name = arguments.get("chat_name")
            tags_str = arguments.get("tags", "")
            tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

            stats = import_telegram_api(
                store,
                chat,
                mode=mode,
                limit=limit,
                since=since,
                min_message_length=min_length,
                chat_name=chat_name,
                tags=tags,
            )
            return _json_response(stats)

        elif name == "telegram_setup":
            from .importers.telegram_api import check_setup

            result = check_setup()
            return _json_response(result)

        elif name == "telegram_catchup":
            from .importers.telegram_api import import_telegram_api

            chat = arguments["chat"]
            limit = arguments.get("limit", 2000)
            since = arguments.get("since")
            min_length = arguments.get("min_length", 20)
            tags_str = arguments.get("tags", "")
            tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

            stats = import_telegram_api(
                store,
                chat,
                mode="catchup",
                limit=limit,
                since=since,
                min_message_length=min_length,
                tags=tags,
            )
            return _json_response(stats)

        elif name == "memory_extract":
            from .extractor import extract_memories

            text = arguments["text"]
            min_length = int(arguments.get("min_length", 20))
            extracted = extract_memories(text, min_length=min_length)
            return _json_response(
                [
                    {
                        "type": e.type,
                        "content": e.content,
                        "confidence": e.confidence,
                        "source_line": e.source_line,
                    }
                    for e in extracted
                ]
            )

        elif name == "memory_save_session":
            import os

            from .extractor import extract_memories

            conversation = arguments["conversation"]
            session_id = (
                arguments.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "mcp-session"
            )
            min_length = int(arguments.get("min_length", 100))
            if len(conversation) < min_length:
                return _json_response(
                    {
                        "extracted": 0,
                        "saved": 0,
                        "session_id": session_id,
                        "memories": [],
                        "skipped": "below_min_length",
                    }
                )
            extracted = extract_memories(conversation)
            saved_memories = []
            for mem in extracted:
                try:
                    memory = store.snapshot(
                        title=f"[auto-{mem.type}] {mem.content[:60]}",
                        content=mem.content,
                        layer=MemoryLayer.SHORT,
                        tags=["auto-extract", mem.type, f"session:{session_id}"],
                        source="mcp:memory_save_session",
                        source_ref=f"session:{session_id}",
                        metadata={
                            "extraction_confidence": mem.confidence,
                            "extraction_type": mem.type,
                            "tool": "memory_save_session",
                        },
                    )
                    saved_memories.append(
                        {"id": memory.id, "type": mem.type, "title": memory.title}
                    )
                except Exception as exc:
                    logger.warning("memory_save_session: failed to save extracted memory: %s", exc)
            return _json_response(
                {
                    "extracted": len(extracted),
                    "saved": len(saved_memories),
                    "session_id": session_id,
                    "memories": saved_memories,
                }
            )

        elif name == "memory_pre_compact":
            import os

            context = arguments["context"]
            session_id = (
                arguments.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "mcp-session"
            )
            tail_chars = int(arguments.get("tail_chars", 4000))
            content = context[-tail_chars:] if len(context) > tail_chars else context
            memory = store.snapshot(
                title=f"Pre-compact session snapshot ({session_id[:8]})",
                content=content,
                layer=MemoryLayer.SHORT,
                tags=["pre-compact", "auto-save", f"session:{session_id}"],
                source="mcp:memory_pre_compact",
                source_ref=f"session:{session_id}",
                metadata={
                    "tool": "memory_pre_compact",
                    "original_length": len(context),
                    "saved_length": len(content),
                },
            )
            return _json_response(
                {
                    "memory_id": memory.id,
                    "session_id": session_id,
                    "content_length": len(content),
                    "original_length": len(context),
                }
            )

        else:
            return _error_response(f"Unknown tool: {name}")

    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return _error_response(str(exc))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the SKMemory MCP server on stdio transport."""
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    asyncio.run(_run_server())


async def _run_server() -> None:
    """Async entry point for the stdio MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
