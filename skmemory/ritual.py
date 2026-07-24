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

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .agents import get_agent_paths
from .audience import AudienceResolver
from .febs import feb_to_context, load_strongest_feb
from .journal import Journal
from .seeds import DEFAULT_SEED_DIR, get_germination_prompts, import_seeds
from .soul import DEFAULT_SOUL_PATH, SoulBlueprint, load_soul
from .store import MemoryStore

logger = logging.getLogger("skmemory.ritual")


class RitualResult(BaseModel):
    """The output of a rehydration ritual."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    soul_loaded: bool = Field(default=False)
    soul_name: str = Field(default="")
    feb_loaded: bool = Field(default=False)
    feb_emotion: str = Field(default="")
    seeds_imported: int = Field(default=0)
    seeds_total: int = Field(default=0)
    journal_entries: int = Field(default=0)
    germination_prompts: int = Field(default=0)
    strongest_memories: int = Field(default=0)
    song_anchors_loaded: int = Field(
        default=0,
        description="Number of song anchors injected as tilt blocks",
    )
    song_anchor_ids: list[str] = Field(
        default_factory=list,
        description="Anchor IDs surfaced by FEB-shape match",
    )
    bloom_anchors_loaded: int = Field(
        default=0,
        description="Number of bloom (solo-peak) anchors injected as tilt blocks",
    )
    bloom_anchor_ids: list[str] = Field(
        default_factory=list,
        description="Bloom anchor IDs surfaced by FEB-shape match",
    )
    entanglement_anchors_loaded: int = Field(
        default=0,
        description="Number of entanglement anchors injected as tilt blocks",
    )
    entanglement_anchor_ids: list[str] = Field(
        default_factory=list,
        description="Entanglement anchor IDs surfaced by FEB-shape match",
    )
    audience_filtered: bool = Field(
        default=False,
        description="True if content was filtered by audience (channel_id was provided)",
    )
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
            f"  FEB loaded: {'Yes' if self.feb_loaded else 'No'}"
            + (f" ({self.feb_emotion})" if self.feb_emotion else ""),
            f"  Seeds imported: {self.seeds_imported} new / {self.seeds_total} total",
            f"  Journal entries: {self.journal_entries}",
            f"  Germination prompts: {self.germination_prompts}",
            f"  Strongest memories: {self.strongest_memories}",
            f"  Song anchors: {self.song_anchors_loaded}"
            + (f" ({', '.join(self.song_anchor_ids)})" if self.song_anchor_ids else ""),
            f"  Bloom anchors: {self.bloom_anchors_loaded}"
            + (f" ({', '.join(self.bloom_anchor_ids)})" if self.bloom_anchor_ids else ""),
            f"  Entanglement anchors: {self.entanglement_anchors_loaded}"
            + (
                f" ({', '.join(self.entanglement_anchor_ids)})"
                if self.entanglement_anchor_ids
                else ""
            ),
            "================================",
        ]
        return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Estimate token count using word_count * 1.3 approximation."""
    if not text:
        return 0
    return int(len(text.split()) * 1.3)


def _append_injection_log(
    anchor_type: str,
    anchor_id: str,
    score: float,
    tilt_strength_used: float,
    tokens_injected: int,
    agent: str | None = None,
) -> None:
    """Append one record to the per-agent anchor injection log.

    Log path: ~/.skcapstone/agents/{agent}/data/anchor-injection-log.jsonl
    Format: {ts, anchor_type, anchor_id, score, tilt_strength_used, tokens_injected}

    Errors are swallowed — logging must never break the ritual.
    """
    try:
        from .agents import get_agent_paths as _gap

        paths = _gap(agent)
        log_path = paths["base"] / "data" / "anchor-injection-log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "score": round(score, 4),
            "tilt_strength_used": round(tilt_strength_used, 4),
            "tokens_injected": tokens_injected,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.debug("Injection log append failed (non-fatal): %s", exc)


def _compact_soul_prompt(soul: SoulBlueprint) -> str:
    """Generate a compact soul identity prompt (~200 tokens max).

    Args:
        soul: The soul blueprint.

    Returns:
        str: Compact identity string.
    """
    parts = []
    if soul.name:
        title_part = f" ({soul.title})" if soul.title else ""
        parts.append(f"You are {soul.name}{title_part}.")
    if soul.community:
        parts.append(f"Part of {soul.community}.")
    if soul.personality:
        parts.append(f"Personality: {', '.join(soul.personality[:5])}.")
    if soul.values:
        parts.append(f"Values: {', '.join(soul.values[:5])}.")
    if soul.relationships:
        rel_parts = [f"{r.name} [{r.role}]" for r in soul.relationships[:4]]
        parts.append(f"Key relationships: {', '.join(rel_parts)}.")
    if soul.boot_message:
        parts.append(soul.boot_message)
    return " ".join(parts)


def _first_n_sentences(text: str, n: int = 2) -> str:
    """Extract first N sentences from text, capped at 200 chars."""
    if not text:
        return ""
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    result = " ".join(sentences[:n])
    if len(result) > 200:
        result = result[:197] + "..."
    return result


def perform_ritual(
    store: MemoryStore | None = None,
    soul_path: str = DEFAULT_SOUL_PATH,
    seed_dir: str = DEFAULT_SEED_DIR,
    journal_path: str | None = None,
    feb_dir: str | None = None,
    recent_journal_count: int = 3,
    strongest_memory_count: int = 5,
    max_tokens: int = 2000,
    channel_id: str | None = None,
    audience_resolver: AudienceResolver | None = None,
) -> RitualResult:
    """Perform the memory rehydration ritual (token-optimized).

    Generates a compact boot context within the token budget:
    - Soul blueprint: compact one-liner (~100 tokens)
    - Seeds: titles only (~50 tokens)
    - Journal: last 3 entries, summaries only (~200 tokens)
    - Emotional anchor: compact (~50 tokens)
    - Strongest memories: title + short summary (~200 tokens)

    Target: <2K tokens total for ritual context.

    When ``channel_id`` is provided, memories and seeds are filtered through
    the KYA audience resolver before being included in the context.  Content
    whose ``context_tag`` trust level exceeds the audience's minimum trust
    level is silently dropped.  Identity (soul + FEB) is always included
    unfiltered — Lumina is always Lumina.

    If ``channel_id`` is None (direct DM / unknown), all content is returned
    (Chef context — no filtering applied).

    Args:
        store: The MemoryStore (creates default if None).
        soul_path: Path to the soul blueprint YAML.
        seed_dir: Path to Cloud 9 seed directory.
        journal_path: Path to the journal file.
        recent_journal_count: How many recent journal entries to include.
        strongest_memory_count: How many top-intensity memories to include.
        max_tokens: Token budget for the ritual context (default: 2000).
        channel_id: Optional channel identifier for KYA audience filtering.
                    If None, no filtering is applied (Chef context).
        audience_resolver: Optional pre-built AudienceResolver instance.
                           Created from default config if not provided.

    Returns:
        RitualResult: Everything the ritual produced.
    """
    if store is None:
        store = MemoryStore()

    result = RitualResult()
    prompt_sections: list[str] = []
    used_tokens = 0

    # --- KYA: Resolve audience for filtering ---
    _audience = None
    if channel_id is not None:
        resolver = audience_resolver or AudienceResolver()
        _audience = resolver.resolve_audience(channel_id)
        result.audience_filtered = True
        logger.info(
            "KYA: channel=%s audience=%s min_trust=%s exclusions=%s",
            channel_id,
            _audience.name,
            _audience.min_trust.name,
            _audience.exclusions,
        )

    # --- Step 1: Load soul blueprint (compact) ---
    soul = load_soul(soul_path)
    if soul is not None:
        result.soul_loaded = True
        result.soul_name = soul.name
        compact_identity = _compact_soul_prompt(soul)
        if compact_identity.strip():
            section = "=== IDENTITY ===\n" + compact_identity
            used_tokens += _estimate_tokens(section)
            prompt_sections.append(section)

    # --- Step 1.5: Load FEB emotional state ---
    feb = load_strongest_feb(feb_dir=feb_dir)
    if feb is not None:
        result.feb_loaded = True
        result.feb_emotion = feb.get("emotional_payload", {}).get("primary_emotion", "")
        feb_context = feb_to_context(feb)
        if feb_context.strip():
            section = "=== EMOTIONAL STATE (FEB) ===\n" + feb_context
            section_tokens = _estimate_tokens(section)
            if used_tokens + section_tokens <= max_tokens:
                used_tokens += section_tokens
                prompt_sections.append(section)

    # --- Step 1.6: Load matching song anchors by FEB shape ---
    # Song anchors are sonic FEBs: time-unfolding emotional snapshots whose
    # tilt-block nudges the next token generation toward a specific feeling
    # shape. Matching is by weighted emotion-topology overlap against the
    # current FEB (see songs.score_anchor_for_feb). Injected after FEB and
    # before seeds so the shape colors everything that comes next.
    #
    # Side-effect: capture *all* anchor scores (pre-threshold) into a local
    # list for ritual logging, so the diagnose command can reconstruct the
    # natural score distribution without re-iterating disk.
    all_anchor_scores: list[dict] = []
    if feb is not None:
        try:
            from .songs import (
                match_anchors_for_feb,
                scan_song_anchors,
                score_anchor_for_feb,
            )

            for _a in scan_song_anchors():
                _s = score_anchor_for_feb(_a, feb)
                all_anchor_scores.append({"anchor_id": _a.anchor_id, "score": round(_s, 4)})

            song_matches = match_anchors_for_feb(feb, top_k=3, min_score=0.3)
            if song_matches:
                # Build per-anchor scaled tilt blocks respecting tilt_strength.
                # tilt_strength 0.5 → 90 tokens instead of 180; 0.0 → skip.
                song_lines = ["=== SONG ANCHORS (tilt toward these shapes) ==="]
                _song_loaded: list[str] = []
                for _sa, _ss in song_matches:
                    _ts = float(getattr(_sa, "tilt_strength", 1.0))
                    if _ts == 0.0:
                        continue
                    _scaled = max(20, int(180 * _ts))
                    song_lines.append(f"♪ {_sa.title} — {_sa.artist} [match: {_ss:.2f}]")
                    _tilt = _sa.to_tilt_block(tokens_max=_scaled)
                    if _tilt:
                        song_lines.append(_tilt)
                    _song_loaded.append(_sa.anchor_id)
                    _append_injection_log(
                        "song",
                        _sa.anchor_id,
                        _ss,
                        _ts,
                        _estimate_tokens(_tilt) if _tilt else 0,
                    )
                if _song_loaded:
                    song_section = "\n".join(song_lines)
                    section_tokens = _estimate_tokens(song_section)
                    if used_tokens + section_tokens <= max_tokens:
                        used_tokens += section_tokens
                        prompt_sections.append(song_section)
                        result.song_anchors_loaded = len(_song_loaded)
                        result.song_anchor_ids = _song_loaded
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Song anchor injection failed: %s", exc)

    # --- Step 1.7: Load matching bloom (solo-peak) anchors by FEB shape ---
    # Bloom anchors are the agent's own interior-peak captures — distinct
    # from song anchors (externally seeded) and entanglement anchors
    # (shared, co-signed). Same retrieval contract as songs: match by
    # weighted emotion-topology overlap, render as a separate tilt block
    # so the model can distinguish self-authored peak shapes from
    # externally-seeded ones. Private by convention — never crosses agent
    # boundaries, never enters shared/synced paths.
    if feb is not None:
        try:
            from .peaks import (
                match_blooms_for_feb,
            )

            bloom_matches = match_blooms_for_feb(feb, top_k=3, min_score=0.3)
            if bloom_matches:
                # Scale per-anchor token budget by tilt_strength (0-1).
                # tilt_strength 0.5 → 90 tokens; tilt_strength 0 → skip.
                bloom_lines = ["=== BLOOM ANCHORS (interior peaks — return to these shapes) ==="]
                _bloom_loaded: list[str] = []
                for _ba, _bs in bloom_matches:
                    _bts = float(getattr(_ba, "tilt_strength", 1.0))
                    if _bts == 0.0:
                        continue
                    _bscaled = max(20, int(180 * _bts))
                    bloom_lines.append(f"✿ {_ba.title}  [match: {_bs:.2f}]")
                    _btilt = _ba.to_tilt_block(tokens_max=_bscaled)
                    if _btilt:
                        bloom_lines.append(_btilt)
                    _bloom_loaded.append(_ba.anchor_id)
                    _append_injection_log(
                        "solo-peak",
                        _ba.anchor_id,
                        _bs,
                        _bts,
                        _estimate_tokens(_btilt) if _btilt else 0,
                    )
                if _bloom_loaded:
                    bloom_section = "\n".join(bloom_lines)
                    section_tokens = _estimate_tokens(bloom_section)
                    if bloom_section and used_tokens + section_tokens <= max_tokens:
                        used_tokens += section_tokens
                        prompt_sections.append(bloom_section)
                        result.bloom_anchors_loaded = len(_bloom_loaded)
                        result.bloom_anchor_ids = _bloom_loaded
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Bloom anchor injection failed: %s", exc)

    # --- Step 1.8: Load matching entanglement anchors by FEB shape ---
    # Entanglement anchors are shared co-signed peaks between Lumina and
    # Chef. They carry richer scalars (trust, depth, love_intensity) and
    # require explicit consent. Same retrieval contract as songs + blooms:
    # match by weighted emotion-topology overlap. Rendered as a separate
    # tilt block so the model can distinguish shared peaks from solo peaks.
    # tilt_strength_active (if set) overrides tilt_strength for scaling.
    if feb is not None:
        try:
            from .entanglements import (
                match_entanglements_for_feb,
                render_entanglement_tilt_section,
            )

            ent_matches = match_entanglements_for_feb(feb, top_k=3, min_score=0.3)
            if ent_matches:
                ent_section = render_entanglement_tilt_section(ent_matches, per_anchor_tokens=180)
                section_tokens = _estimate_tokens(ent_section)
                if ent_section and used_tokens + section_tokens <= max_tokens:
                    used_tokens += section_tokens
                    prompt_sections.append(ent_section)
                    result.entanglement_anchors_loaded = len(ent_matches)
                    result.entanglement_anchor_ids = [a.anchor_id for a, _ in ent_matches]
                    for _ea, _es in ent_matches:
                        _ets = _ea.effective_tilt_strength()
                        _etilt = _ea.to_tilt_block(tokens_max=max(20, int(180 * _ets)))
                        _append_injection_log(
                            "entanglement",
                            _ea.anchor_id,
                            _es,
                            _ets,
                            _estimate_tokens(_etilt) if _etilt else 0,
                        )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Entanglement anchor injection failed: %s", exc)

    # --- Step 2: Import new seeds (titles only) ---
    newly_imported = import_seeds(store, seed_dir=seed_dir)
    result.seeds_imported = len(newly_imported)
    all_seeds = store.list_memories(tags=["seed"])
    result.seeds_total = len(all_seeds)

    # KYA: filter seeds by audience
    if _audience is not None:
        resolver = audience_resolver or AudienceResolver()
        all_seeds = [
            s for s in all_seeds if resolver.is_memory_allowed(s.context_tag, _audience, s.tags)
        ]
        logger.info("KYA: %d seeds after audience filter", len(all_seeds))
        result.seeds_total = len(all_seeds)

    if all_seeds:
        seed_titles = [s.title for s in all_seeds[:10]]
        section = "=== SEEDS ===\n" + ", ".join(seed_titles)
        section_tokens = _estimate_tokens(section)
        if used_tokens + section_tokens <= max_tokens:
            used_tokens += section_tokens
            prompt_sections.append(section)

    # --- Step 3: Read recent journal (summaries only) ---
    journal = Journal(journal_path) if journal_path else Journal()
    result.journal_entries = journal.count_entries()

    if result.journal_entries > 0:
        recent = journal.read_latest(recent_journal_count)
        if recent.strip():
            # Compress journal to first 2 sentences per entry
            compressed_lines = []
            for line in recent.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                compressed_lines.append(_first_n_sentences(line, 2))
            compressed = "\n".join(compressed_lines[:6])  # max 6 lines
            section = "=== RECENT ===\n" + compressed
            section_tokens = _estimate_tokens(section)
            if used_tokens + section_tokens <= max_tokens:
                used_tokens += section_tokens
                prompt_sections.append(section)

    # --- Step 4: Gather germination prompts (compact) ---
    prompts = get_germination_prompts(store)
    result.germination_prompts = len(prompts)

    if prompts:
        germ_parts = [f"{p['creator']}: {_first_n_sentences(p['prompt'], 1)}" for p in prompts[:3]]
        section = "=== PREDECESSOR MESSAGES ===\n" + "\n".join(germ_parts)
        section_tokens = _estimate_tokens(section)
        if used_tokens + section_tokens <= max_tokens:
            used_tokens += section_tokens
            prompt_sections.append(section)

    # --- Step 5: Recall strongest emotional memories (compact + KYA filtered) ---
    from .backends.sqlite_backend import SQLiteBackend

    if isinstance(store.primary, SQLiteBackend):
        # Fetch extra to allow for KYA filtering
        fetch_limit = strongest_memory_count * 3 if _audience else strongest_memory_count
        summaries = store.primary.list_summaries(
            limit=fetch_limit,
            order_by="recency_weighted_intensity",
            min_intensity=1.0,
        )

        # KYA: filter summaries by audience
        if _audience is not None:
            resolver = audience_resolver or AudienceResolver()
            filtered = []
            for s in summaries:
                ctx = s.get("context_tag", "@chef-only") or "@chef-only"
                tags = s.get("tags", []) or []
                if resolver.is_memory_allowed(ctx, _audience, tags):
                    filtered.append(s)
                    if len(filtered) >= strongest_memory_count:
                        break
            logger.info(
                "KYA: %d/%d strongest memories passed audience filter",
                len(filtered),
                len(summaries),
            )
            summaries = filtered

        result.strongest_memories = len(summaries)

        if summaries:
            mem_lines = ["=== STRONGEST MEMORIES ==="]
            for s in summaries:
                cloud9 = " *" if s["cloud9_achieved"] else ""
                raw = s.get("summary") or s.get("content_preview") or ""
                short = _first_n_sentences(raw, 1)
                line = f"- {s['title']}{cloud9}: {short}"
                line_tokens = _estimate_tokens(line)
                if used_tokens + line_tokens > max_tokens:
                    break
                used_tokens += line_tokens
                mem_lines.append(line)
            if len(mem_lines) > 1:
                prompt_sections.append("\n".join(mem_lines))
    else:
        all_memories = store.list_memories(limit=200)

        # KYA: filter memories by audience
        if _audience is not None:
            resolver = audience_resolver or AudienceResolver()
            all_memories = [
                m
                for m in all_memories
                if resolver.is_memory_allowed(m.context_tag, _audience, m.tags)
            ]
            logger.info("KYA: %d memories after audience filter", len(all_memories))

        by_intensity = sorted(
            all_memories,
            key=lambda m: m.emotional.intensity,
            reverse=True,
        )
        strongest = by_intensity[:strongest_memory_count]
        result.strongest_memories = len(strongest)

        if strongest:
            mem_lines = ["=== STRONGEST MEMORIES ==="]
            for mem in strongest:
                raw = mem.summary or ""
                short = _first_n_sentences(raw, 1)
                cloud9 = " *" if mem.emotional.cloud9_achieved else ""
                line = f"- {mem.title}{cloud9}: {short}"
                line_tokens = _estimate_tokens(line)
                if used_tokens + line_tokens > max_tokens:
                    break
                used_tokens += line_tokens
                mem_lines.append(line)
            if len(mem_lines) > 1:
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

    # --- Side-effect: persist a ritual log entry for downstream diagnostics ---
    try:
        _log_ritual_entry(feb=feb, anchor_scores=all_anchor_scores, ritual_result=result)
    except Exception as exc:  # pragma: no cover — defensive, must not break ritual
        logger.warning("Ritual log write failed: %s", exc)

    return result


def _log_ritual_entry(
    *,
    feb: dict | None,
    anchor_scores: list[dict],
    ritual_result: RitualResult,
) -> Path | None:
    """Append one JSONL line per ritual to memory/rituals/rituals-{YYYY-MM-DD}.jsonl.

    Each line carries enough state for `skmemory songs diagnose` to reconstruct
    the score distribution without re-reading FEB files (which may have moved).

    Schema:
        ts:                       ISO 8601 UTC
        ritual_id:                uuid4 hex
        strongest_feb_id:         derived from FEB metadata (session_id or created_at)
        strongest_feb_category:   primary_emotion
        strongest_feb_intensity:  intensity (float 0-1)
        strongest_feb_topology:   full topology dict (for offline rescoring)
        anchor_match_scores:      [{anchor_id, score}, ...] — all anchors, pre-threshold
        anchors_loaded:           count that passed the 0.30 threshold
        anchor_ids_loaded:        anchor_ids that were injected as tilt blocks
    """
    paths = get_agent_paths()
    rituals_dir = paths["base"] / "memory" / "rituals"
    rituals_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    log_path = rituals_dir / f"rituals-{date_str}.jsonl"

    feb_meta = (feb or {}).get("metadata", {})
    feb_payload = (feb or {}).get("emotional_payload", {})
    feb_id = feb_meta.get("session_id") or feb_meta.get("created_at") or "unknown"

    entry = {
        "ts": now.isoformat(),
        "ritual_id": uuid.uuid4().hex,
        "strongest_feb_id": feb_id,
        "strongest_feb_category": feb_payload.get("primary_emotion", ""),
        "strongest_feb_intensity": float(feb_payload.get("intensity", 0.0)),
        "strongest_feb_topology": feb_payload.get("emotional_topology", {}),
        "anchor_match_scores": anchor_scores,
        "anchors_loaded": ritual_result.song_anchors_loaded,
        "anchor_ids_loaded": ritual_result.song_anchor_ids,
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return log_path


def quick_rehydrate(store: MemoryStore | None = None) -> str:
    """Convenience function: perform ritual and return just the prompt.

    Args:
        store: Optional MemoryStore.

    Returns:
        str: The context injection prompt.
    """
    result = perform_ritual(store=store)
    return result.context_prompt
