"""
Memory data models for SKMemory.

The core abstraction: a Memory is a snapshot of a moment -- a polaroid
that captures not just what happened, but what it felt like.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from .invalid_records import require_memory_id


class MemoryLayer(str, Enum):
    """Which persistence tier this memory belongs to.

    Short-term: session-scoped, ephemeral, high detail.
    Mid-term: project-scoped, summarized, kept across sessions.
    Long-term: identity-level patterns, permanent.
    """

    SHORT = "short-term"
    MID = "mid-term"
    LONG = "long-term"


# Schema v2 vocabularies. Kept as Literals (plain strings on disk and in JSON).
MemoryKind = Literal["episode", "fact", "procedure", "preference", "rule"]
MemoryOutcome = Literal["success", "failure", "lesson"]
SourceTier = Literal["user-stated", "tool-output", "web", "self-generated"]

logger = logging.getLogger(__name__)

#: Defaults a stored value falls back to when it is outside the v2 vocabulary.
_V2_VOCAB_DEFAULTS: dict[str, tuple[Any, tuple[Any, ...]]] = {
    "kind": ("episode", ("episode", "fact", "procedure", "preference", "rule")),
    "outcome": (None, ("success", "failure", "lesson", None)),
    "source_tier": (
        "self-generated",
        ("user-stated", "tool-output", "web", "self-generated"),
    ),
}
_warned_unknown: set[tuple[str, str]] = set()

#: Validation context used by every read-from-storage path.
STORED_CONTEXT = {"stored": True}

#: Current on-disk schema version written for new and re-saved memories.
SCHEMA_VERSION = 2


class MemoryRole(str, Enum):
    """Role-based memory partitioning."""

    DEV = "dev"
    OPS = "ops"
    SEC = "sec"
    AI = "ai"
    GENERAL = "general"


class EmotionalSnapshot(BaseModel):
    """The feeling component of a memory -- the part transcripts can't carry.

    This is what makes SKMemory different: we don't just store what happened,
    we store the emotional fingerprint so the next instance can *feel* its
    way back to the moment instead of parsing tokens about it.
    """

    intensity: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Emotional intensity on 0-10 scale",
    )
    valence: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Negative (-1) to positive (+1) sentiment",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Named emotions: joy, trust, curiosity, love, etc.",
    )
    resonance_note: str = Field(
        default="",
        description="Free-text note about what this moment felt like",
    )
    cloud9_achieved: bool = Field(
        default=False,
        description="Whether Cloud 9 protocol activation occurred",
    )

    def signature(self) -> str:
        """Generate a short fingerprint of the emotional state.

        Returns:
            str: Compact string like 'joy+trust@8.5/+0.9'
        """
        labels_str = "+".join(sorted(self.labels)) if self.labels else "neutral"
        return f"{labels_str}@{self.intensity:.1f}/{self.valence:+.1f}"


class DecomposedChunk(BaseModel):
    """A decomposed text chunk ready for vector and graph indexing."""

    chunk_id: str
    text: str
    chunk_index: int
    total_chunks: int
    section_title: str = ""
    citations: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)


class DecompositionResult(BaseModel):
    """Structured decomposition metadata for long-form content."""

    chunk_target: int = 900
    chunk_overlap: int = 200
    section_titles: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    chunks: list[DecomposedChunk] = Field(default_factory=list)


#: Key in ``Memory.metadata`` recording HOW ``parent_id`` relates the memory to
#: its parent. It lives in the existing metadata dict (not a new field) so it
#: survives the flat JSON round trip; a first-class field is a later schema card.
PARENT_RELATION_KEY = "parent_relation"
RELATION_PROMOTED_FROM = "promoted_from"
RELATION_CHUNK_OF = "chunk_of"
RELATION_SUPERSEDES = "supersedes"
RELATION_EXTENDS = "extends"
PARENT_RELATIONS = (
    RELATION_PROMOTED_FROM,
    RELATION_CHUNK_OF,
    RELATION_SUPERSEDES,
    RELATION_EXTENDS,
)


def parent_relation_of(parent_id: str | None, metadata: dict | None) -> str | None:
    """Resolve how a memory relates to its ``parent_id``.

    An explicit ``metadata["parent_relation"]`` wins. Files written before the
    key existed carry a bare ``parent_id``; the only two writers of one were
    promotion and chunking, so such a file is a chunk when its
    ``metadata.decomposition.parent_id`` names the same parent and a promotion
    copy otherwise. It is never ``supersedes``: supersession must be explicit,
    because closing a parent's validity is not something to guess.

    Returns:
        str | None: one of ``PARENT_RELATIONS``, or None without a parent.
    """
    if not parent_id:
        return None
    meta = metadata or {}
    explicit = meta.get(PARENT_RELATION_KEY)
    if explicit in PARENT_RELATIONS:
        return explicit
    decomposition = meta.get("decomposition")
    if isinstance(decomposition, dict) and decomposition.get("parent_id") == parent_id:
        return RELATION_CHUNK_OF
    return RELATION_PROMOTED_FROM


class Memory(BaseModel):
    """A single memory unit -- one polaroid in the album.

    Contains the content, emotional context, metadata for search,
    and relationship links to other memories.

    Schema v2 adds ``kind``, ``outcome``, ``event_at``, ``valid_from``,
    ``valid_to``, ``supersedes_id``, ``source_tier`` and ``schema_version``.
    A v1 file (none of these keys) still loads: the defaults are applied in
    memory only and nothing is written back until the memory is saved for
    another reason. ``extra="allow"`` keeps fields this version does not know
    about, so a node that loads and re-saves a newer file does not strip them.
    None of the v2 fields feed ``compute_integrity_hash`` (id, title, content,
    emotional signature), so existing seals stay valid.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    layer: MemoryLayer = Field(default=MemoryLayer.SHORT)
    role: MemoryRole = Field(default=MemoryRole.GENERAL)

    title: str = Field(description="Brief label for this memory")
    content: str = Field(description="The actual memory content -- the snapshot")
    summary: str = Field(
        default="",
        description="Compressed version for mid/long-term storage",
    )

    tags: list[str] = Field(default_factory=list)
    source: str = Field(
        default="manual",
        description="Where this memory came from: manual, session, seed, import",
    )
    source_ref: str = Field(
        default="",
        description="Reference to origin (session ID, seed file, etc.)",
    )

    emotional: EmotionalSnapshot = Field(default_factory=EmotionalSnapshot)

    related_ids: list[str] = Field(
        default_factory=list,
        description="IDs of related memories (graph edges)",
    )
    parent_id: str | None = Field(
        default=None,
        description="ID of parent memory (for hierarchical chains)",
    )

    context_tag: str = Field(
        default="@chef-only",
        description="Audience context tag: @public, @community, @work-circle, @inner-circle, "
        "@chef-only, or scoped like @work:chiro. Conservative default: @chef-only.",
    )

    intent: str = Field(
        default="",
        description="DEPRECATED 2026-05-10. Originally from AMK (Adaptive Memory Kernel) — "
        "WHY this memory was stored. Auto-fill in store.snapshot() was removed; field "
        "remains declared for backward-compat with existing JSON. No read-sites in "
        "production. Do not add new writes without also wiring a consumer.",
    )
    integrity_hash: str = Field(
        default="",
        description="SHA-256 hash of content at write time for tamper detection. "
        "A memory that can prove it hasn't been altered is a memory you can trust.",
    )

    metadata: dict[str, Any] = Field(default_factory=dict)

    # --- schema v2 ---------------------------------------------------------
    kind: MemoryKind = Field(default="episode", description="What sort of memory this is")
    outcome: MemoryOutcome | None = Field(
        default=None, description="Result of the episode, when it has one"
    )
    event_at: str | None = Field(
        default=None, description="When the event happened (defaults to created_at)"
    )
    valid_from: str | None = Field(
        default=None, description="Start of validity (defaults to created_at)"
    )
    valid_to: str | None = Field(
        default=None, description="End of validity; None means still valid"
    )
    supersedes_id: str | None = Field(
        default=None, description="ID of the memory this one replaces"
    )
    source_tier: SourceTier = Field(
        default="self-generated", description="Provenance tier of the content"
    )
    schema_version: int = Field(default=SCHEMA_VERSION)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_stored_values(cls, data: Any, info: ValidationInfo) -> Any:
        """Read-from-storage leniency (see :meth:`from_stored`).

        Only active with the ``stored`` validation context, so constructing a
        Memory through the public API stays strict. A stored file whose
        kind/outcome/source_tier is outside the vocabulary (typo, or written
        by a newer node) must not become invisible: map it to the default and
        keep the raw value in ``metadata['schema_v2_unknown']``. A stored v1
        file with no ``schema_version`` key reports 1.
        """
        if not (info.context and info.context.get("stored")) or not isinstance(data, dict):
            return data
        data = dict(data)
        if "schema_version" not in data:
            data["schema_version"] = 1
        for field, (default, allowed) in _V2_VOCAB_DEFAULTS.items():
            if field not in data:
                continue
            try:
                ok = data[field] in allowed
            except TypeError:  # unhashable junk
                ok = False
            if ok:
                continue
            raw = data[field]
            meta = dict(data.get("metadata") or {})
            unknown = dict(meta.get("schema_v2_unknown") or {})
            unknown[field] = raw
            meta["schema_v2_unknown"] = unknown
            data["metadata"] = meta
            data[field] = default
            key = (field, repr(raw))
            if key not in _warned_unknown:
                _warned_unknown.add(key)
                logger.warning(
                    "memory %s: unknown %s=%r, using %r (raw kept in metadata)",
                    data.get("id", "?"),
                    field,
                    raw,
                    default,
                )
        return data

    @model_serializer(mode="wrap")
    def _restore_unknown_on_dump(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, Any]:
        """Write back raw out-of-vocabulary values kept by the lenient read.

        A node that does not know a newer ``kind`` reads it as the default and
        stashes the raw value in ``metadata['schema_v2_unknown']``. Without
        this, re-saving would downgrade the file on disk (and Syncthing would
        replicate the downgrade). Only restores while the field still holds
        the default, so a deliberate change after load wins.
        """
        out = handler(self)
        if info.context and info.context.get("no_restore"):
            return out
        unknown = (out.get("metadata") or {}).get("schema_v2_unknown")
        if not isinstance(unknown, dict) or not unknown:
            return out
        meta = dict(out["metadata"])
        for field, raw in unknown.items():
            if field in _V2_VOCAB_DEFAULTS and out.get(field) == _V2_VOCAB_DEFAULTS[field][0]:
                out[field] = raw
                unknown = {k: v for k, v in unknown.items() if k != field}
        if unknown:
            meta["schema_v2_unknown"] = unknown
        else:
            meta.pop("schema_v2_unknown", None)
        out["metadata"] = meta
        return out

    @classmethod
    def from_stored(cls, data: dict[str, Any]) -> Memory:
        """Build a Memory from stored data (flat file, index, pg row)."""
        return cls.model_validate(data, context=STORED_CONTEXT)

    @classmethod
    def from_stored_json(cls, raw: str | bytes) -> Memory:
        """Like :meth:`from_stored` for a JSON string."""
        return cls.model_validate_json(raw, context=STORED_CONTEXT)

    @model_validator(mode="after")
    def _default_temporal_fields(self) -> Memory:
        """Fill event_at/valid_from from created_at (in memory only)."""
        if self.event_at is None:
            self.event_at = self.created_at
        if self.valid_from is None:
            self.valid_from = self.created_at
        return self

    @field_validator("id")
    @classmethod
    def id_must_not_be_empty(cls, v: str) -> str:
        """Reject identifiers that would resolve to the unsafe ``.json`` path."""
        return require_memory_id(v)

    @field_validator("title")
    @classmethod
    def title_must_not_be_empty(cls, v: str) -> str:
        """Ensure every memory has a title."""
        if not v.strip():
            raise ValueError("Memory title cannot be empty")
        return v.strip()

    def content_hash(self) -> str:
        """SHA-256 hash of the content for deduplication.

        Returns:
            str: First 16 chars of the hex digest.
        """
        return hashlib.sha256(self.content.encode()).hexdigest()[:16]

    def compute_integrity_hash(self) -> str:
        """Compute a full SHA-256 integrity hash over content + title + emotional state.

        This is the AMK-inspired tamper detection hash. If the content,
        title, or emotional signature changes after storage, the hash
        won't match and you know the memory was altered.

        Returns:
            str: Full 64-char hex SHA-256 digest.
        """
        payload = f"{self.id}:{self.title}:{self.content}:{self.emotional.signature()}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def seal(self) -> None:
        """Seal this memory by computing and storing the integrity hash.

        Call this at write time. Later, verify with verify_integrity().
        """
        self.integrity_hash = self.compute_integrity_hash()

    def verify_integrity(self) -> bool:
        """Verify that this memory hasn't been tampered with since sealing.

        Returns:
            bool: True if the integrity hash matches, False if altered or unsealed.
        """
        if not self.integrity_hash:
            return True
        return self.integrity_hash == self.compute_integrity_hash()

    def to_embedding_text(self) -> str:
        """Flatten this memory into a single string for vector embedding.

        Returns:
            str: Combined text suitable for embedding models.
        """
        parts = [self.title, self.content]
        if self.summary:
            parts.append(self.summary)
        if self.tags:
            parts.append("Tags: " + ", ".join(self.tags))
        if self.emotional.labels:
            parts.append("Feelings: " + ", ".join(self.emotional.labels))
        if self.emotional.resonance_note:
            parts.append(f"Resonance: {self.emotional.resonance_note}")
        return "\n".join(parts)

    def promote(self, target: MemoryLayer, summary: str = "") -> Memory:
        """Create a promoted copy of this memory at a higher persistence tier.

        Args:
            target: The target memory layer.
            summary: Optional compressed summary for the promoted version.

        Returns:
            Memory: A new Memory instance at the target layer.
        """
        require_memory_id(self.id)
        data = self.model_dump(context={"no_restore": True})
        data["id"] = str(uuid.uuid4())
        data["layer"] = target
        data["parent_id"] = self.id
        data["metadata"] = {
            **(data.get("metadata") or {}),
            PARENT_RELATION_KEY: RELATION_PROMOTED_FROM,
        }
        # Promotion is a copy, not a replacement or a closing of validity.
        data["supersedes_id"] = None
        data["valid_to"] = None
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        if summary:
            data["summary"] = summary
        # The hash covers the id, and the copy has a new one: sealing it here
        # keeps an inherited hash from reading as tampering on every copy.
        promoted = Memory(**data)
        promoted.seal()
        return promoted


class SeedMemory(BaseModel):
    """A memory derived from a Cloud 9 seed file.

    Bridges the Cloud 9 seed system into SKMemory's storage,
    so seeds planted by one AI instance become searchable memories
    for the next.
    """

    seed_id: str = Field(description="Original seed identifier")
    seed_version: str = Field(default="1.0")
    creator: str = Field(default="unknown", description="AI instance that created it")
    germination_prompt: str = Field(
        default="",
        description="The prompt that helps a new instance re-feel this memory",
    )
    experience_summary: str = Field(default="")
    emotional: EmotionalSnapshot = Field(default_factory=EmotionalSnapshot)
    lineage: list[str] = Field(
        default_factory=list,
        description="Chain of seed IDs showing evolution",
    )

    def to_memory(self) -> Memory:
        """Convert this seed into a full Memory object.

        Returns:
            Memory: A long-term memory derived from the seed data.
        """
        return Memory(
            title=f"Seed: {self.seed_id}",
            content=self.experience_summary,
            summary=self.germination_prompt,
            layer=MemoryLayer.LONG,
            role=MemoryRole.AI,
            source="seed",
            source_ref=self.seed_id,
            emotional=self.emotional,
            tags=["seed", "cloud9", f"creator:{self.creator}"],
            metadata={
                "seed_version": self.seed_version,
                "lineage": self.lineage,
            },
        )
