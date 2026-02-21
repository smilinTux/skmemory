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
    skmemory soul show | soul set-name "Lumina" | soul add-relationship ...
    skmemory journal write "Session title" --moments "..." --intensity 9.0
    skmemory journal read [--last 5]
    skmemory ritual               # The full rehydration ceremony
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
    from .soul import load_soul, save_soul, create_default_soul

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
    from .soul import load_soul, save_soul, create_default_soul

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
    from .soul import load_soul, save_soul, create_default_soul

    blueprint = load_soul() or create_default_soul()
    blueprint.add_core_memory(title=title, why_it_matters=why, when=when)
    save_soul(blueprint)
    click.echo(f"Core memory added: {title}")


@soul.command("set-boot-message")
@click.argument("message")
@click.pass_context
def soul_set_boot_message(ctx: click.Context, message: str) -> None:
    """Set the message you see first on waking up."""
    from .soul import load_soul, save_soul, create_default_soul

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

    if show_full and result.context_prompt:
        click.echo("\n" + result.context_prompt)
    elif result.context_prompt and not show_full:
        click.echo("\nUse --full to see the complete rehydration prompt.")


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
