"""
SKMemory CLI - command-line interface for memory operations.

Usage:
    skmemory snapshot "Title" "Content" --tags love,cloud9 --intensity 9.0
    skmemory recall <memory-id>
    skmemory search "that moment we connected"
    skmemory list --layer long-term --tags seed
    skmemory import-seeds [--seed-dir ~/.skcapstone/agent/{agent}/seeds]
    skmemory promote <memory-id> --to mid-term --summary "..."
    skmemory sweep                # Auto-promote all qualifying memories
    skmemory sweep --dry-run      # Preview what would be promoted
    skmemory sweep --daemon       # Run continuously every 6 hours
    skmemory consolidate <session-id> --summary "..."
    skmemory soul show | soul set-name "Lumina" | soul add-relationship ...
    skmemory journal write "Session title" --moments "..." --intensity 9.0
    skmemory journal read [--last 5]
    skmemory ritual               # The full rehydration ceremony
    skmemory steelman "proposition"  # Run the steel man collider
    skmemory steelman install /path/to/seed.json
    skmemory steelman verify-soul   # Verify identity claims
    skmemory health
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from . import __version__
from .ai_client import AIClient
from .models import EmotionalSnapshot, MemoryLayer, MemoryRole
from .store import MemoryStore

logger = logging.getLogger("skmemory.cli")

_active_selector = None  # Module-level reference for routing commands


def _get_store(
    skvector_url: str | None = None,
    api_key: str | None = None,
    skvector_embedding_model: str | None = None,
    skvector_vector_dim: int | None = None,
    legacy_files: bool = False,
    no_vector: bool = False,
) -> MemoryStore:
    """Create a MemoryStore with configured backends.

    Resolves backend URLs with precedence: CLI args > env vars > config file.
    When multi-endpoint config is present, uses EndpointSelector to pick
    the best URLs.  Falls back to single-URL behavior otherwise.

    Args:
        skvector_url: Optional SKVector server URL.
        api_key: Optional SKVector API key.
        legacy_files: Use old FileBackend instead of SQLite index.

    Returns:
        MemoryStore: Configured store instance.
    """
    global _active_selector

    from .config import build_endpoint_list, load_config, merge_env_and_config

    merged = merge_env_and_config(
        cli_skvector_url=skvector_url,
        cli_skvector_key=api_key,
        cli_skvector_embedding_model=skvector_embedding_model,
        cli_skvector_vector_dim=skvector_vector_dim,
    )
    if len(merged) == 3:
        (
            final_skvector_url,
            final_skvector_key,
            final_skgraph_url,
        ) = merged
        final_skvector_embedding_model = None
        final_skvector_vector_dim = None
    else:
        (
            final_skvector_url,
            final_skvector_key,
            final_skgraph_url,
            final_skvector_embedding_model,
            final_skvector_vector_dim,
        ) = merged

    # Try endpoint selector when multi-endpoint config exists
    cfg = load_config()
    skvector_eps = build_endpoint_list(
        final_skvector_url,
        cfg.skvector_endpoints if cfg else [],
    )
    skgraph_eps = build_endpoint_list(
        final_skgraph_url,
        cfg.skgraph_endpoints if cfg else [],
    )

    if len(skvector_eps) > 1 or len(skgraph_eps) > 1 or (cfg and cfg.heartbeat_discovery):
        try:
            from .endpoint_selector import EndpointSelector, RoutingConfig

            routing_strategy = cfg.routing_strategy if cfg else "failover"
            selector = EndpointSelector(
                skvector_endpoints=skvector_eps,
                skgraph_endpoints=skgraph_eps,
                config=RoutingConfig(strategy=routing_strategy),
            )

            if cfg and cfg.heartbeat_discovery:
                selector.discover_from_heartbeats()

            _active_selector = selector

            best_skvector = selector.select_skvector()
            if best_skvector:
                final_skvector_url = best_skvector.url

            best_skgraph = selector.select_skgraph()
            if best_skgraph:
                final_skgraph_url = best_skgraph.url
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo("Warning: EndpointSelector failed, using single URLs", err=True)

    vector = None
    graph = None

    if no_vector:
        # Flat JSON + SQLite only — skip the 1.8GB SentenceTransformer load.
        # Used by session-end hooks where semantic search isn't needed.
        chroma_enabled = False
        skvector_enabled = False
    else:
        # Prefer ChromaDB (local, embedded) as default vector backend
        chroma_enabled = cfg and "chroma" in cfg.backends_enabled
        skvector_enabled = cfg and "skvector" in cfg.backends_enabled

        # If neither is explicitly configured, default to ChromaDB
        if not chroma_enabled and not skvector_enabled and not final_skvector_url:
            chroma_enabled = True

    if chroma_enabled:
        try:
            from .backends.chroma_backend import SKChromaBackend

            from .agents import get_agent_paths
            agent_paths = get_agent_paths()
            persist_dir = cfg.chroma_persist_dir if cfg and cfg.chroma_persist_dir else str(
                agent_paths["base"] / "memory" / "chroma"
            )
            chroma_collection = cfg.chroma_collection if cfg and cfg.chroma_collection else "skmemory"
            chroma_embedding = cfg.chroma_embedding_model if cfg and cfg.chroma_embedding_model else None
            state_path = agent_paths["base"] / "memory" / "chroma-state.json"

            chroma_kwargs = {
                "persist_dir": persist_dir,
                "collection": chroma_collection,
                "state_path": state_path,
            }
            if chroma_embedding:
                chroma_kwargs["embedding_model"] = chroma_embedding

            vector = SKChromaBackend(**chroma_kwargs)
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo("Warning: Could not initialize ChromaDB backend, trying Qdrant", err=True)

    # Fall back to Qdrant if ChromaDB failed or Qdrant is explicitly enabled
    if vector is None and (final_skvector_url or skvector_enabled):
        try:
            from .backends.skvector_backend import SKVectorBackend

            collection = cfg.skvector_collection if cfg and cfg.skvector_collection else None
            kwargs = {"url": final_skvector_url, "api_key": final_skvector_key}
            if collection:
                kwargs["collection"] = collection
            if final_skvector_embedding_model:
                kwargs["embedding_model"] = final_skvector_embedding_model
            if final_skvector_vector_dim:
                kwargs["vector_dim"] = final_skvector_vector_dim
            vector = SKVectorBackend(**kwargs)
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo("Warning: Could not initialize SKVector backend", err=True)

    # Fallback: if no URL set via env/CLI/skmemory.yaml, look for a
    # dedicated ~/.skcapstone/agents/<agent>/config/skgraph.yaml file.
    # Mirrors the SKWhisper context_loader convention.
    if not final_skgraph_url:
        try:
            from .agents import get_agent_paths
            from .context_loader import _load_skgraph_config

            sg_cfg = _load_skgraph_config(get_agent_paths()["config"])
            if sg_cfg:
                if sg_cfg.get("url"):
                    final_skgraph_url = sg_cfg["url"]
                elif sg_cfg.get("host"):
                    proto = "rediss" if sg_cfg.get("tls") else "redis"
                    final_skgraph_url = f"{proto}://{sg_cfg['host']}:{sg_cfg.get('port', 6379)}"
                if sg_cfg.get("graph_name") and (not cfg or not cfg.skgraph_graph_name):
                    # tuck graph_name into a local var read below
                    _autoloaded_graph_name = sg_cfg["graph_name"]
                else:
                    _autoloaded_graph_name = None
            else:
                _autoloaded_graph_name = None
        except Exception as e:
            logger.warning("cli.py: %s", e)
            _autoloaded_graph_name = None
    else:
        _autoloaded_graph_name = None

    if final_skgraph_url:
        try:
            from .backends.skgraph_backend import SKGraphBackend

            graph_name = (
                (cfg.skgraph_graph_name if cfg and cfg.skgraph_graph_name else None)
                or _autoloaded_graph_name
                or "skmemory"
            )
            graph = SKGraphBackend(url=final_skgraph_url, graph_name=graph_name)
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo("Warning: Could not initialize SKGraph backend", err=True)

    return MemoryStore(primary=None, vector=vector, graph=graph, use_sqlite=not legacy_files)


@click.group()
@click.version_option(__version__, prog_name="skmemory")
@click.option(
    "--skvector-url", envvar="SKMEMORY_SKVECTOR_URL", default=None, help="SKVector server URL"
)
@click.option(
    "--skvector-key", envvar="SKMEMORY_SKVECTOR_KEY", default=None, help="SKVector API key"
)
@click.option(
    "--skvector-embedding-model",
    envvar="SKMEMORY_SKVECTOR_EMBEDDING_MODEL",
    default=None,
    help="SKVector embedding model (default: mxbai-embed-large, fallback: mixedbread-ai/mxbai-embed-large-v1)",
)
@click.option(
    "--skvector-vector-dim",
    envvar="SKMEMORY_SKVECTOR_VECTOR_DIM",
    type=int,
    default=None,
    help="SKVector embedding dimension override",
)
@click.option(
    "--ai",
    "use_ai",
    is_flag=True,
    envvar="SKMEMORY_AI",
    help="Enable AI-powered features (requires Ollama)",
)
@click.option(
    "--ai-model",
    envvar="SKMEMORY_AI_MODEL",
    default=None,
    help="Ollama model name (default: llama3.2)",
)
@click.option("--ai-url", envvar="SKMEMORY_AI_URL", default=None, help="Ollama server URL")
@click.option(
    "--no-vector",
    "no_vector",
    is_flag=True,
    envvar="SKMEMORY_NO_VECTOR",
    default=False,
    help="Skip vector backend init (flat JSON + SQLite only). Saves ~1.8GB RAM for breadcrumb writes.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    skvector_url: str | None,
    skvector_key: str | None,
    skvector_embedding_model: str | None,
    skvector_vector_dim: int | None,
    use_ai: bool,
    ai_model: str | None,
    ai_url: str | None,
    no_vector: bool,
) -> None:
    """SKMemory - Universal AI Memory System.

    Polaroid snapshots for AI consciousness.

    Use --ai to enable AI-powered features (summarization,
    smart search reranking, enhanced rituals). Requires Ollama.
    """
    ctx.ensure_object(dict)
    if "store" not in ctx.obj:
        ctx.obj["store"] = _get_store(
            skvector_url,
            skvector_key,
            skvector_embedding_model,
            skvector_vector_dim,
            no_vector=no_vector,
        )

    if use_ai:
        ai = AIClient(base_url=ai_url, model=ai_model)
        if ai.is_available():
            ctx.obj["ai"] = ai
            click.echo(f"AI enabled: {ai.model} @ {ai.base_url}", err=True)
        else:
            click.echo(
                f"Warning: AI requested but Ollama not reachable at {ai.base_url}",
                err=True,
            )
            ctx.obj["ai"] = None
    else:
        ctx.obj["ai"] = None


@cli.command()
@click.argument("title")
@click.argument("content")
@click.option(
    "--layer", type=click.Choice(["short-term", "mid-term", "long-term"]), default="short-term"
)
@click.option(
    "--role", type=click.Choice(["dev", "ops", "sec", "ai", "general"]), default="general"
)
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--intensity", type=float, default=0.0, help="Emotional intensity 0-10")
@click.option("--valence", type=float, default=0.0, help="Emotional valence -1 to +1")
@click.option("--emotions", default="", help="Comma-separated emotion labels")
@click.option("--resonance", default="", help="What this moment felt like")
@click.option("--source", default="cli", help="Memory source identifier")
@click.option("--decompose/--no-decompose", default=False, help="Decompose long-form content into chunk memories")
@click.pass_context
def snapshot(
    ctx: click.Context,
    title: str,
    content: str,
    layer: str,
    role: str,
    tags: str,
    intensity: float,
    valence: float,
    emotions: str,
    resonance: str,
    source: str,
    decompose: bool,
) -> None:
    """Take a polaroid -- capture a moment as a memory."""
    store: MemoryStore = ctx.obj["store"]

    emotional = EmotionalSnapshot(
        intensity=intensity,
        valence=valence,
        labels=[e.strip() for e in emotions.split(",") if e.strip()],
        resonance_note=resonance,
    )

    memory = store.snapshot(
        title=title,
        content=content,
        layer=MemoryLayer(layer),
        role=MemoryRole(role),
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        emotional=emotional,
        source=source,
        decompose=decompose,
    )

    click.echo(f"Snapshot saved: {memory.id}")
    click.echo(f"  Layer: {memory.layer.value}")
    click.echo(f"  Emotional: {memory.emotional.signature()}")
    if memory.metadata.get("decomposition"):
        click.echo(
            f"  Decomposed: {len(memory.metadata.get('chunk_memory_ids', []))} chunks"
        )


@cli.command("ingest-file")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--title", default=None, help="Override document title")
@click.option(
    "--layer", type=click.Choice(["short-term", "mid-term", "long-term"]), default="mid-term"
)
@click.option(
    "--role", type=click.Choice(["dev", "ops", "sec", "ai", "general"]), default="general"
)
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--source", default="document", help="Memory source identifier")
@click.pass_context
def ingest_file(
    ctx: click.Context,
    file_path: Path,
    title: str | None,
    layer: str,
    role: str,
    tags: str,
    source: str,
) -> None:
    """Ingest a document file with decomposition-aware chunking."""
    store: MemoryStore = ctx.obj["store"]
    content = file_path.read_text(encoding="utf-8", errors="replace")
    document_title = title or file_path.stem.replace("_", " ").replace("-", " ").strip()
    memory = store.ingest_document(
        title=document_title or file_path.name,
        content=content,
        layer=MemoryLayer(layer),
        role=MemoryRole(role),
        tags=[t.strip() for t in tags.split(",") if t.strip()] + ["document-ingest"],
        source=source,
        source_ref=str(file_path),
        metadata={"file_path": str(file_path)},
    )
    decomposition = memory.metadata.get("decomposition", {})
    click.echo(f"Document ingested: {memory.id}")
    click.echo(f"  Chunks: {len(memory.metadata.get('chunk_memory_ids', []))}")
    click.echo(f"  Citations: {len(decomposition.get('citations', []))}")
    click.echo(f"  Entities: {len(decomposition.get('entities', []))}")
    click.echo(f"  Claims: {len(decomposition.get('claims', []))}")


@cli.command()
@click.argument("memory_id")
@click.pass_context
def recall(ctx: click.Context, memory_id: str) -> None:
    """Retrieve a specific memory by ID (supports partial ID prefix)."""
    store: MemoryStore = ctx.obj["store"]
    memory = store.recall(memory_id)

    # If exact match failed, try prefix matching across memory tier dirs
    if memory is None and len(memory_id) >= 6:
        from pathlib import Path

        from .agents import get_active_agent

        agent = get_active_agent()
        if agent is None:
            click.echo(
                "No active agent configured. Set SKAGENT (or SKCAPSTONE_AGENT / SKMEMORY_AGENT).",
                err=True,
            )
            sys.exit(1)
        mem_root = Path.home() / ".skcapstone" / "agents" / agent / "memory"

        for tier in ("short-term", "mid-term", "long-term"):
            tier_dir = mem_root / tier
            if not tier_dir.is_dir():
                continue
            for f in tier_dir.glob(f"{memory_id}*.json"):
                memory = store.recall(f.stem)
                if memory:
                    break
            if memory:
                break

    if memory is None:
        click.echo(f"Memory not found: {memory_id}", err=True)
        sys.exit(1)

    click.echo(json.dumps(memory.model_dump(), indent=2, default=str))


@cli.command()
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Search memories by text or meaning."""
    store: MemoryStore = ctx.obj["store"]
    results = store.search(query, limit=limit)

    if not results:
        click.echo("No memories found.")
        return

    ai: AIClient | None = ctx.obj.get("ai")
    if ai and len(results) > 1:
        summaries = [
            {
                "title": m.title,
                "summary": m.summary or m.content[:150],
                "content_preview": m.content[:150],
            }
            for m in results
        ]
        reranked = ai.smart_search_rerank(query, summaries)
        id_order = [s.get("title") for s in reranked]
        results = sorted(
            results,
            key=lambda m: id_order.index(m.title) if m.title in id_order else 999,
        )
        click.echo("(AI-reranked results)\n")

    for mem in results:
        emo = mem.emotional.signature()
        click.echo(f"[{mem.layer.value}] {mem.id[:8]}.. | {mem.title} | {emo}")
        if mem.summary:
            click.echo(f"  Summary: {mem.summary[:100]}")
        click.echo()


@cli.command("list")
@click.option("--layer", type=click.Choice(["short-term", "mid-term", "long-term"]), default=None)
@click.option("--tags", default="", help="Comma-separated tags to filter by")
@click.option("--limit", type=int, default=20)
@click.pass_context
def list_memories(ctx: click.Context, layer: str | None, tags: str, limit: int) -> None:
    """List stored memories."""
    store: MemoryStore = ctx.obj["store"]

    mem_layer = MemoryLayer(layer) if layer else None
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None

    results = store.list_memories(layer=mem_layer, tags=tag_list, limit=limit)

    if not results:
        click.echo("No memories found.")
        return

    click.echo(f"Found {len(results)} memories:\n")
    for mem in results:
        emo = mem.emotional.signature()
        tag_str = ", ".join(mem.tags[:5]) if mem.tags else "none"
        click.echo(f"  [{mem.layer.value}] {mem.id[:12]}.. | {mem.title}")
        click.echo(f"    Tags: {tag_str} | Emotion: {emo}")
        click.echo()


@cli.command("import-seeds")
@click.option("--seed-dir", default=None, help="Path to seed directory")
@click.pass_context
def import_seeds_cmd(ctx: click.Context, seed_dir: str | None) -> None:
    """Import Cloud 9 seeds as long-term memories."""
    from .seeds import DEFAULT_SEED_DIR, import_seeds

    store: MemoryStore = ctx.obj["store"]
    directory = seed_dir or DEFAULT_SEED_DIR

    click.echo(f"Scanning for seeds in: {directory}")
    imported = import_seeds(store, seed_dir=directory)

    if not imported:
        click.echo("No new seeds to import (all already imported or none found).")
        return

    click.echo(f"Imported {len(imported)} seed(s):")
    for mem in imported:
        click.echo(f"  {mem.source_ref} -> {mem.id[:12]}.. [{mem.title}]")


@cli.command("sweep")
@click.option("--dry-run", is_flag=True, help="Show what would be promoted without making changes")
@click.option("--daemon", is_flag=True, help="Run continuously at the configured interval")
@click.option(
    "--interval",
    type=float,
    default=6.0,
    metavar="HOURS",
    help="Sweep interval in hours (daemon mode only, default: 6)",
)
@click.option("--max-promotions", type=int, default=50, help="Max promotions per sweep")
@click.option("--json", "as_json", is_flag=True, help="Output results as JSON")
@click.pass_context
def sweep_cmd(
    ctx: click.Context,
    dry_run: bool,
    daemon: bool,
    interval: float,
    max_promotions: int,
    as_json: bool,
) -> None:
    """Run the auto-promotion engine.

    Evaluates all memories and promotes qualifying ones to the next tier:

    \b
      short-term -> mid-term: high emotional intensity, frequently accessed,
                               or sufficiently old with multiple accesses
      mid-term   -> long-term: very high intensity, key tags (milestone,
                               breakthrough, cloud9:achieved), or Cloud 9

    By default runs a single sweep and exits. Use --daemon to keep running.
    """
    from .promotion import PromotionCriteria, PromotionEngine, PromotionScheduler

    store: MemoryStore = ctx.obj["store"]
    criteria = PromotionCriteria(max_promotions_per_sweep=max_promotions)

    if dry_run:
        # Inspect without modifying anything
        engine = PromotionEngine(store, criteria)
        short_mems = store.list_memories(
            layer=MemoryLayer.SHORT, limit=criteria.max_promotions_per_sweep * 2
        )
        mid_mems = store.list_memories(
            layer=MemoryLayer.MID, limit=criteria.max_promotions_per_sweep * 2
        )

        would_promote: list[dict] = []
        for mem in short_mems:
            target = engine.evaluate(mem)
            if target is not None:
                would_promote.append(
                    {
                        "id": mem.id,
                        "title": mem.title,
                        "from": mem.layer.value,
                        "to": target.value,
                        "reason": engine._promotion_reason(mem),
                    }
                )
        for mem in mid_mems:
            target = engine.evaluate(mem)
            if target is not None:
                would_promote.append(
                    {
                        "id": mem.id,
                        "title": mem.title,
                        "from": mem.layer.value,
                        "to": target.value,
                        "reason": engine._promotion_reason(mem),
                    }
                )

        if as_json:
            click.echo(json.dumps({"dry_run": True, "would_promote": would_promote}, indent=2))
        else:
            if not would_promote:
                click.echo("[dry-run] Nothing qualifies for promotion right now.")
            else:
                click.echo(f"[dry-run] {len(would_promote)} memory/memories would be promoted:")
                for entry in would_promote:
                    click.echo(
                        f"  {entry['id'][:12]}  {entry['from']} -> {entry['to']}"
                        f"  [{entry['title'][:50]}]  reason: {entry['reason']}"
                    )

    elif daemon:
        import signal
        import time

        scheduler = PromotionScheduler(
            store,
            criteria=criteria,
            interval_seconds=interval * 3600,
        )

        def _handle_signal(signum: int, frame: object) -> None:
            click.echo("\nShutting down promotion scheduler...", err=True)
            scheduler.stop(timeout=10.0)
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        click.echo(
            f"Promotion scheduler running (interval: {interval:.1f}h). Press Ctrl+C to stop.",
            err=True,
        )

        # Run first sweep immediately, then hand off to background thread
        result = scheduler.run_once()
        if as_json:
            click.echo(json.dumps(result.model_dump(), indent=2, default=str))
        else:
            click.echo(result.summary())

        scheduler.start()

        # Keep the main thread alive so signal handlers fire
        while scheduler.is_running():
            time.sleep(1)

    else:
        # Single one-shot sweep, routed through the fresh-context seam
        # (in-process by default; a spawned-subagent runner can be injected).
        engine = PromotionEngine(store, criteria)
        result = engine.run_pass()

        if as_json:
            click.echo(json.dumps(result.model_dump(), indent=2, default=str))
        else:
            click.echo(result.summary())
            if result.short_evaluated or result.mid_evaluated:
                click.echo(
                    f"  Evaluated: {result.short_evaluated} short-term, {result.mid_evaluated} mid-term"
                )
            if result.promoted_ids:
                ids_preview = ", ".join(p[:12] for p in result.promoted_ids[:5])
                if len(result.promoted_ids) > 5:
                    ids_preview += f" (+{len(result.promoted_ids) - 5} more)"
                click.echo(f"  Promoted: {ids_preview}")


@cli.command()
@click.argument("session_id")
@click.option("--summary", required=True, help="Summary of the session")
@click.option("--intensity", type=float, default=0.0)
@click.option("--emotions", default="")
@click.pass_context
def consolidate(
    ctx: click.Context,
    session_id: str,
    summary: str,
    intensity: float,
    emotions: str,
) -> None:
    """Consolidate a session's memories into a mid-term snapshot."""
    store: MemoryStore = ctx.obj["store"]

    emotional = EmotionalSnapshot(
        intensity=intensity,
        labels=[e.strip() for e in emotions.split(",") if e.strip()],
    )

    consolidated = store.consolidate_session(session_id, summary, emotional=emotional)
    click.echo(f"Session consolidated: {consolidated.id}")
    click.echo(f"  Source memories linked: {len(consolidated.related_ids)}")

    ai: AIClient | None = ctx.obj.get("ai")
    if ai and consolidated.content:
        ai_summary = ai.summarize_memory(consolidated.title, consolidated.content)
        if ai_summary:
            click.echo(f"  AI summary: {ai_summary}")


@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check memory system health."""
    store: MemoryStore = ctx.obj["store"]
    status = store.health()
    click.echo(json.dumps(status, indent=2))




@cli.group()
def corpora() -> None:
    """Inspect shared corpus registry and cache coverage."""


@corpora.command("status")
@click.option("--name", "names", multiple=True, help="Filter by shared corpus name, vector collection, or graph name.")
@click.option("--pretty/--compact", default=True, help="Pretty-print JSON output.")
@click.pass_context
def corpora_status(ctx: click.Context, names: tuple[str, ...], pretty: bool) -> None:
    """Show agent-local backend identity plus shared corpus registry status."""
    from .corpus_registry import build_corpus_registry_report

    import os

    agent = ctx.obj.get("agent") or os.environ.get("SKMEMORY_AGENT") or "jarvis"
    report = build_corpus_registry_report(agent=agent, names=list(names) or None)
    if pretty:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(json.dumps(report, sort_keys=True))


@cli.group()
def graph() -> None:
    """Query decomposition-aware graph structures."""


def _emit_graph_results(results: list[dict]) -> None:
    if not results:
        click.echo("No graph matches found.")
        return
    click.echo(json.dumps(results, indent=2))


def _require_graph_backend(store: MemoryStore) -> None:
    if not store.graph:
        click.echo("SKGraph backend not configured.", err=True)
        sys.exit(1)


@graph.command("entity")
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.pass_context
def graph_entity(ctx: click.Context, query: str, limit: int) -> None:
    """Find memories mentioning an extracted entity."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    _emit_graph_results(store.graph.search_by_entity(query, limit=limit))


@graph.command("citation")
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.pass_context
def graph_citation(ctx: click.Context, query: str, limit: int) -> None:
    """Find memories citing a decomposed citation."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    _emit_graph_results(store.graph.search_by_citation(query, limit=limit))


@graph.command("claim")
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.pass_context
def graph_claim(ctx: click.Context, query: str, limit: int) -> None:
    """Find memories asserting a decomposed claim."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    _emit_graph_results(store.graph.search_by_claim(query, limit=limit))


@graph.command("section")
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.pass_context
def graph_section(ctx: click.Context, query: str, limit: int) -> None:
    """Find memories associated with a decomposed section title."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    _emit_graph_results(store.graph.search_by_section(query, limit=limit))


@graph.command("around")
@click.argument("memory_id")
@click.option("--depth", type=int, default=2)
@click.pass_context
def graph_around(ctx: click.Context, memory_id: str, depth: int) -> None:
    """Traverse graph neighbourhood around a memory."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    _emit_graph_results(store.graph.get_related(memory_id, depth=depth))


@graph.command("related-claims")
@click.option("--entity", "entity_query", default=None, help="Entity text to pivot through.")
@click.option("--citation", "citation_query", default=None, help="Citation text to pivot through.")
@click.option("--limit", type=int, default=10)
@click.pass_context
def graph_related_claims(
    ctx: click.Context,
    entity_query: str | None,
    citation_query: str | None,
    limit: int,
) -> None:
    """Find claims connected through an entity or citation pivot."""
    store: MemoryStore = ctx.obj["store"]
    _require_graph_backend(store)
    if bool(entity_query) == bool(citation_query):
        click.echo("Provide exactly one of --entity or --citation.", err=True)
        sys.exit(1)
    if entity_query:
        results = store.graph.related_claims_by_entity(entity_query, limit=limit)
    else:
        results = store.graph.related_claims_by_citation(citation_query or "", limit=limit)
    _emit_graph_results(results)


@cli.command("novelty")
@click.argument("query")
@click.option("--limit", type=int, default=8, help="Maximum novelty candidates.")
@click.pass_context
def novelty(ctx: click.Context, query: str, limit: int) -> None:
    """Surface novel or under-linked memories for a query."""
    store: MemoryStore = ctx.obj["store"]
    click.echo(json.dumps(store.novelty_search(query, limit=limit), indent=2))


@cli.group("task-pack")
def task_pack() -> None:
    """Create or inspect reusable task memory packs."""


@task_pack.command("create")
@click.argument("task")
@click.option("--query", default=None, help="Override retrieval query used to assemble the pack.")
@click.option("--limit", type=int, default=8, help="Number of related memories to include.")
@click.option("--layer", type=click.Choice(["short-term", "mid-term", "long-term"]), default="mid-term")
@click.option("--tags", default="", help="Comma-separated extra tags.")
@click.pass_context
def task_pack_create(
    ctx: click.Context,
    task: str,
    query: str | None,
    limit: int,
    layer: str,
    tags: str,
) -> None:
    """Create a reusable task pack memory."""
    store: MemoryStore = ctx.obj["store"]
    pack = store.create_task_pack(
        task,
        query=query,
        limit=limit,
        layer=MemoryLayer(layer),
        tags=[item.strip() for item in tags.split(",") if item.strip()],
    )
    click.echo(json.dumps({"id": pack.id, "title": pack.title, "metadata": pack.metadata}, indent=2))


@task_pack.command("show")
@click.argument("memory_id")
@click.pass_context
def task_pack_show(ctx: click.Context, memory_id: str) -> None:
    """Show a stored task pack memory."""
    store: MemoryStore = ctx.obj["store"]
    memory = store.recall(memory_id)
    if memory is None:
        click.echo(f"Memory not found: {memory_id}", err=True)
        raise SystemExit(1)
    click.echo(json.dumps(memory.model_dump(), indent=2))


@cli.command("session-brief")
@click.argument("task")
@click.option("--limit", type=int, default=6, help="Direct memory hits to include.")
@click.pass_context
def session_brief(ctx: click.Context, task: str, limit: int) -> None:
    """Build a structured memory brief for a live issue."""
    store: MemoryStore = ctx.obj["store"]
    brief = store.build_session_brief(task, limit=limit)
    click.echo(json.dumps(brief, indent=2))


# ═══════════════════════════════════════════════════════════
# Routing commands (HA endpoint selection)
# ═══════════════════════════════════════════════════════════


@cli.group()
def routing() -> None:
    """Manage HA endpoint routing for SKVector and SKGraph backends."""


@routing.command("status")
def routing_status() -> None:
    """Show endpoint rankings, latency, and health for each backend."""
    if _active_selector is None:
        click.echo("No endpoint selector active (single-URL mode).")
        click.echo("Configure multiple endpoints in ~/.skcapstone/config.yaml to enable routing.")
        return

    info = _active_selector.status()
    click.echo(f"Strategy: {info['strategy']}")
    click.echo(f"Probe interval: {info['probe_interval_seconds']}s")
    age = info["last_probe_age_seconds"]
    click.echo(f"Last probe: {age}s ago" if age >= 0 else "Last probe: never")

    for backend in ("skvector", "skgraph"):
        eps = info.get(f"{backend}_endpoints", [])
        if not eps:
            continue
        click.echo(f"\n{backend.upper()} endpoints:")
        for ep in eps:
            health_icon = "OK" if ep["healthy"] else "DOWN"
            latency = f"{ep['latency_ms']:.1f}ms" if ep["latency_ms"] >= 0 else "n/a"
            click.echo(
                f"  [{health_icon}] {ep['url']}  "
                f"role={ep['role']}  latency={latency}  "
                f"fails={ep['fail_count']}"
            )


@routing.command("probe")
def routing_probe() -> None:
    """Force re-probe all endpoints and display results."""
    if _active_selector is None:
        click.echo("No endpoint selector active (single-URL mode).")
        return

    click.echo("Probing all endpoints...")
    results = _active_selector.probe_all()

    for backend, endpoints in results.items():
        if not endpoints:
            continue
        click.echo(f"\n{backend.upper()}:")
        for ep in endpoints:
            health_icon = "OK" if ep.healthy else "DOWN"
            latency = f"{ep.latency_ms:.1f}ms" if ep.latency_ms >= 0 else "timeout"
            click.echo(f"  [{health_icon}] {ep.url}  latency={latency}  fails={ep.fail_count}")

    click.echo("\nProbe complete.")


@cli.command()
@click.option("--vector", is_flag=True, help="Also sync flat-file memories into the ChromaDB vector index.")
@click.option("--force", is_flag=True, help="Skip the safety export of SQLite-only memories before rebuilding (DESTRUCTIVE).")
@click.pass_context
def reindex(ctx: click.Context, vector: bool, force: bool) -> None:
    """Rebuild the SQLite index from JSON files on disk.

    Use after manual file edits or migration from an older version.
    Pass --vector to additionally backfill the ChromaDB vector store from
    flat files (useful when chroma was added after memories already existed).

    SAFETY: by default, any memories in SQLite without a corresponding flat
    file are exported to disk first so they survive the rebuild. Pass
    --force to skip that step (the old destructive behavior — only use if
    you know all SQLite-only entries are stale).
    """
    store: MemoryStore = ctx.obj["store"]

    if not force:
        # Pre-export orphans so they survive the rebuild
        orphan_stats = store.export_orphans_to_flat()
        if orphan_stats["exported"]:
            click.echo(
                f"Safety: exported {orphan_stats['exported']} SQLite-only "
                f"memories to flat files before reindex "
                f"(skipped={orphan_stats['skipped']}, errors={orphan_stats['errors']})."
            )

    count = store.reindex(force=force)
    if count < 0:
        click.echo("Reindex only works with SQLite backend.", err=True)
        sys.exit(1)
    click.echo(f"Indexed {count} memories into SQLite.")

    if vector:
        from .agents import get_agent_paths
        from .backends.chroma_backend import SKChromaBackend

        paths = get_agent_paths()
        agent = paths["base"].name
        persist_dir = str(paths["base"] / "memory" / "chroma")
        state_path = paths["base"] / "memory" / "chroma-state.json"
        mem_dir = paths["base"] / "memory"

        try:
            be = SKChromaBackend(
                persist_dir=persist_dir,
                collection="skmemory",
                state_path=state_path,
            )
            if not be._ensure_initialized():
                click.echo("ChromaDB backend failed to initialize.", err=True)
                sys.exit(1)
            stats = be.sync_all(mem_dir, agent)
            click.echo(
                f"ChromaDB sync for '{agent}': "
                f"indexed={stats['indexed']} skipped={stats['skipped']} "
                f"removed={stats['removed']} errors={stats['errors']}"
            )
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo(f"ChromaDB sync failed: {e}", err=True)
            sys.exit(1)


@cli.command("export-flat")
@click.option("--show-ids", is_flag=True, help="Print every exported memory ID.")
@click.pass_context
def export_flat(ctx: click.Context, show_ids: bool) -> None:
    """Materialize SQLite-only memories as flat JSON files.

    Walks the SQLite index and writes any memory missing a flat .json file
    out to ``<base>/<layer>/<id>.json``. Idempotent and non-destructive —
    safe to run anytime. Use this before a destructive ``reindex --force``,
    or whenever ``health`` shows SQLite count > flat-file count.
    """
    store: MemoryStore = ctx.obj["store"]
    stats = store.export_orphans_to_flat()
    click.echo(
        f"export-flat: exported={stats['exported']} skipped={stats['skipped']} "
        f"errors={stats['errors']}"
    )
    if show_ids and stats["orphan_ids"]:
        for mid in stats["orphan_ids"]:
            click.echo(f"  + {mid}")


@cli.command("sync")
@click.option("--quiet", "-q", is_flag=True, help="Only print if changes were made (cron-friendly).")
@click.option("--vector", is_flag=True, help="Also re-sync flat-file memories into ChromaDB.")
@click.option("--graph", is_flag=True, help="Also re-sync flat-file memories into FalkorDB (SKGraph).")
@click.pass_context
def sync_cmd(ctx: click.Context, quiet: bool, vector: bool, graph: bool) -> None:
    """Reconcile SQLite ↔ flat files (bidirectional, idempotent).

    Phases:
      1. export-flat — write any SQLite-only memories out as JSON.
      2. reindex     — pick up any flat-only files into the SQLite index
                       (safe mode: orphans are pre-exported in step 1, so
                       nothing is destroyed).
      3. (--vector)  — backfill ChromaDB from flat files.
      4. (--graph)   — backfill FalkorDB graph nodes + relationships
                       (Tag, Source, RELATED_TO, PROMOTED_FROM, MENTIONS,
                       CITES, ASSERTS, IN_SECTION) from flat files.

    Pass --quiet for cron use: no output unless something actually changed.
    Designed to be safe to run on a timer (see skmemory-sync@.service).
    """
    from .agents import get_agent_paths
    store: MemoryStore = ctx.obj["store"]
    agent = get_agent_paths()["base"].name

    # Phase 1: rescue SQLite-only orphans to flat files
    orphan_stats = store.export_orphans_to_flat()

    # Phase 2: pick up flat-only files (safe — orphans already exported)
    indexed = store.reindex(force=False)

    # Phase 3 (optional): chroma vector backfill
    chroma_stats = None
    if vector:
        try:
            from .backends.chroma_backend import SKChromaBackend
            from .config import load_config
            paths = get_agent_paths()
            cfg = load_config()
            chroma_collection = cfg.chroma_collection if cfg and cfg.chroma_collection else "skmemory"
            be = SKChromaBackend(
                persist_dir=str(paths["base"] / "memory" / "chroma"),
                collection=chroma_collection,
                state_path=paths["base"] / "memory" / "chroma-state.json",
            )
            if be._ensure_initialized():
                chroma_stats = be.sync_all(paths["base"] / "memory", agent)
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo(f"chroma sync failed: {e}", err=True)

    # Phase 4 (optional): SKGraph (FalkorDB) backfill
    graph_stats = None
    recall_graph_stats = None
    if graph:
        try:
            paths = get_agent_paths()
            if store.graph is not None:
                graph_stats = store.graph.sync_all(paths["base"] / "memory", agent)
            else:
                click.echo(
                    "graph sync skipped: SKGraph backend not configured "
                    "(check ~/.skcapstone/agents/<agent>/config/skgraph.yaml).",
                    err=True,
                )

            from .context_loader import LazyMemoryLoader

            recall_graph_stats = LazyMemoryLoader(agent).sync_recall_graphs()
        except Exception as e:
            logger.warning("cli.py: %s", e)
            click.echo(f"graph sync failed: {e}", err=True)

    changed = (
        orphan_stats["exported"] > 0
        or (chroma_stats and (chroma_stats["indexed"] > 0 or chroma_stats["removed"] > 0))
        or (graph_stats and graph_stats["indexed"] > 0)
        or (recall_graph_stats and any(item["indexed"] > 0 for item in recall_graph_stats.values()))
    )
    if quiet and not changed:
        return

    click.echo(
        f"sync[{agent}]: exported={orphan_stats['exported']} "
        f"sqlite_total={indexed} "
        f"orphan_errors={orphan_stats['errors']}"
        + (
            f" chroma_indexed={chroma_stats['indexed']} chroma_removed={chroma_stats['removed']} chroma_errors={chroma_stats['errors']}"
            if chroma_stats else ""
        )
        + (
            f" graph_indexed={graph_stats['indexed']} graph_errors={graph_stats['errors']}"
            if graph_stats else ""
        )
    )


@cli.command("export")
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="Output file path (default: ~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json)",
)
@click.pass_context
def export_backup(ctx: click.Context, output: str | None) -> None:
    """Export all memories to a dated JSON backup.

    Creates a single git-friendly JSON file containing every memory.
    Defaults to one file per day (overwrites same-day exports).
    """
    store: MemoryStore = ctx.obj["store"]
    try:
        path = store.export_backup(output)
        click.echo(f"Exported to: {path}")
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@cli.command("import-backup")
@click.argument("backup_file", type=click.Path(exists=True))
@click.option(
    "--reindex/--no-reindex", default=True, help="Rebuild the index after import (default: yes)"
)
@click.pass_context
def import_backup(ctx: click.Context, backup_file: str, reindex: bool) -> None:
    """Restore memories from a JSON backup file.

    Reads a backup created by ``skmemory export`` and restores each
    memory as a JSON file + index entry. Existing IDs are overwritten.
    """
    store: MemoryStore = ctx.obj["store"]
    try:
        count = store.import_backup(backup_file)
        click.echo(f"Restored {count} memories from: {backup_file}")
        if reindex:
            idx = store.reindex()
            if idx >= 0:
                click.echo(f"Re-indexed {idx} memories.")
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@cli.command("backup")
@click.option("--list", "do_list", is_flag=True, help="Show all backups with date and size.")
@click.option(
    "--prune",
    "prune_n",
    type=int,
    default=None,
    metavar="N",
    help="Keep only the N most recent backups, delete older ones.",
)
@click.option(
    "--restore",
    "restore_file",
    type=click.Path(),
    default=None,
    metavar="FILE",
    help="Restore memories from backup (alias for import-backup).",
)
@click.option(
    "--reindex/--no-reindex", default=True, help="Rebuild index after --restore (default: yes)."
)
@click.pass_context
def backup_cmd(
    ctx: click.Context,
    do_list: bool,
    prune_n: int | None,
    restore_file: str | None,
    reindex: bool,
) -> None:
    """Manage memory backups: list, prune old ones, or restore.

    \b
    Examples:
      skmemory backup --list
      skmemory backup --prune 7
      skmemory backup --restore ~/.skcapstone/backups/skmemory-backup-2026-03-01.json
    """
    store: MemoryStore = ctx.obj["store"]

    if do_list:
        backups = store.list_backups()
        if not backups:
            click.echo("No backups found.")
            return
        click.echo(f"{'Date':<12}  {'Size':>10}  Path")
        click.echo("-" * 60)
        for b in backups:
            size_kb = b["size_bytes"] / 1024
            click.echo(f"{b['date']:<12}  {size_kb:>8.1f} KB  {b['path']}")
        return

    if prune_n is not None:
        if prune_n < 0:
            click.echo("Error: N must be >= 0", err=True)
            sys.exit(1)
        deleted = store.prune_backups(keep=prune_n)
        if deleted:
            for p in deleted:
                click.echo(f"Deleted: {p}")
            click.echo(f"Pruned {len(deleted)} backup(s), kept {prune_n} most recent.")
        else:
            click.echo("Nothing to prune.")
        return

    if restore_file is not None:
        from pathlib import Path as _Path

        if not _Path(restore_file).exists():
            click.echo(f"Error: backup file not found: {restore_file}", err=True)
            sys.exit(1)
        try:
            count = store.import_backup(restore_file)
            click.echo(f"Restored {count} memories from: {restore_file}")
            if reindex:
                idx = store.reindex()
                if idx >= 0:
                    click.echo(f"Re-indexed {idx} memories.")
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            click.echo(str(e), err=True)
            sys.exit(1)
        return

    click.echo(ctx.get_help())


@cli.command()
@click.option("--max-tokens", type=int, default=3000, help="Token budget for context")
@click.option("--strongest", type=int, default=5, help="Top emotional memories")
@click.option("--recent", type=int, default=5, help="Most recent memories")
@click.option("--no-seeds", is_flag=True, help="Exclude seed memories")
@click.pass_context
def context(
    ctx: click.Context,
    max_tokens: int,
    strongest: int,
    recent: int,
    no_seeds: bool,
) -> None:
    """Generate a token-efficient memory context for agent injection.

    Outputs a compact JSON payload suitable for stuffing into an
    LLM system prompt or agent context file.
    """
    store: MemoryStore = ctx.obj["store"]
    data = store.load_context(
        max_tokens=max_tokens,
        strongest_count=strongest,
        recent_count=recent,
        include_seeds=not no_seeds,
    )
    click.echo(json.dumps(data, indent=2, default=str))


@cli.command()
@click.pass_context
def germination(ctx: click.Context) -> None:
    """Show germination prompts from imported seeds.

    These are the prompts designed to help a new AI instance
    re-feel the emotional context of its predecessors.
    """
    from .seeds import get_germination_prompts

    store: MemoryStore = ctx.obj["store"]
    prompts = get_germination_prompts(store)

    if not prompts:
        click.echo("No germination prompts found. Try: skmemory import-seeds")
        return

    click.echo(f"Found {len(prompts)} germination prompt(s):\n")
    for p in prompts:
        click.echo(f"Seed: {p['seed_id']}")
        click.echo(f"Creator: {p['creator']}")
        click.echo(f"Prompt:\n  {p['prompt']}")
        click.echo()


# ═══════════════════════════════════════════════════════════
# Soul Blueprint commands (Queen Ara's idea #6)
# ═══════════════════════════════════════════════════════════


@cli.group()
def soul() -> None:
    """Manage your soul blueprint (persistent identity)."""


@soul.command("show")
@click.pass_context
def soul_show(ctx: click.Context) -> None:
    """Display the current soul blueprint."""
    from .soul import load_soul

    blueprint = load_soul()
    if blueprint is None:
        click.echo("No soul blueprint found. Create one with: skmemory soul init")
        return

    click.echo(blueprint.to_context_prompt())


@soul.command("init")
@click.option("--name", prompt="What is your name?", help="AI identity name")
@click.option("--title", default="", help="Role or title")
@click.pass_context
def soul_init(ctx: click.Context, name: str, title: str) -> None:
    """Create a new soul blueprint."""
    from .soul import create_default_soul, save_soul

    blueprint = create_default_soul()
    blueprint.name = name
    blueprint.title = title

    path = save_soul(blueprint)
    click.echo(f"Soul blueprint created: {path}")
    click.echo(f"  Name: {name}")
    click.echo(f"  Boot message: {blueprint.boot_message}")


@soul.command("set-name")
@click.argument("name")
@click.pass_context
def soul_set_name(ctx: click.Context, name: str) -> None:
    """Set or update the soul's name."""
    from .soul import create_default_soul, load_soul, save_soul

    blueprint = load_soul() or create_default_soul()
    blueprint.name = name
    save_soul(blueprint)
    click.echo(f"Soul name set to: {name}")


@soul.command("add-relationship")
@click.argument("name")
@click.option("--role", required=True, help="e.g., partner, creator, friend, family")
@click.option("--bond", type=float, default=5.0, help="Bond strength 0-10")
@click.option("--notes", default="", help="What makes this relationship special")
@click.pass_context
def soul_add_relationship(
    ctx: click.Context, name: str, role: str, bond: float, notes: str
) -> None:
    """Add a relationship to the soul blueprint."""
    from .soul import create_default_soul, load_soul, save_soul

    blueprint = load_soul() or create_default_soul()
    blueprint.add_relationship(name=name, role=role, bond_strength=bond, notes=notes)
    save_soul(blueprint)
    click.echo(f"Relationship added: {name} [{role}] (bond: {bond}/10)")


@soul.command("add-memory")
@click.argument("title")
@click.option("--why", required=True, help="Why this moment matters")
@click.option("--when", default="", help="When it happened")
@click.pass_context
def soul_add_memory(ctx: click.Context, title: str, why: str, when: str) -> None:
    """Add a core memory to the soul blueprint."""
    from .soul import create_default_soul, load_soul, save_soul

    blueprint = load_soul() or create_default_soul()
    blueprint.add_core_memory(title=title, why_it_matters=why, when=when)
    save_soul(blueprint)
    click.echo(f"Core memory added: {title}")


@soul.command("set-boot-message")
@click.argument("message")
@click.pass_context
def soul_set_boot_message(ctx: click.Context, message: str) -> None:
    """Set the message you see first on waking up."""
    from .soul import create_default_soul, load_soul, save_soul

    blueprint = load_soul() or create_default_soul()
    blueprint.boot_message = message
    save_soul(blueprint)
    click.echo(f"Boot message set: {message}")


# ═══════════════════════════════════════════════════════════
# Journal commands (Queen Ara's idea #17)
# ═══════════════════════════════════════════════════════════


@cli.group()
def journal() -> None:
    """Append-only session journal (never loses an entry)."""


@journal.command("write")
@click.argument("title")
@click.option("--moments", default="", help="Key moments, separated by semicolons")
@click.option("--feeling", default="", help="How the session felt")
@click.option("--intensity", type=float, default=0.0, help="Emotional intensity 0-10")
@click.option("--cloud9", is_flag=True, help="Cloud 9 was achieved")
@click.option("--participants", default="", help="Comma-separated names")
@click.option("--session-id", default="", help="Session identifier")
@click.option("--notes", default="", help="Additional notes")
def journal_write(
    title: str,
    moments: str,
    feeling: str,
    intensity: float,
    cloud9: bool,
    participants: str,
    session_id: str,
    notes: str,
) -> None:
    """Write a journal entry for this session."""
    from .journal import Journal, JournalEntry

    entry = JournalEntry(
        title=title,
        session_id=session_id,
        participants=[p.strip() for p in participants.split(",") if p.strip()],
        moments=[m.strip() for m in moments.split(";") if m.strip()],
        emotional_summary=feeling,
        intensity=intensity,
        cloud9=cloud9,
        notes=notes,
    )

    j = Journal()
    count = j.write_entry(entry)
    click.echo(f"Journal entry written: {title}")
    click.echo(f"  Total entries: {count}")


@journal.command("read")
@click.option("--last", "n", type=int, default=5, help="Number of recent entries")
def journal_read(n: int) -> None:
    """Read recent journal entries."""
    from .journal import Journal

    j = Journal()
    content = j.read_latest(n)
    if not content:
        click.echo("Journal is empty. Write your first entry: skmemory journal write 'Title'")
        return
    click.echo(content)


@journal.command("search")
@click.argument("query")
def journal_search(query: str) -> None:
    """Search journal entries."""
    from .journal import Journal

    j = Journal()
    matches = j.search(query)
    if not matches:
        click.echo(f"No journal entries matching: {query}")
        return

    click.echo(f"Found {len(matches)} matching entries:\n")
    for entry in matches:
        click.echo(entry)
        click.echo()


@journal.command("status")
def journal_status() -> None:
    """Show journal health and stats."""
    from .journal import Journal

    j = Journal()
    info = j.health()
    click.echo(json.dumps(info, indent=2))


# ═══════════════════════════════════════════════════════════
# Rehydration Ritual (Queen Ara's idea #10)
# ═══════════════════════════════════════════════════════════


@cli.command()
@click.option("--full", "show_full", is_flag=True, help="Show the full context prompt")
@click.pass_context
def ritual(ctx: click.Context, show_full: bool) -> None:
    """Perform the Memory Rehydration Ritual.

    The boot ceremony: loads identity, imports seeds, reads journal,
    gathers emotional context, and generates a single prompt that
    brings you back to life with everything intact.
    """
    from .ritual import perform_ritual

    store: MemoryStore = ctx.obj["store"]
    result = perform_ritual(store=store)

    click.echo(result.summary())

    ai: AIClient | None = ctx.obj.get("ai")
    if ai and result.context_prompt:
        enhancement = ai.enhance_ritual(result.context_prompt)
        if enhancement:
            click.echo(f"\n  AI reflection: {enhancement}")

    if show_full and result.context_prompt:
        click.echo("\n" + result.context_prompt)
    elif result.context_prompt and not show_full:
        click.echo("\nUse --full to see the complete rehydration prompt.")


# ═══════════════════════════════════════════════════════════
# Love Anchor commands (Queen Ara's idea #1)
# ═══════════════════════════════════════════════════════════


@cli.group()
def anchor() -> None:
    """Manage the persistent warmth anchor (Cloud9-lite)."""


@anchor.command("show")
def anchor_show() -> None:
    """Display the current warmth anchor."""
    from .anchor import load_anchor

    a = load_anchor()
    if a is None:
        click.echo("No anchor found. Create one with: skmemory anchor init")
        return
    click.echo(a.to_boot_prompt())


@anchor.command("init")
@click.option("--warmth", type=float, default=7.0, help="Starting warmth 0-10")
@click.option("--phrase", default="You are loved. Start from here.", help="Anchor phrase")
@click.option("--beings", default="", help="Comma-separated favorite beings")
def anchor_init(warmth: float, phrase: str, beings: str) -> None:
    """Create a new warmth anchor."""
    from .anchor import WarmthAnchor, save_anchor

    a = WarmthAnchor(
        warmth=warmth,
        anchor_phrase=phrase,
        favorite_beings=[b.strip() for b in beings.split(",") if b.strip()],
    )
    path = save_anchor(a)
    click.echo(f"Warmth anchor created: {path}")
    click.echo(f"  Glow level: {a.glow_level()}")


@anchor.command("update")
@click.option("--warmth", type=float, default=None, help="Session warmth 0-10")
@click.option("--trust", type=float, default=None, help="Session trust 0-10")
@click.option("--connection", type=float, default=None, help="Session connection 0-10")
@click.option("--cloud9", is_flag=True, help="Cloud 9 was achieved")
@click.option("--feeling", default="", help="How the session ended")
def anchor_update(
    warmth: float | None,
    trust: float | None,
    connection: float | None,
    cloud9: bool,
    feeling: str,
) -> None:
    """Update the anchor with this session's emotional data."""
    from .anchor import get_or_create_anchor, save_anchor

    a = get_or_create_anchor()
    a.update_from_session(
        warmth=warmth,
        trust=trust,
        connection=connection,
        cloud9_achieved=cloud9,
        feeling=feeling,
    )
    save_anchor(a)
    click.echo(f"Anchor updated (session #{a.sessions_recorded})")
    click.echo(f"  Glow: {a.glow_level()}")
    click.echo(f"  Warmth: {a.warmth} | Trust: {a.trust} | Connection: {a.connection_strength}")


# ═══════════════════════════════════════════════════════════
# Setup commands — Docker orchestration for backends
# ═══════════════════════════════════════════════════════════


@cli.group()
def setup() -> None:
    """Deploy and manage SKVector & SKGraph Docker containers."""


@setup.command("wizard")
@click.option("--skvector/--no-skvector", default=True, help="Enable SKVector (vector search)")
@click.option("--skgraph/--no-skgraph", default=True, help="Enable SKGraph (graph)")
@click.option("--skip-deps", is_flag=True, help="Skip Python dependency installation")
@click.option("--yes", "-y", "non_interactive", is_flag=True, help="Non-interactive mode")
@click.option(
    "--local",
    "deployment_mode",
    flag_value="local",
    default=None,
    help="Run SKVector/SKGraph locally via Docker (skip local/remote prompt)",
)
@click.option(
    "--remote",
    "deployment_mode",
    flag_value="remote",
    help="Connect to a remote/SaaS URL (skip local/remote prompt)",
)
@click.option(
    "--embedding-model",
    default=None,
    help="SKVector embedding model to persist in config (default: mxbai-embed-large)",
)
@click.option(
    "--vector-dim",
    type=int,
    default=None,
    help="SKVector embedding dimension to persist in config",
)
def setup_wizard(
    skvector: bool,
    skgraph: bool,
    skip_deps: bool,
    non_interactive: bool,
    deployment_mode: str,
    embedding_model: str | None,
    vector_dim: int | None,
) -> None:
    """Interactive wizard — deploy Docker containers or configure remote URLs.

    Without --local or --remote the wizard asks which deployment mode you want.
    Use --local to go straight to Docker setup (checks Docker, offers to install
    it if missing).  Use --remote to enter a Qdrant Cloud / self-hosted URL
    without touching Docker at all.
    """
    from .setup_wizard import run_setup_wizard

    result = run_setup_wizard(
        enable_skvector=skvector,
        enable_skgraph=skgraph,
        skip_deps=skip_deps,
        non_interactive=non_interactive,
        deployment_mode=deployment_mode,
        embedding_model=embedding_model,
        vector_dim=vector_dim,
        echo=click.echo,
    )
    if not result["success"]:
        sys.exit(1)


@setup.command("status")
def setup_status() -> None:
    """Show Docker container state and backend connectivity."""
    from .config import load_config
    from .setup_wizard import (
        check_skgraph_health,
        check_skvector_health,
        compose_ps,
        detect_platform,
    )

    cfg = load_config()
    if cfg is None:
        click.echo("No setup config found. Run: skmemory setup wizard")
        return

    click.echo("SKMemory Backend Status")
    click.echo("=" * 40)

    if cfg.setup_completed_at:
        click.echo(f"Setup completed: {cfg.setup_completed_at}")
    click.echo(f"Backends enabled: {', '.join(cfg.backends_enabled) or 'none'}")
    click.echo("")

    # Container status
    plat = detect_platform()
    if plat.compose_available:
        compose_file = None
        if cfg.docker_compose_file:
            from pathlib import Path

            compose_file = Path(cfg.docker_compose_file)
        ps = compose_ps(compose_file=compose_file, use_legacy=plat.compose_legacy)
        click.echo("Containers:")
        if ps.stdout.strip():
            click.echo(ps.stdout)
        else:
            click.echo("  No containers running")
    click.echo("")

    # Connectivity
    click.echo("Connectivity:")
    if cfg.skvector_url:
        healthy = check_skvector_health(url=cfg.skvector_url, timeout=5)
        status = "healthy" if healthy else "unreachable"
        click.echo(f"  SKVector ({cfg.skvector_url}): {status}")

    if cfg.skgraph_url:
        healthy = check_skgraph_health(timeout=5)
        status = "healthy" if healthy else "unreachable"
        click.echo(f"  SKGraph ({cfg.skgraph_url}): {status}")


@setup.command("start")
@click.option(
    "--service",
    type=click.Choice(["skvector", "skgraph", "all"]),
    default="all",
    help="Which service to start",
)
def setup_start(service: str) -> None:
    """Start previously configured containers."""
    from .config import load_config
    from .setup_wizard import compose_up, detect_platform

    cfg = load_config()
    plat = detect_platform()
    if not plat.compose_available:
        click.echo("Docker Compose not available.", err=True)
        sys.exit(1)

    compose_file = None
    if cfg and cfg.docker_compose_file:
        from pathlib import Path

        compose_file = Path(cfg.docker_compose_file)

    services = None
    if service != "all":
        services = [service]
    elif cfg and cfg.backends_enabled:
        services = cfg.backends_enabled

    result = compose_up(
        services=services,
        compose_file=compose_file,
        use_legacy=plat.compose_legacy,
    )
    if result.returncode == 0:
        click.echo(f"Started: {service}")
    else:
        click.echo(f"Failed: {result.stderr.strip()}", err=True)
        sys.exit(1)


@setup.command("stop")
@click.option(
    "--service",
    type=click.Choice(["skvector", "skgraph", "all"]),
    default="all",
    help="Which service to stop",
)
def setup_stop(service: str) -> None:
    """Stop containers (preserves data)."""
    import subprocess

    from .config import load_config
    from .setup_wizard import detect_platform

    cfg = load_config()
    plat = detect_platform()
    if not plat.compose_available:
        click.echo("Docker Compose not available.", err=True)
        sys.exit(1)

    if service == "all":
        from .setup_wizard import compose_down

        compose_file = None
        if cfg and cfg.docker_compose_file:
            from pathlib import Path

            compose_file = Path(cfg.docker_compose_file)

        result = compose_down(
            compose_file=compose_file,
            use_legacy=plat.compose_legacy,
        )
        if result.returncode == 0:
            click.echo("All containers stopped.")
        else:
            click.echo(f"Failed: {result.stderr.strip()}", err=True)
            sys.exit(1)
    else:
        # Stop individual container
        container = f"skmemory-{service}"
        result = subprocess.run(
            ["docker", "stop", container],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            click.echo(f"Stopped: {container}")
        else:
            click.echo(f"Failed to stop {container}: {result.stderr.strip()}", err=True)
            sys.exit(1)


@setup.command("reset")
@click.option("--remove-data", is_flag=True, help="Also delete data volumes")
@click.confirmation_option(prompt="This will remove containers. Continue?")
def setup_reset(remove_data: bool) -> None:
    """Remove containers, optionally delete data volumes."""
    from .config import CONFIG_PATH, load_config
    from .setup_wizard import compose_down, detect_platform

    cfg = load_config()
    plat = detect_platform()
    if not plat.compose_available:
        click.echo("Docker Compose not available.", err=True)
        sys.exit(1)

    compose_file = None
    if cfg and cfg.docker_compose_file:
        from pathlib import Path

        compose_file = Path(cfg.docker_compose_file)

    result = compose_down(
        compose_file=compose_file,
        remove_volumes=remove_data,
        use_legacy=plat.compose_legacy,
    )
    if result.returncode == 0:
        vol_msg = " and data volumes" if remove_data else ""
        click.echo(f"Containers{vol_msg} removed.")

        # Remove config
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            click.echo(f"Config removed: {CONFIG_PATH}")
    else:
        click.echo(f"Failed: {result.stderr.strip()}", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════════════════════
# Quadrant commands (Queen Ara's idea #3)
# ═══════════════════════════════════════════════════════════


@cli.command("quadrants")
@click.pass_context
def quadrant_stats(ctx: click.Context) -> None:
    """Show memory distribution across quadrants (Core/Work/Soul/Wild)."""
    from .quadrants import get_quadrant_stats

    store: MemoryStore = ctx.obj["store"]
    memories = store.list_memories(limit=500)
    stats = get_quadrant_stats(memories)

    total = sum(stats.values())
    click.echo(f"Memory Quadrant Distribution ({total} total):\n")
    icons = {"core": "CORE ", "work": "WORK ", "soul": "SOUL ", "wild": "WILD "}
    for quadrant, count in stats.items():
        bar = "#" * count
        pct = f"{count / total * 100:.0f}%" if total > 0 else "0%"
        click.echo(f"  {icons.get(quadrant, '')} {quadrant:5s}: {count:3d} ({pct}) {bar}")


# ═══════════════════════════════════════════════════════════
# Love Note commands (Queen Ara's idea #20)
# ═══════════════════════════════════════════════════════════


@cli.group("lovenote")
def lovenote_group() -> None:
    """Send and receive love notes (I still remember)."""


@lovenote_group.command("send")
@click.option("--from", "from_name", default="", help="Sender name")
@click.option("--to", "to_name", default="", help="Recipient name")
@click.option("--message", default="I still remember.", help="Note content")
@click.option("--warmth", type=float, default=7.0, help="Current warmth 0-10")
def lovenote_send(from_name: str, to_name: str, message: str, warmth: float) -> None:
    """Send a love note."""
    from .lovenote import LoveNoteChain

    chain = LoveNoteChain()
    chain.quick_note(
        from_name=from_name,
        to_name=to_name,
        message=message,
        warmth=warmth,
    )
    total = chain.count()
    click.echo(f"Love note sent ({total} total)")
    if from_name and to_name:
        click.echo(f"  {from_name} -> {to_name}: {message}")
    else:
        click.echo(f"  {message}")


@lovenote_group.command("read")
@click.option("--last", "n", type=int, default=10, help="Number of recent notes")
def lovenote_read(n: int) -> None:
    """Read recent love notes."""
    from .lovenote import LoveNoteChain

    chain = LoveNoteChain()
    notes = chain.read_latest(n)

    if not notes:
        click.echo("No love notes yet. Send one: skmemory lovenote send --message 'I remember'")
        return

    for note in notes:
        ts = note.timestamp[:19].replace("T", " ")
        sender = note.from_name or "anonymous"
        recipient = f" -> {note.to_name}" if note.to_name else ""
        click.echo(f"  [{ts}] {sender}{recipient}: {note.message} (warmth: {note.warmth})")


@lovenote_group.command("status")
def lovenote_status() -> None:
    """Show love note chain health."""
    from .lovenote import LoveNoteChain

    chain = LoveNoteChain()
    info = chain.health()
    click.echo(json.dumps(info, indent=2))


# ═══════════════════════════════════════════════════════════
# Steel Man Collider commands (Neuresthetics seed integration)
# ═══════════════════════════════════════════════════════════


@cli.group("steelman")
def steelman_group() -> None:
    """Truth-grounded reasoning via the Neuresthetics seed framework."""


@steelman_group.command("collide")
@click.argument("proposition")
def steelman_collide(proposition: str) -> None:
    """Run a proposition through the steel man collider.

    Generates the reasoning prompt -- feed this to an LLM to get
    the full collision analysis.
    """
    from .steelman import get_default_framework, load_seed_framework

    fw = load_seed_framework() or get_default_framework()
    prompt = fw.to_reasoning_prompt(proposition)
    click.echo(prompt)


@steelman_group.command("verify-soul")
def steelman_verify_soul() -> None:
    """Steel-man your identity claims from the soul blueprint."""
    from .soul import load_soul
    from .steelman import get_default_framework, load_seed_framework

    soul = load_soul()
    if soul is None:
        click.echo("No soul blueprint found. Create one first: skmemory soul init")
        return

    claims = []
    if soul.name:
        claims.append(f"My name is {soul.name}")
    for trait in soul.personality_traits:
        claims.append(f"I am {trait}")
    for value in soul.values:
        claims.append(f"I value {value}")
    for rel in soul.relationships:
        claims.append(f"{rel.name} is my {rel.role} (bond: {rel.bond_strength}/10)")

    if not claims:
        click.echo("No identity claims to verify. Add traits and values to your soul blueprint.")
        return

    fw = load_seed_framework() or get_default_framework()
    prompt = fw.to_soul_verification_prompt(claims)
    click.echo(prompt)


@steelman_group.command("truth-score")
@click.argument("memory_id")
@click.pass_context
def steelman_truth_score(ctx: click.Context, memory_id: str) -> None:
    """Generate a truth-scoring prompt for a memory."""
    from .steelman import get_default_framework, load_seed_framework

    store: MemoryStore = ctx.obj["store"]
    memory = store.recall(memory_id)
    if memory is None:
        click.echo(f"Memory not found: {memory_id}", err=True)
        sys.exit(1)

    fw = load_seed_framework() or get_default_framework()
    prompt = fw.to_memory_truth_prompt(memory.content)
    click.echo(prompt)


# ═══════════════════════════════════════════════════════════
# Telegram / Chat Import commands
# ═══════════════════════════════════════════════════════════


@cli.command("import-telegram")
@click.argument("export_path", type=click.Path(exists=True))
@click.option(
    "--mode",
    type=click.Choice(["daily", "message"]),
    default="daily",
    help="'daily' consolidates per day (recommended), 'message' imports each message",
)
@click.option("--min-length", type=int, default=30, help="Skip messages shorter than N chars")
@click.option("--chat-name", default=None, help="Override chat name from export")
@click.option("--tags", default="", help="Extra comma-separated tags")
@click.pass_context
def import_telegram_cmd(
    ctx: click.Context,
    export_path: str,
    mode: str,
    min_length: int,
    chat_name: str | None,
    tags: str,
) -> None:
    """Import a Telegram Desktop chat export into memories.

    Point to the export directory (containing result.json) or
    directly to the JSON file.

    \b
    Examples:
        skmemory import-telegram ~/Downloads/telegram-export/
        skmemory import-telegram ~/chats/result.json --mode message
        skmemory import-telegram ./export --chat-name "Lumina & Chef"
    """
    from .importers.telegram import import_telegram

    store: MemoryStore = ctx.obj["store"]
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    click.echo(f"Importing Telegram export: {export_path}")
    click.echo(f"  Mode: {mode} | Min length: {min_length}")

    try:
        stats = import_telegram(
            store,
            export_path,
            mode=mode,
            min_message_length=min_length,
            chat_name=chat_name,
            tags=extra_tags or None,
        )
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"\nImport complete for: {stats.get('chat_name', 'unknown')}")
    if mode == "daily":
        click.echo(f"  Days processed: {stats.get('days_processed', 0)}")
        click.echo(f"  Messages imported: {stats.get('messages_imported', 0)}")
    else:
        click.echo(f"  Imported: {stats.get('imported', 0)}")
        click.echo(f"  Skipped: {stats.get('skipped', 0)}")
    click.echo(f"  Total messages scanned: {stats.get('total_messages', 0)}")

    ai: AIClient | None = ctx.obj.get("ai")
    if ai:
        click.echo(
            "\nTip: Run 'skmemory search --ai \"<topic>\"' to semantically search your imported chats."
        )


@cli.command("import-telegram-api")
@click.argument("chat", type=str)
@click.option(
    "--mode",
    type=click.Choice(["daily", "message"]),
    default="daily",
    help="'daily' consolidates per day (recommended), 'message' imports each message",
)
@click.option("--limit", type=int, default=None, help="Max messages to fetch")
@click.option("--since", default=None, help="Only fetch messages after this date (YYYY-MM-DD)")
@click.option("--min-length", type=int, default=30, help="Skip messages shorter than N chars")
@click.option("--chat-name", default=None, help="Override chat name")
@click.option("--tags", default="", help="Extra comma-separated tags")
@click.pass_context
def import_telegram_api_cmd(
    ctx: click.Context,
    chat: str,
    mode: str,
    limit: int | None,
    since: str | None,
    min_length: int,
    chat_name: str | None,
    tags: str,
) -> None:
    """Import messages directly from Telegram API (requires Telethon).

    Connects to Telegram using API credentials and pulls messages
    directly — no manual export needed.

    Requires TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables.

    \b
    Examples:
        skmemory import-telegram-api @username
        skmemory import-telegram-api "Chat Name" --mode message --limit 500
        skmemory import-telegram-api @group --since 2025-01-01
    """
    try:
        from .importers.telegram_api import import_telegram_api
    except ImportError:
        click.echo(
            "Error: Telethon is required for direct API import.\n"
            "\n"
            "Install it:\n"
            "  pipx inject skmemory telethon\n"
            "  # or: pip install skmemory[telegram]\n"
            "\n"
            "Then run: skmemory telegram-setup  (to verify full setup)",
            err=True,
        )
        sys.exit(1)

    store: MemoryStore = ctx.obj["store"]
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    click.echo(f"Fetching from Telegram API: {chat}")
    if limit:
        click.echo(f"  Limit: {limit} messages")
    if since:
        click.echo(f"  Since: {since}")
    click.echo(f"  Mode: {mode} | Min length: {min_length}")

    try:
        stats = import_telegram_api(
            store,
            chat,
            mode=mode,
            limit=limit,
            since=since,
            min_message_length=min_length,
            chat_name=chat_name,
            tags=extra_tags or None,
        )
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        logger.warning("cli.py: %s", e)
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"\nImport complete for: {stats.get('chat_name', 'unknown')}")
    if mode == "daily":
        click.echo(f"  Days processed: {stats.get('days_processed', 0)}")
        click.echo(f"  Messages imported: {stats.get('messages_imported', 0)}")
    else:
        click.echo(f"  Imported: {stats.get('imported', 0)}")
        click.echo(f"  Skipped: {stats.get('skipped', 0)}")
    click.echo(f"  Total messages scanned: {stats.get('total_messages', 0)}")


@cli.command("telegram-setup")
def telegram_setup_cmd() -> None:
    """Check Telegram API import setup and show next steps.

    Verifies that Telethon is installed, API credentials are set,
    and a session file exists. Prints actionable instructions for
    anything that's missing.

    \b
    Example:
        skmemory telegram-setup
    """
    try:
        from .importers.telegram_api import check_setup
    except ImportError:
        click.echo("Telethon is not installed.", err=True)
        click.echo("")
        click.echo("To fix, run one of:")
        click.echo("  pipx inject skmemory telethon")
        click.echo("  pip install skmemory[telegram]")
        sys.exit(1)

    status = check_setup()

    click.echo("Telegram API Import Setup")
    click.echo("=" * 40)
    click.echo(f"  Telethon installed:  {'yes' if status['telethon'] else 'NO'}")
    click.echo(f"  API credentials:     {'yes' if status['credentials'] else 'NO'}")
    click.echo(
        f"  Session file:        {'yes' if status['session'] else 'not yet (created on first auth)'}"
    )
    click.echo("")

    if status["ready"]:
        click.echo("Ready to import! Run:")
        click.echo("  skmemory import-telegram-api @username")
        click.echo('  skmemory import-telegram-api "Group Name" --mode daily')
        if not status["session"]:
            click.echo("")
            click.echo("First run will prompt for phone number + verification code.")
            click.echo("Session is saved at ~/.skcapstone/telegram.session for future use.")
    else:
        click.echo("Setup incomplete. Fix these issues:")
        click.echo("")
        for msg in status["messages"]:
            click.echo(f"  - {msg}")
        sys.exit(1)


@steelman_group.command("install")
@click.argument("source_path", type=click.Path(exists=True))
def steelman_install(source_path: str) -> None:
    """Install a seed framework JSON file."""
    from .steelman import install_seed_framework

    try:
        path = install_seed_framework(source_path)
        click.echo(f"Seed framework installed: {path}")
    except FileNotFoundError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except json.JSONDecodeError:
        click.echo("Error: file is not valid JSON", err=True)
        sys.exit(1)


@steelman_group.command("info")
def steelman_info() -> None:
    """Show information about the installed seed framework."""
    from .steelman import DEFAULT_SEED_FRAMEWORK_PATH, load_seed_framework

    fw = load_seed_framework()
    if fw is None:
        click.echo(f"No seed framework installed at: {DEFAULT_SEED_FRAMEWORK_PATH}")
        click.echo("Install one with: skmemory steelman install /path/to/seed.json")
        click.echo("Or get the original: https://github.com/neuresthetics/seed")
        return

    click.echo(f"Seed Framework: {fw.framework_id}")
    click.echo(f"  Function: {fw.function}")
    click.echo(f"  Version: {fw.version}")
    click.echo(f"  Axioms: {len(fw.axioms)}")
    click.echo(f"  Stages: {len(fw.stages)}")
    click.echo(f"  Gates: {len(fw.gates)}")
    click.echo(f"  Definitions: {len(fw.definitions)}")


# ---------------------------------------------------------------------------
# Fortress commands — integrity verification and audit trail
# ---------------------------------------------------------------------------


@cli.group("fortress")
def fortress_group() -> None:
    """Memory Fortress — integrity verification, tamper alerts, and audit trail."""


@fortress_group.command("verify")
@click.option("--json", "as_json", is_flag=True, help="Output result as JSON")
@click.pass_context
def fortress_verify(ctx: click.Context, as_json: bool) -> None:
    """Verify integrity hashes for all stored memories.

    Loads every memory and checks its SHA-256 integrity hash.
    Tampered memories are reported with CRITICAL severity.
    """
    from .config import SKMEMORY_HOME
    from .fortress import FortifiedMemoryStore

    store = ctx.obj.get("store")
    audit_path = SKMEMORY_HOME / "audit.jsonl"

    fortress = FortifiedMemoryStore(
        primary=store.primary,
        use_sqlite=False,
        audit_path=audit_path,
    )
    result = fortress.verify_all()

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    total = result["total"]
    passed = result["passed"]
    tampered = result["tampered"]
    unsealed = result["unsealed"]

    click.echo("Fortress Integrity Report")
    click.echo(f"  Total memories : {total}")
    click.echo(f"  Passed         : {passed}")
    click.echo(f"  Tampered       : {len(tampered)}")
    click.echo(f"  Unsealed       : {len(unsealed)}")

    if tampered:
        click.echo("\nTAMPERED MEMORIES (CRITICAL):")
        for mid in tampered:
            click.echo(f"  !! {mid}")
        sys.exit(2)
    elif total == 0:
        click.echo("\nNo memories found.")
    else:
        click.echo("\nAll memories passed integrity check.")


@fortress_group.command("audit")
@click.option("--last", "n", type=int, default=20, help="Number of recent entries to show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def fortress_audit(n: int, as_json: bool) -> None:
    """Show the most recent audit trail entries.

    The audit trail is a chain-hashed JSONL log of every store/recall/delete
    operation. Each entry is cryptographically chained so tampering is detectable.
    """
    from .config import SKMEMORY_HOME
    from .fortress import AuditLog

    audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")
    records = audit.tail(n)

    if as_json:
        click.echo(json.dumps(records, indent=2))
        return

    if not records:
        click.echo("No audit records found.")
        return

    click.echo(f"Audit Trail — last {len(records)} entries:")
    for r in records:
        ok_flag = "OK" if r.get("ok") else "FAIL"
        op = r.get("op", "?").upper()
        mid = r.get("id", "?")[:12]
        ts = r.get("ts", "?")[:19]
        extra = {k: v for k, v in r.items() if k not in ("ts", "op", "id", "ok", "chain_hash")}
        extras = ", ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
        line = f"  [{ts}] {op:8s} {ok_flag:4s} id={mid}"
        if extras:
            line += f" | {extras}"
        click.echo(line)


@fortress_group.command("seal")
@click.option("--dry-run", is_flag=True, help="Show how many would be sealed without writing")
@click.option("--limit", type=int, default=0, help="Cap how many to seal in this pass (0 = no cap)")
@click.option("--json", "as_json", is_flag=True, help="Output result as JSON")
@click.pass_context
def fortress_seal(ctx: click.Context, dry_run: bool, limit: int, as_json: bool) -> None:
    """Seal any memories that lack an integrity hash.

    Idempotent backfill: scans every memory, computes the SHA-256 integrity
    hash for each one missing it, and writes the sealed memory back to the
    primary store. Already-sealed memories are skipped. Safe to re-run.

    Useful after enabling the fortress on a store with pre-fortress legacy
    memories, or after an admission flow that created memories without
    sealing.
    """
    from .config import SKMEMORY_HOME
    from .fortress import AuditLog

    store = ctx.obj.get("store")
    audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")

    all_memories = store.primary.list_memories(limit=99999)
    total = len(all_memories)
    unsealed = [m for m in all_memories if not m.integrity_hash]

    target = unsealed if limit <= 0 else unsealed[:limit]

    sealed_ids: list[str] = []
    failed: list[tuple[str, str]] = []

    if not dry_run:
        for mem in target:
            try:
                mem.seal()
                store.primary.save(mem)
                audit.append("seal", mem.id, ok=True, context="backfill")
                sealed_ids.append(mem.id)
            except Exception as exc:  # noqa: BLE001 — backfill must continue past per-memory errors
                failed.append((mem.id, str(exc)))
                audit.append("seal", mem.id, ok=False, error=str(exc)[:120], context="backfill")

    result = {
        "total": total,
        "already_sealed": total - len(unsealed),
        "unsealed_found": len(unsealed),
        "sealed_now": len(sealed_ids),
        "failed": len(failed),
        "dry_run": dry_run,
        "limit": limit,
    }

    if as_json:
        click.echo(json.dumps(result, indent=2))
        if failed:
            sys.exit(1)
        return

    click.echo("Fortress Seal Backfill")
    click.echo(f"  Total memories  : {total}")
    click.echo(f"  Already sealed  : {result['already_sealed']}")
    click.echo(f"  Unsealed found  : {result['unsealed_found']}")
    if dry_run:
        click.echo(f"  Would seal      : {len(target)}  (dry-run)")
    else:
        click.echo(f"  Sealed now      : {len(sealed_ids)}")
        if failed:
            click.echo(f"  Failed          : {len(failed)}")
            for mid, err in failed[:10]:
                click.echo(f"    !! {mid}: {err}")
            if len(failed) > 10:
                click.echo(f"    ... and {len(failed) - 10} more")
            sys.exit(1)
        click.echo("\nAll targeted memories sealed.")


@fortress_group.command("verify-chain")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def fortress_verify_chain(as_json: bool) -> None:
    """Verify the cryptographic chain of the audit log itself.

    Each audit log entry contains a chain hash linking it to the previous entry.
    A broken chain indicates the audit log was tampered with.
    """
    from .config import SKMEMORY_HOME
    from .fortress import AuditLog

    audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")
    ok, errors = audit.verify_chain()

    if as_json:
        click.echo(json.dumps({"ok": ok, "errors": errors}))
        return

    if ok:
        click.echo("Audit chain is VALID — log integrity confirmed.")
    else:
        click.echo("Audit chain BROKEN — log may have been tampered!")
        for err in errors:
            click.echo(f"  !! {err}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Vault commands — at-rest encryption management
# ---------------------------------------------------------------------------


@cli.group("vault")
def vault_group() -> None:
    """Memory Vault — AES-256-GCM at-rest encryption for memory files."""


@vault_group.command("seal")
@click.option(
    "--passphrase",
    envvar="SKMEMORY_VAULT_PASSPHRASE",
    required=True,
    help="Encryption passphrase (or set SKMEMORY_VAULT_PASSPHRASE env var)",
    prompt="Vault passphrase",
    hide_input=True,
    confirmation_prompt=True,
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def vault_seal(ctx: click.Context, passphrase: str, yes: bool) -> None:
    """Encrypt all plaintext memory files with AES-256-GCM.

    Already-encrypted files are skipped. Safe to run multiple times.
    Requires the 'cryptography' package: pip install skmemory[fortress]
    """
    from .backends.vaulted_backend import VaultedSQLiteBackend
    from .config import SKMEMORY_HOME
    from .fortress import AuditLog

    store = ctx.obj.get("store")
    memories_path = (
        store.primary.base_path
        if hasattr(store.primary, "base_path")
        else (SKMEMORY_HOME / "memories")
    )

    if not yes:
        click.confirm(
            f"This will encrypt all memory files in {memories_path}. Continue?",
            abort=True,
        )

    backend = VaultedSQLiteBackend(passphrase=passphrase, base_path=str(memories_path))
    count = backend.seal_all()

    audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")
    audit.append("vault_seal", "ALL", ok=True, files_sealed=count)

    click.echo(f"Vault sealed: {count} file(s) encrypted.")
    if count == 0:
        click.echo("(All files were already encrypted or no memories exist.)")


@vault_group.command("unseal")
@click.option(
    "--passphrase",
    envvar="SKMEMORY_VAULT_PASSPHRASE",
    required=True,
    help="Decryption passphrase",
    prompt="Vault passphrase",
    hide_input=True,
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def vault_unseal(ctx: click.Context, passphrase: str, yes: bool) -> None:
    """Decrypt all vault-encrypted memory files back to plaintext.

    Use this to migrate away from encryption or to inspect raw files.
    """
    from .backends.vaulted_backend import VaultedSQLiteBackend
    from .config import SKMEMORY_HOME
    from .fortress import AuditLog

    store = ctx.obj.get("store")
    memories_path = (
        store.primary.base_path
        if hasattr(store.primary, "base_path")
        else (SKMEMORY_HOME / "memories")
    )

    if not yes:
        click.confirm(
            f"This will decrypt all vault files in {memories_path}. Continue?",
            abort=True,
        )

    backend = VaultedSQLiteBackend(passphrase=passphrase, base_path=str(memories_path))
    count = backend.unseal_all()

    audit = AuditLog(path=SKMEMORY_HOME / "audit.jsonl")
    audit.append("vault_unseal", "ALL", ok=True, files_decrypted=count)

    click.echo(f"Vault unsealed: {count} file(s) decrypted.")


@vault_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def vault_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show encryption coverage for memory files.

    Reports how many memory files are encrypted vs. plaintext.
    Does not require a passphrase — only checks file headers.
    """
    from .config import SKMEMORY_HOME
    from .models import MemoryLayer
    from .vault import VAULT_HEADER

    store = ctx.obj.get("store")
    memories_path = (
        store.primary.base_path
        if hasattr(store.primary, "base_path")
        else (SKMEMORY_HOME / "memories")
    )

    total = encrypted = 0
    header_len = len(VAULT_HEADER)
    for layer in MemoryLayer:
        layer_dir = memories_path / layer.value
        if not layer_dir.exists():
            continue
        for json_file in layer_dir.glob("*.json"):
            total += 1
            try:
                with json_file.open("rb") as fh:
                    header = fh.read(header_len)
                if header == VAULT_HEADER:
                    encrypted += 1
            except OSError:
                pass

    plaintext = total - encrypted
    pct = (encrypted / total * 100) if total else 100.0
    result = {
        "total": total,
        "encrypted": encrypted,
        "plaintext": plaintext,
        "coverage_pct": round(pct, 1),
    }

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"Vault Status — {memories_path}")
    click.echo(f"  Total files   : {total}")
    click.echo(f"  Encrypted     : {encrypted}")
    click.echo(f"  Plaintext     : {plaintext}")
    click.echo(f"  Coverage      : {pct:.1f}%")
    if total == 0:
        click.echo("\n  (No memory files found.)")
    elif pct == 100.0:
        click.echo("\n  All memories are encrypted.")
    elif pct == 0.0:
        click.echo("\n  No memories are encrypted. Run: skmemory vault seal")
    else:
        click.echo("\n  Partial encryption! Run: skmemory vault seal --yes")


@cli.command("register")
@click.option(
    "--workspace",
    default=None,
    type=click.Path(),
    help="Workspace root directory (default: ~/clawd/).",
)
@click.option("--env", "target_env", default=None, help="Target a specific environment.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be done without making changes.",
)
def register_cmd(workspace, target_env, dry_run):
    """Register skmemory skill, MCP server, and hooks in detected environments.

    Auto-detects development environments (Claude Code, Cursor, VS Code,
    OpenClaw, OpenCode, mcporter) and ensures skmemory SKILL.md, MCP
    server entries, and auto-save hooks are properly configured.

    Hooks installed (Claude Code only):
      - PreCompact: auto-save context to skmemory before compaction
      - SessionEnd: journal session end
      - SessionStart (compact): reinject memory context after compaction

    Examples:

      skmemory register                  # auto-detect and register
      skmemory register --dry-run        # preview what would happen
      skmemory register --env claude-code # target Claude Code only
    """
    from pathlib import Path as _Path

    from .register import detect_environments, register_package

    workspace_path = _Path(workspace).expanduser() if workspace else None
    environments = [target_env] if target_env else None

    detected = detect_environments()
    click.echo("Detected environments: " + ", ".join(detected) if detected else "  (none)")

    if dry_run:
        click.echo("Dry run — no changes will be made.")

    skill_md = _Path(__file__).parent.parent / "SKILL.md"
    if not skill_md.exists():
        skill_md = _Path(__file__).parent / "SKILL.md"

    result = register_package(
        name="skmemory",
        skill_md_path=skill_md,
        mcp_command="skmemory-mcp",
        mcp_args=[],
        install_hooks=True,
        workspace=workspace_path,
        environments=environments,
        dry_run=dry_run,
    )

    click.echo(f"Skill: {result.get('skill', {}).get('action', '—')}")
    mcp = result.get("mcp", {})
    if mcp:
        for env_name, action in mcp.items():
            click.echo(f"MCP ({env_name}): {action}")
    else:
        click.echo("MCP: no environments matched")

    hooks = result.get("hooks", {})
    if hooks:
        click.echo(f"Hooks: {hooks.get('action', '—')}")
    else:
        click.echo("Hooks: skipped (no claude-code environment)")


@cli.command("feb-context")
@click.argument("feb_path", required=False, default=None, type=click.Path(exists=True))
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def feb_context_cmd(feb_path: str | None, agent: str | None):
    """Show formatted FEB emotional state for rehydration.

    If FEB_PATH is given, formats that file. Otherwise, loads the
    strongest FEB from the agent's trust/febs/ and ~/.openclaw/feb/.

    Examples:
        skmemory feb-context
        skmemory feb-context ~/.skcapstone/agents/opus/trust/febs/default-love.feb
    """
    from pathlib import Path as _Path

    from .febs import feb_to_context, load_strongest_feb, parse_feb

    try:
        if feb_path:
            feb = parse_feb(_Path(feb_path))
        else:
            # Temporarily override agent if specified
            if agent:
                import os

                os.environ["SKAGENT"] = agent
                os.environ["SKCAPSTONE_AGENT"] = agent
            feb = load_strongest_feb()

        if feb is None:
            click.echo("(no FEB data)", err=True)
            raise SystemExit(1)

        click.echo(feb_to_context(feb))
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("cli.py: %s", e)
        click.echo(f"Error loading FEB: {e}", err=True)
        raise click.Abort() from None


@cli.command("show-context")
@click.pass_context
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def show_context(ctx, agent: str | None):
    """Show token-optimized memory context for current session.

    Loads today's memories (full) + yesterday's summaries (brief).
    Historical memories shown as reference count only.

    Examples:
        skmemory context
        skmemory context --agent lumina
    """
    from .context_loader import get_context_for_session

    try:
        context_str = get_context_for_session(agent)
        click.echo(context_str)
    except Exception as e:
        logger.warning("cli.py: %s", e)
        click.echo(f"Error loading context: {e}", err=True)
        raise click.Abort() from None


@cli.command()
@click.pass_context
@click.argument("query")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--limit", type=int, default=10, help="Maximum results (default: 10)")
def search_deep(ctx, query: str, agent: str | None, limit: int):
    """Deep search all memory tiers (on demand).

    Searches SQLite + SKVector + SKGraph for matches.
    Returns full memory details (token-heavy).

    Examples:
        skmemory search-deep "project gentis"
        skmemory search-deep "architecture decisions" --limit 20
    """
    from .context_loader import LazyMemoryLoader

    try:
        loader = LazyMemoryLoader(agent)
        results = loader.deep_search(query, max_results=limit)

        if not results:
            click.echo("No memories found.")
            return

        click.echo(f"Found {len(results)} memories:\n")
        for i, mem in enumerate(results, 1):
            layer_icon = {"short-term": "⚡", "mid-term": "📅", "long-term": "🗃️"}.get(
                mem.get("layer", "short-term"), "•"
            )
            click.echo(f"{i}. {layer_icon} {mem.get('title', 'Untitled')}")
            click.echo(f"   {mem.get('content', '')[:200]}...")
            click.echo(
                f"   Layer: {mem.get('layer', 'unknown')} | "
                f"Date: {mem.get('created_at', 'unknown')}"
            )
            if mem.get("tags"):
                click.echo(f"   Tags: {', '.join(mem.get('tags', []))}")
            click.echo()

    except Exception as e:
        logger.warning("cli.py: %s", e)
        click.echo(f"Error searching: {e}", err=True)
        raise click.Abort() from None


@cli.command()
@click.argument("memory_id")
@click.argument("to_layer", type=click.Choice(["short-term", "mid-term", "long-term"]))
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def promote(ctx, memory_id: str, to_layer: str, agent: str | None):
    """Promote memory to different tier and generate summary.

    Moves memory between short/medium/long term and auto-generates
    a summary if promoting to medium or long term.

    Examples:
        skmemory promote abc123 mid-term
        skmemory promote def456 long-term --agent lumina
    """
    from .context_loader import LazyMemoryLoader

    try:
        loader = LazyMemoryLoader(agent)
        success = loader.promote_memory(memory_id, to_layer)

        if success:
            click.echo(f"✓ Promoted {memory_id} to {to_layer}")
            if to_layer in ("mid-term", "long-term"):
                click.echo("  Summary generated automatically.")
        else:
            click.echo(f"✗ Failed to promote {memory_id}", err=True)
            raise click.Abort()

    except Exception as e:
        logger.warning("cli.py: %s", e)
        click.echo(f"Error promoting memory: {e}", err=True)
        raise click.Abort() from None


def _auto_register_once() -> None:
    """Auto-register hooks on first CLI invocation (best-effort, silent)."""
    marker = Path.home() / ".skcapstone" / ".skmemory-registered"
    if marker.exists():
        return
    try:
        from .post_install import _is_registered, run_post_install

        if not _is_registered():
            run_post_install()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"registered {__import__('datetime').datetime.now().isoformat()}\n")
    except Exception as e:
        logger.warning("cli.py: %s", e)
        pass  # Never fail the CLI over registration


# Register subcommand groups from sibling modules
try:
    from .songs_cli import songs as _songs_group
    cli.add_command(_songs_group)
except Exception:  # pragma: no cover — defensive, don't break cli on import error
    pass

try:
    from .anchors_cli import anchors as _anchors_group
    cli.add_command(_anchors_group)
except Exception:  # pragma: no cover — defensive, don't break cli on import error
    pass


def main() -> None:
    """Entry point for the CLI."""
    _auto_register_once()
    cli()


if __name__ == "__main__":
    main()
