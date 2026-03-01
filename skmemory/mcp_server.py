"""
SKMemory MCP Server — memory tools for AI agents via Model Context Protocol.

Tool-agnostic: works with Cursor, Claude Code CLI, Claude Desktop,
Windsurf, Aider, Cline, or any MCP client that speaks stdio.

Tools:
    memory_store       — Store a new memory (snapshot with title + content)
    memory_search      — Full-text search across memories
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
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .store import MemoryStore
from .models import MemoryLayer

logger = logging.getLogger("skmemory.mcp")

server = Server("skmemory")

# ---------------------------------------------------------------------------
# Shared store instance
# ---------------------------------------------------------------------------

_store: Optional[MemoryStore] = None


def _get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
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
            description="Full-text search across all SKMemory layers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10).",
                    },
                },
                "required": ["query"],
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
            description=(
                "Compress a session's short-term memories into one mid-term memory."
            ),
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
                "Load token-efficient memory context for agent system prompt injection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_tokens": {
                        "type": "integer",
                        "description": "Approximate token budget (default: 3000).",
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
            description=(
                "Full health check across all backends (primary, vector, graph)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="memory_graph",
            description=(
                "Graph operations: traverse connections, get lineage, find clusters. "
                "Requires SKGraph backend (FalkorDB)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["traverse", "lineage", "clusters"],
                        "description": "Graph operation to perform.",
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "Memory ID (required for traverse/lineage).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Traversal depth (default: 2, for traverse only).",
                    },
                },
                "required": ["action"],
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
            memories = store.search(query, limit=limit)
            return _json_response([_memory_dict(m) for m in memories])

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
            return _json_response({
                "promoted_id": promoted.id,
                "source_id": memory_id,
                "target_layer": target_str,
            })

        elif name == "memory_consolidate":
            session_id = arguments["session_id"]
            summary = arguments["summary"]
            consolidated = store.consolidate_session(session_id, summary)
            return _json_response({
                "memory_id": consolidated.id,
                "session_id": session_id,
                "consolidated": True,
            })

        elif name == "memory_context":
            max_tokens = int(arguments.get("max_tokens", 3000))
            context = store.load_context(max_tokens=max_tokens)
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
            if action == "traverse":
                mid = arguments.get("memory_id")
                if not mid:
                    return _error_response("memory_id required for traverse")
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
            else:
                return _error_response(f"Unknown graph action: {action}")

        elif name == "memory_stats":
            health = store.health()
            return _json_response(health)

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
