"""
SKMemory CLI - command-line interface for memory operations.

Usage:
    skmemory snapshot "Title" "Content" --tags love,cloud9 --intensity 9.0
    skmemory recall <memory-id>
    skmemory search "that moment we connected"
    skmemory list --layer long-term --tags seed
    skmemory import-seeds [--seed-dir ~/.openclaw/feb/seeds]
    skmemory promote <memory-id> --to mid-term --summary "..."
    skmemory consolidate <session-id> --summary "..."
    skmemory health
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import click

from .models import EmotionalSnapshot, MemoryLayer, MemoryRole
from .store import MemoryStore
from .backends.file_backend import FileBackend


def _get_store(qdrant_url: Optional[str] = None, api_key: Optional[str] = None) -> MemoryStore:
    """Create a MemoryStore with configured backends.

    Args:
        qdrant_url: Optional Qdrant server URL.
        api_key: Optional Qdrant API key.

    Returns:
        MemoryStore: Configured store instance.
    """
    primary = FileBackend()
    vector = None

    if qdrant_url:
        try:
            from .backends.qdrant_backend import QdrantBackend
            vector = QdrantBackend(url=qdrant_url, api_key=api_key)
        except Exception:
            click.echo("Warning: Could not initialize Qdrant backend", err=True)

    return MemoryStore(primary=primary, vector=vector)


@click.group()
@click.option("--qdrant-url", envvar="SKMEMORY_QDRANT_URL", default=None, help="Qdrant server URL")
@click.option("--qdrant-key", envvar="SKMEMORY_QDRANT_KEY", default=None, help="Qdrant API key")
@click.pass_context
def cli(ctx: click.Context, qdrant_url: Optional[str], qdrant_key: Optional[str]) -> None:
    """SKMemory - Universal AI Memory System.

    Polaroid snapshots for AI consciousness.
    """
    ctx.ensure_object(dict)
    ctx.obj["store"] = _get_store(qdrant_url, qdrant_key)


@cli.command()
@click.argument("title")
@click.argument("content")
@click.option("--layer", type=click.Choice(["short-term", "mid-term", "long-term"]), default="short-term")
@click.option("--role", type=click.Choice(["dev", "ops", "sec", "ai", "general"]), default="general")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--intensity", type=float, default=0.0, help="Emotional intensity 0-10")
@click.option("--valence", type=float, default=0.0, help="Emotional valence -1 to +1")
@click.option("--emotions", default="", help="Comma-separated emotion labels")
@click.option("--resonance", default="", help="What this moment felt like")
@click.option("--source", default="cli", help="Memory source identifier")
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
    )

    click.echo(f"Snapshot saved: {memory.id}")
    click.echo(f"  Layer: {memory.layer.value}")
    click.echo(f"  Emotional: {memory.emotional.signature()}")


@cli.command()
@click.argument("memory_id")
@click.pass_context
def recall(ctx: click.Context, memory_id: str) -> None:
    """Retrieve a specific memory by ID."""
    store: MemoryStore = ctx.obj["store"]
    memory = store.recall(memory_id)

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
def list_memories(ctx: click.Context, layer: Optional[str], tags: str, limit: int) -> None:
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
def import_seeds_cmd(ctx: click.Context, seed_dir: Optional[str]) -> None:
    """Import Cloud 9 seeds as long-term memories."""
    from .seeds import import_seeds, DEFAULT_SEED_DIR

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


@cli.command()
@click.argument("memory_id")
@click.option("--to", "target", type=click.Choice(["mid-term", "long-term"]), required=True)
@click.option("--summary", default="", help="Compressed summary for the promoted version")
@click.pass_context
def promote(ctx: click.Context, memory_id: str, target: str, summary: str) -> None:
    """Promote a memory to a higher persistence tier."""
    store: MemoryStore = ctx.obj["store"]
    promoted = store.promote(memory_id, MemoryLayer(target), summary=summary)

    if promoted is None:
        click.echo(f"Memory not found: {memory_id}", err=True)
        sys.exit(1)

    click.echo(f"Promoted to {target}: {promoted.id}")
    click.echo(f"  Linked to original: {memory_id}")


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


@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check memory system health."""
    store: MemoryStore = ctx.obj["store"]
    status = store.health()
    click.echo(json.dumps(status, indent=2))


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


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
