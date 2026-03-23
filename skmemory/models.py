"""
Memory data models for SKMemory.

The core abstraction: a Memory is a snapshot of a moment -- a polaroid
that captures not just what happened, but what it felt like.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MemoryLayer(str, Enum):
    """Which persistence tier this memory belongs to.

    Short-term: session-scoped, ephemeral, high detail.
    Mid-term: project-scoped, summarized, kept across sessions.
    Long-term: identity-level patterns, permanent.
    """

    SHORT = "short-term"
    MID = "mid-term"
    LONG = "long-term"


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


class Memory(BaseModel):
    """A single memory unit -- one polaroid in the album.

    Contains the content, emotional context, metadata for search,
    and relationship links to other memories.
    """

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
        description="WHY this memory was stored — the purpose, not just the content. "
        "Inspired by Jonathan Clements' AMK (Adaptive Memory Kernel).",
    )
    integrity_hash: str = Field(
        default="",
        description="SHA-256 hash of content at write time for tamper detection. "
        "A memory that can prove it hasn't been altered is a memory you can trust.",
    )

    metadata: dict[str, Any] = Field(default_factory=dict)

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
        data = self.model_dump()
        data["id"] = str(uuid.uuid4())
        data["layer"] = target
        data["parent_id"] = self.id
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        if summary:
            data["summary"] = summary
        return Memory(**data)


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
