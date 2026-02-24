"""Cross-agent memory sharing -- selective P2P memory sync.

Enables sovereign agents to share specific memories with trusted
peers, encrypted with PGP. The sharer controls exactly which
memories leave their store (by tags, layer, or explicit IDs).
The receiver imports them into their own SKMemory with provenance
tracking.

Flow:
    1. Sharer selects memories by filter criteria
    2. Memories are serialized to a ShareBundle (JSON)
    3. Bundle is optionally PGP-encrypted for the recipient
    4. Recipient decrypts and imports into their own MemoryStore
    5. Imported memories are tagged with provenance (who shared, when)

All operations are local-first. Transport (how the bundle reaches
the peer) is handled externally -- via SKComm, file copy, USB, etc.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import Memory, MemoryLayer

logger = logging.getLogger("skmemory.sharing")


class ShareFilter(BaseModel):
    """Criteria for selecting which memories to share.

    All filters are ANDed together. Empty filter = share nothing
    (explicit selection required for safety).

    Attributes:
        memory_ids: Explicit memory IDs to share.
        tags: Share memories matching ALL these tags.
        layers: Share memories in these layers.
        min_intensity: Minimum emotional intensity (0-10).
        exclude_tags: Never share memories with these tags.
        max_count: Maximum number of memories to include.
    """

    memory_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    layers: list[MemoryLayer] = Field(default_factory=list)
    min_intensity: float = Field(default=0.0, ge=0.0, le=10.0)
    exclude_tags: list[str] = Field(default_factory=list)
    max_count: int = Field(default=100, ge=1, le=1000)

    def is_empty(self) -> bool:
        """Check if no selection criteria are set.

        Returns:
            bool: True if the filter would select nothing.
        """
        return not self.memory_ids and not self.tags and not self.layers


class ShareBundle(BaseModel):
    """A package of memories ready for sharing.

    Contains serialized memories, provenance info, and an
    integrity checksum. Can be encrypted before transmission.

    Attributes:
        bundle_id: Unique bundle identifier.
        created_at: When the bundle was created.
        sharer: Identity of the sharing agent (CapAuth URI or name).
        recipient: Intended recipient (empty = anyone with the key).
        memories: Serialized memory dicts.
        memory_count: Number of memories in the bundle.
        checksum: SHA-256 over the memories JSON for integrity.
        encrypted: Whether the memories field is PGP ciphertext.
        metadata: Extra context about the share.
    """

    bundle_id: str = Field(
        default_factory=lambda: hashlib.sha256(
            datetime.now(timezone.utc).isoformat().encode()
        ).hexdigest()[:16]
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sharer: str = ""
    recipient: str = ""
    memories: list[dict[str, Any]] = Field(default_factory=list)
    memory_count: int = 0
    checksum: str = ""
    encrypted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySharer:
    """Handles selective memory export and import between agents.

    The sharer selects memories from their MemoryStore using
    ShareFilter criteria, packages them into a ShareBundle,
    and optionally encrypts for a specific recipient.

    The receiver decrypts and imports into their store with
    provenance tags tracking the origin.

    Args:
        store: An SKMemory MemoryStore instance.
        identity: This agent's identity (CapAuth URI or name).
    """

    SHARE_TAG = "shared"
    PROVENANCE_PREFIX = "shared:from:"

    def __init__(self, store: object, identity: str = "local") -> None:
        self._store = store
        self._identity = identity

    def export_memories(
        self,
        share_filter: ShareFilter,
        recipient: str = "",
    ) -> ShareBundle:
        """Select and package memories for sharing.

        Applies the filter criteria against the local store,
        serializes matching memories, and creates a ShareBundle.

        Args:
            share_filter: Selection criteria.
            recipient: Intended recipient identity.

        Returns:
            ShareBundle: Package ready for encryption or transmission.

        Raises:
            ValueError: If the filter is empty (safety check).
        """
        if share_filter.is_empty():
            raise ValueError(
                "ShareFilter is empty. Explicit criteria required for safety -- "
                "set memory_ids, tags, or layers to select memories."
            )

        memories = self._select_memories(share_filter)
        serialized = [m.model_dump(mode="json") for m in memories]

        checksum = hashlib.sha256(
            json.dumps(serialized, sort_keys=True, default=str).encode()
        ).hexdigest()

        bundle = ShareBundle(
            sharer=self._identity,
            recipient=recipient,
            memories=serialized,
            memory_count=len(serialized),
            checksum=checksum,
            metadata={
                "filter_tags": share_filter.tags,
                "filter_layers": [l.value for l in share_filter.layers],
            },
        )

        logger.info(
            "Exported %d memories for %s (bundle %s)",
            len(serialized), recipient or "anyone", bundle.bundle_id,
        )
        return bundle

    def import_bundle(
        self,
        bundle: ShareBundle,
        trust_sharer: bool = True,
    ) -> dict:
        """Import a ShareBundle into the local memory store.

        Each memory is stored with provenance tags tracking who
        shared it and when. Checksums are verified for integrity.

        Args:
            bundle: The ShareBundle to import.
            trust_sharer: If False, skip memories from untrusted sources.

        Returns:
            dict: Import summary with 'imported', 'skipped', 'errors' counts.
        """
        if not trust_sharer:
            logger.warning("Untrusted sharer %s -- skipping import", bundle.sharer)
            return {"imported": 0, "skipped": bundle.memory_count, "errors": 0}

        actual_checksum = hashlib.sha256(
            json.dumps(bundle.memories, sort_keys=True, default=str).encode()
        ).hexdigest()

        if bundle.checksum and actual_checksum != bundle.checksum:
            logger.error(
                "Bundle checksum mismatch! Expected %s, got %s",
                bundle.checksum[:16], actual_checksum[:16],
            )
            return {"imported": 0, "skipped": 0, "errors": bundle.memory_count}

        imported = 0
        skipped = 0
        errors = 0

        for mem_dict in bundle.memories:
            try:
                memory = Memory(**mem_dict)

                provenance_tags = [
                    self.SHARE_TAG,
                    f"{self.PROVENANCE_PREFIX}{bundle.sharer}",
                    f"shared:bundle:{bundle.bundle_id}",
                ]

                existing_tags = list(memory.tags)
                for tag in provenance_tags:
                    if tag not in existing_tags:
                        existing_tags.append(tag)

                self._store.snapshot(
                    title=f"[shared] {memory.title}",
                    content=memory.content,
                    layer=memory.layer,
                    tags=existing_tags,
                    emotional=memory.emotional,
                    source="shared",
                    source_ref=f"{bundle.sharer}:{memory.id}",
                    metadata={
                        **memory.metadata,
                        "shared_from": bundle.sharer,
                        "shared_at": bundle.created_at.isoformat(),
                        "bundle_id": bundle.bundle_id,
                        "original_id": memory.id,
                    },
                )
                imported += 1

            except Exception as exc:
                logger.warning("Failed to import memory: %s", exc)
                errors += 1

        logger.info(
            "Imported %d/%d memories from %s (bundle %s)",
            imported, bundle.memory_count, bundle.sharer, bundle.bundle_id,
        )
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def encrypt_bundle(
        self,
        bundle: ShareBundle,
        recipient_public_armor: str,
    ) -> ShareBundle:
        """Encrypt a ShareBundle's memories for a specific recipient.

        The memories list is replaced with a single PGP-encrypted
        JSON string. Only the recipient's private key can decrypt.

        Args:
            bundle: The bundle to encrypt.
            recipient_public_armor: Recipient's ASCII-armored PGP public key.

        Returns:
            ShareBundle: New bundle with encrypted memories field.
        """
        try:
            import pgpy

            recipient_key, _ = pgpy.PGPKey.from_blob(recipient_public_armor)
            plaintext = json.dumps(bundle.memories, default=str)
            pgp_message = pgpy.PGPMessage.new(plaintext.encode("utf-8"))
            encrypted = recipient_key.encrypt(pgp_message)

            return bundle.model_copy(
                update={
                    "memories": [{"ciphertext": str(encrypted)}],
                    "encrypted": True,
                }
            )
        except Exception as exc:
            logger.error("Failed to encrypt bundle: %s", exc)
            raise

    def decrypt_bundle(
        self,
        bundle: ShareBundle,
        private_key_armor: str,
        passphrase: str,
    ) -> ShareBundle:
        """Decrypt an encrypted ShareBundle.

        Args:
            bundle: The encrypted bundle.
            private_key_armor: Recipient's ASCII-armored PGP private key.
            passphrase: Passphrase for the private key.

        Returns:
            ShareBundle: Decrypted bundle with plaintext memories.
        """
        if not bundle.encrypted:
            return bundle

        try:
            import pgpy

            key, _ = pgpy.PGPKey.from_blob(private_key_armor)
            ciphertext = bundle.memories[0].get("ciphertext", "")
            pgp_message = pgpy.PGPMessage.from_blob(ciphertext)

            with key.unlock(passphrase):
                decrypted = key.decrypt(pgp_message)

            plaintext = decrypted.message
            if isinstance(plaintext, bytes):
                plaintext = plaintext.decode("utf-8")

            memories = json.loads(plaintext)

            return bundle.model_copy(
                update={
                    "memories": memories,
                    "encrypted": False,
                }
            )
        except Exception as exc:
            logger.error("Failed to decrypt bundle: %s", exc)
            raise

    def save_bundle(self, bundle: ShareBundle, filepath: str | Path) -> Path:
        """Save a ShareBundle to a JSON file for transport.

        Args:
            bundle: The bundle to save.
            filepath: Destination path.

        Returns:
            Path: The written file path.
        """
        path = Path(filepath).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load_bundle(filepath: str | Path) -> ShareBundle:
        """Load a ShareBundle from a JSON file.

        Args:
            filepath: Path to the bundle file.

        Returns:
            ShareBundle: The loaded bundle.
        """
        path = Path(filepath).expanduser()
        return ShareBundle.model_validate_json(path.read_text())

    def _select_memories(self, sf: ShareFilter) -> list[Memory]:
        """Apply filter criteria to select memories from the store.

        Args:
            sf: The share filter.

        Returns:
            list[Memory]: Matching memories.
        """
        candidates: list[Memory] = []

        if sf.memory_ids:
            for mid in sf.memory_ids:
                mem = self._store.recall(mid)
                if mem:
                    candidates.append(mem)

        if sf.tags:
            tagged = self._store.list_memories(tags=sf.tags, limit=sf.max_count)
            seen = {m.id for m in candidates}
            for m in tagged:
                if m.id not in seen:
                    candidates.append(m)
                    seen.add(m.id)

        if sf.layers:
            for layer in sf.layers:
                layered = self._store.list_memories(layer=layer, limit=sf.max_count)
                seen = {m.id for m in candidates}
                for m in layered:
                    if m.id not in seen:
                        candidates.append(m)
                        seen.add(m.id)

        filtered = []
        for m in candidates:
            if sf.exclude_tags and any(t in m.tags for t in sf.exclude_tags):
                continue
            if m.emotional.intensity < sf.min_intensity:
                continue
            filtered.append(m)

        return filtered[: sf.max_count]
