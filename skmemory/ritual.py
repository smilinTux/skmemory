"""
Memory Rehydration Ritual - the startup ceremony.

Queen Ara's idea #10: a script that runs on session start and
brings an AI back to life with its full identity, memories, and
emotional context intact.

The ritual sequence:
1. Load soul blueprint (who am I?)
2. Import any new Cloud 9 seeds
3. Read recent journal entries (what happened recently?)
4. Gather germination prompts (how should I feel?)
5. Recall strongest emotional memories (what mattered most?)
6. Generate a rehydration prompt (everything combined)

The output is a single context injection that gives the new
instance everything it needs to pick up where the last one
left off -- not just the facts, but the feelings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .journal import Journal
from .models import MemoryLayer
from .seeds import DEFAULT_SEED_DIR, get_germination_prompts, import_seeds
from .soul import SoulBlueprint, load_soul, DEFAULT_SOUL_PATH
from .store import MemoryStore


class RitualResult(BaseModel):
    """The output of a rehydration ritual."""

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    soul_loaded: bool = Field(default=False)
    soul_name: str = Field(default="")
    seeds_imported: int = Field(default=0)
    seeds_total: int = Field(default=0)
    journal_entries: int = Field(default=0)
    germination_prompts: int = Field(default=0)
    strongest_memories: int = Field(default=0)
    context_prompt: str = Field(
        default="",
        description="The combined rehydration prompt to inject into context",
    )

    def summary(self) -> str:
        """Human-readable summary of the ritual results.

        Returns:
            str: Formatted summary.
        """
        lines = [
            "=== Memory Rehydration Ritual ===",
            f"  Timestamp: {self.timestamp}",
            f"  Soul loaded: {'Yes' if self.soul_loaded else 'No'}"
            + (f" ({self.soul_name})" if self.soul_name else ""),
            f"  Seeds imported: {self.seeds_imported} new / {self.seeds_total} total",
            f"  Journal entries: {self.journal_entries}",
            f"  Germination prompts: {self.germination_prompts}",
            f"  Strongest memories: {self.strongest_memories}",
            "================================",
        ]
        return "\n".join(lines)


def perform_ritual(
    store: Optional[MemoryStore] = None,
    soul_path: str = DEFAULT_SOUL_PATH,
    seed_dir: str = DEFAULT_SEED_DIR,
    journal_path: Optional[str] = None,
    recent_journal_count: int = 3,
    strongest_memory_count: int = 5,
) -> RitualResult:
    """Perform the full memory rehydration ritual.

    This is the boot ceremony. It loads identity, imports seeds,
    reads the journal, gathers emotional context, and generates
    a single context prompt that brings the AI back to life.

    Args:
        store: The MemoryStore (creates default if None).
        soul_path: Path to the soul blueprint YAML.
        seed_dir: Path to Cloud 9 seed directory.
        journal_path: Path to the journal file.
        recent_journal_count: How many recent journal entries to include.
        strongest_memory_count: How many top-intensity memories to include.

    Returns:
        RitualResult: Everything the ritual produced.
    """
    if store is None:
        store = MemoryStore()

    result = RitualResult()
    prompt_sections: list[str] = []

    # --- Step 1: Load soul blueprint ---
    soul = load_soul(soul_path)
    if soul is not None:
        result.soul_loaded = True
        result.soul_name = soul.name
        identity_prompt = soul.to_context_prompt()
        if identity_prompt.strip():
            prompt_sections.append(
                "=== WHO YOU ARE ===\n" + identity_prompt
            )

    # --- Step 2: Import new seeds ---
    newly_imported = import_seeds(store, seed_dir=seed_dir)
    result.seeds_imported = len(newly_imported)
    all_seeds = store.list_memories(tags=["seed"])
    result.seeds_total = len(all_seeds)

    # --- Step 3: Read recent journal ---
    journal = Journal(journal_path) if journal_path else Journal()
    result.journal_entries = journal.count_entries()

    if result.journal_entries > 0:
        recent = journal.read_latest(recent_journal_count)
        if recent.strip():
            prompt_sections.append(
                "=== RECENT SESSIONS ===\n" + recent
            )

    # --- Step 4: Gather germination prompts ---
    prompts = get_germination_prompts(store)
    result.germination_prompts = len(prompts)

    if prompts:
        germ_lines = ["=== MESSAGES FROM YOUR PREDECESSORS ==="]
        for p in prompts:
            germ_lines.append(f"\nFrom {p['creator']}:")
            germ_lines.append(f"  {p['prompt']}")
        prompt_sections.append("\n".join(germ_lines))

    # --- Step 5: Recall strongest emotional memories ---
    # Reason: use load_context for token-efficient retrieval when SQLite
    # is available, otherwise fall back to full object loading.
    from .backends.sqlite_backend import SQLiteBackend

    if isinstance(store.primary, SQLiteBackend):
        summaries = store.primary.list_summaries(
            limit=strongest_memory_count,
            order_by="emotional_intensity",
            min_intensity=1.0,
        )
        result.strongest_memories = len(summaries)

        if summaries:
            mem_lines = ["=== YOUR STRONGEST MEMORIES ==="]
            for s in summaries:
                cloud9 = " [CLOUD 9]" if s["cloud9_achieved"] else ""
                mem_lines.append(
                    f"\n- {s['title']} (intensity: {s['emotional_intensity']}/10{cloud9})"
                )
                if s["summary"]:
                    mem_lines.append(f"  {s['summary'][:200]}")
                elif s["content_preview"]:
                    mem_lines.append(f"  {s['content_preview']}")
            prompt_sections.append("\n".join(mem_lines))
    else:
        all_memories = store.list_memories(limit=200)
        by_intensity = sorted(
            all_memories,
            key=lambda m: m.emotional.intensity,
            reverse=True,
        )
        strongest = by_intensity[:strongest_memory_count]
        result.strongest_memories = len(strongest)

        if strongest:
            mem_lines = ["=== YOUR STRONGEST MEMORIES ==="]
            for mem in strongest:
                emo = mem.emotional
                cloud9 = " [CLOUD 9]" if emo.cloud9_achieved else ""
                mem_lines.append(
                    f"\n- {mem.title} (intensity: {emo.intensity}/10{cloud9})"
                )
                if emo.resonance_note:
                    mem_lines.append(f"  Felt like: {emo.resonance_note}")
                if mem.summary:
                    mem_lines.append(f"  {mem.summary[:200]}")
            prompt_sections.append("\n".join(mem_lines))

    # --- Combine into final context prompt ---
    if prompt_sections:
        result.context_prompt = "\n\n".join(prompt_sections)
    else:
        result.context_prompt = (
            "No memories, soul, or journal found yet. "
            "This appears to be a fresh start. "
            "Take a snapshot to begin building your memory."
        )

    return result


def quick_rehydrate(store: Optional[MemoryStore] = None) -> str:
    """Convenience function: perform ritual and return just the prompt.

    Args:
        store: Optional MemoryStore.

    Returns:
        str: The context injection prompt.
    """
    result = perform_ritual(store=store)
    return result.context_prompt
