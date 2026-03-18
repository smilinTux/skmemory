"""Tests for cross-agent memory sharing."""

from __future__ import annotations

from pathlib import Path

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skmemory.models import EmotionalSnapshot, MemoryLayer
from skmemory.sharing import MemorySharer, ShareBundle, ShareFilter
from skmemory.store import MemoryStore

PASSPHRASE = "share-test-2026"


def _generate_keypair() -> tuple[str, str]:
    """Generate a test RSA-2048 keypair."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("ShareTest", email="share@test.io")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign, KeyFlags.Certify},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )
    enc_sub = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_subkey(
        enc_sub,
        usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
    )
    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def recipient_keys() -> tuple[str, str]:
    """Recipient keypair for encryption tests."""
    return _generate_keypair()


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """Fresh MemoryStore with test data."""
    from skmemory.backends.file_backend import FileBackend

    backend = FileBackend(base_path=tmp_path / "memories")
    s = MemoryStore(primary=backend)

    s.snapshot(
        title="Project breakthrough",
        content="Achieved full sovereign messaging",
        tags=["project", "milestone"],
        layer=MemoryLayer.MID,
        emotional=EmotionalSnapshot(intensity=8.0, valence=0.9, labels=["pride"]),
    )
    s.snapshot(
        title="Daily standup",
        content="Routine sync meeting notes",
        tags=["daily", "routine"],
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=2.0),
    )
    s.snapshot(
        title="Secret key rotation",
        content="Rotated PGP keys for all agents",
        tags=["security", "private"],
        layer=MemoryLayer.LONG,
        emotional=EmotionalSnapshot(intensity=5.0),
    )
    return s


@pytest.fixture()
def sharer(store: MemoryStore) -> MemorySharer:
    """MemorySharer wired to the test store."""
    return MemorySharer(store=store, identity="capauth:alice@skworld.io")


@pytest.fixture()
def receiver_store(tmp_path: Path) -> MemoryStore:
    """Separate MemoryStore for the receiving agent."""
    from skmemory.backends.file_backend import FileBackend

    backend = FileBackend(base_path=tmp_path / "receiver-memories")
    return MemoryStore(primary=backend)


class TestShareFilter:
    """Tests for ShareFilter behavior."""

    def test_empty_filter(self) -> None:
        """Empty filter reports as empty."""
        sf = ShareFilter()
        assert sf.is_empty() is True

    def test_filter_with_tags(self) -> None:
        """Filter with tags is not empty."""
        sf = ShareFilter(tags=["project"])
        assert sf.is_empty() is False

    def test_filter_with_ids(self) -> None:
        """Filter with memory_ids is not empty."""
        sf = ShareFilter(memory_ids=["abc"])
        assert sf.is_empty() is False


class TestExportMemories:
    """Tests for memory export/selection."""

    def test_export_by_tags(self, sharer: MemorySharer) -> None:
        """Export selects memories matching tags."""
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf, recipient="capauth:bob@skworld.io")

        assert bundle.memory_count >= 1
        assert bundle.sharer == "capauth:alice@skworld.io"
        assert bundle.recipient == "capauth:bob@skworld.io"
        assert bundle.checksum != ""

    def test_export_by_layer(self, sharer: MemorySharer) -> None:
        """Export selects memories in specified layers."""
        sf = ShareFilter(layers=[MemoryLayer.MID])
        bundle = sharer.export_memories(sf)
        assert bundle.memory_count >= 1

    def test_export_with_intensity_filter(self, sharer: MemorySharer) -> None:
        """Intensity filter excludes low-intensity memories."""
        sf = ShareFilter(tags=["daily", "project", "security"], min_intensity=4.0)
        bundle = sharer.export_memories(sf)
        for mem in bundle.memories:
            assert mem.get("emotional", {}).get("intensity", 0) >= 4.0

    def test_export_with_exclude_tags(self, sharer: MemorySharer) -> None:
        """Exclude tags prevent certain memories from being shared."""
        sf = ShareFilter(
            tags=["project", "security"],
            exclude_tags=["private"],
        )
        bundle = sharer.export_memories(sf)
        for mem in bundle.memories:
            assert "private" not in mem.get("tags", [])

    def test_export_empty_filter_raises(self, sharer: MemorySharer) -> None:
        """Empty filter raises ValueError for safety."""
        sf = ShareFilter()
        with pytest.raises(ValueError, match="Explicit criteria required"):
            sharer.export_memories(sf)

    def test_export_max_count(self, sharer: MemorySharer) -> None:
        """Max count limits the number of exported memories."""
        sf = ShareFilter(tags=["project", "daily", "security"], max_count=1)
        bundle = sharer.export_memories(sf)
        assert bundle.memory_count <= 1


class TestImportBundle:
    """Tests for memory import."""

    def test_import_adds_provenance(
        self,
        sharer: MemorySharer,
        receiver_store: MemoryStore,
    ) -> None:
        """Imported memories have provenance tags."""
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf)

        receiver = MemorySharer(store=receiver_store, identity="capauth:bob@skworld.io")
        result = receiver.import_bundle(bundle)

        assert result["imported"] >= 1
        assert result["errors"] == 0

        imported = receiver_store.list_memories(tags=["shared"])
        assert len(imported) >= 1
        assert any("shared:from:capauth:alice@skworld.io" in m.tags for m in imported)

    def test_import_untrusted_skips(
        self, sharer: MemorySharer, receiver_store: MemoryStore
    ) -> None:
        """Untrusted sharer is rejected."""
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf)

        receiver = MemorySharer(store=receiver_store)
        result = receiver.import_bundle(bundle, trust_sharer=False)
        assert result["imported"] == 0
        assert result["skipped"] == bundle.memory_count

    def test_import_checksum_mismatch(self, receiver_store: MemoryStore) -> None:
        """Tampered bundle fails checksum verification."""
        bundle = ShareBundle(
            sharer="evil",
            memories=[{"title": "fake", "content": "hacked"}],
            memory_count=1,
            checksum="wrong_checksum",
        )
        receiver = MemorySharer(store=receiver_store)
        result = receiver.import_bundle(bundle)
        assert result["errors"] == 1


class TestEncryptDecrypt:
    """Tests for PGP encryption of share bundles."""

    def test_encrypt_decrypt_roundtrip(
        self,
        sharer: MemorySharer,
        recipient_keys: tuple[str, str],
    ) -> None:
        """Bundle encrypted for recipient can be decrypted."""
        priv, pub = recipient_keys
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf)

        encrypted = sharer.encrypt_bundle(bundle, pub)
        assert encrypted.encrypted is True
        assert len(encrypted.memories) == 1
        assert "ciphertext" in encrypted.memories[0]

        receiver = MemorySharer(store=sharer._store)
        decrypted = receiver.decrypt_bundle(encrypted, priv, PASSPHRASE)
        assert decrypted.encrypted is False
        assert decrypted.memory_count == bundle.memory_count

    def test_decrypt_plaintext_is_noop(self, sharer: MemorySharer) -> None:
        """Decrypting a non-encrypted bundle returns it unchanged."""
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf)

        result = sharer.decrypt_bundle(bundle, "key", "pass")
        assert result.encrypted is False


class TestBundlePersistence:
    """Tests for save/load bundle files."""

    def test_save_and_load(self, sharer: MemorySharer, tmp_path: Path) -> None:
        """Bundle survives save/load roundtrip."""
        sf = ShareFilter(tags=["project"])
        bundle = sharer.export_memories(sf)

        filepath = tmp_path / "bundle.json"
        sharer.save_bundle(bundle, filepath)

        loaded = MemorySharer.load_bundle(filepath)
        assert loaded.bundle_id == bundle.bundle_id
        assert loaded.memory_count == bundle.memory_count
        assert loaded.checksum == bundle.checksum
