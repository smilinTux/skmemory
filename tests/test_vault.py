"""Tests for the Memory Vault — at-rest encryption.

Covers encrypt/decrypt roundtrip, tamper detection, file operations,
wrong passphrase handling, and header validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

from skmemory.vault import (
    VAULT_HEADER,
    MemoryVault,
    _derive_key,
    decrypt_memory_store,
    encrypt_memory_store,
)

pytestmark = pytest.mark.skipif(
    not CRYPTO_AVAILABLE,
    reason="cryptography package not installed",
)


@pytest.fixture
def vault() -> MemoryVault:
    """Provide a vault with a test passphrase."""
    return MemoryVault(passphrase="pengu-nation-sovereign")


@pytest.fixture
def sample_json() -> bytes:
    """Sample memory JSON bytes."""
    return b'{"title": "Test Memory", "content": "This is sovereign data."}'


class TestKeyDerivation:
    """Test PBKDF2 key derivation."""

    def test_derive_key_deterministic(self):
        """Same passphrase + salt = same key."""
        salt = b"0" * 16
        k1 = _derive_key("test", salt)
        k2 = _derive_key("test", salt)
        assert k1 == k2

    def test_derive_key_different_salt(self):
        """Different salt = different key."""
        k1 = _derive_key("test", b"a" * 16)
        k2 = _derive_key("test", b"b" * 16)
        assert k1 != k2

    def test_derive_key_length(self):
        """Key is 32 bytes (256 bits)."""
        key = _derive_key("passphrase", b"s" * 16)
        assert len(key) == 32


class TestEncryptDecrypt:
    """Test core encrypt/decrypt operations."""

    def test_roundtrip(self, vault: MemoryVault, sample_json: bytes):
        """Encrypt then decrypt recovers original."""
        encrypted = vault.encrypt(sample_json)
        decrypted = vault.decrypt(encrypted)
        assert decrypted == sample_json

    def test_encrypted_has_header(self, vault: MemoryVault, sample_json: bytes):
        """Encrypted data starts with SKMV1 header."""
        encrypted = vault.encrypt(sample_json)
        assert encrypted[:5] == VAULT_HEADER

    def test_different_nonce_each_time(self, vault: MemoryVault, sample_json: bytes):
        """Same plaintext produces different ciphertext."""
        e1 = vault.encrypt(sample_json)
        e2 = vault.encrypt(sample_json)
        assert e1 != e2

    def test_wrong_passphrase_fails(self, sample_json: bytes):
        """Decryption with wrong passphrase raises."""
        vault1 = MemoryVault(passphrase="correct")
        vault2 = MemoryVault(passphrase="wrong")
        encrypted = vault1.encrypt(sample_json)
        with pytest.raises(Exception):
            vault2.decrypt(encrypted)

    def test_tampered_ciphertext_fails(self, vault: MemoryVault, sample_json: bytes):
        """Altered ciphertext fails authenticated decryption."""
        encrypted = bytearray(vault.encrypt(sample_json))
        encrypted[-10] ^= 0xFF
        with pytest.raises(Exception):
            vault.decrypt(bytes(encrypted))

    def test_bad_header_raises(self, vault: MemoryVault):
        """Non-vault data raises ValueError."""
        with pytest.raises(ValueError, match="bad header"):
            vault.decrypt(b"NOT_A_VAULT_FILE_DATA")

    def test_empty_plaintext(self, vault: MemoryVault):
        """Empty bytes encrypt and decrypt correctly."""
        encrypted = vault.encrypt(b"")
        assert vault.decrypt(encrypted) == b""

    def test_large_plaintext(self, vault: MemoryVault):
        """Large data (1MB) encrypts and decrypts correctly."""
        big = b"A" * (1024 * 1024)
        encrypted = vault.encrypt(big)
        assert vault.decrypt(encrypted) == big


class TestFileOperations:
    """Test file-level encrypt/decrypt."""

    def test_encrypt_file(self, vault: MemoryVault, tmp_path: Path):
        """encrypt_file creates .vault file and removes original."""
        original = tmp_path / "memory.json"
        original.write_bytes(b'{"test": true}')

        vault_path = vault.encrypt_file(original)
        assert vault_path.exists()
        assert vault_path.suffix == ".vault"
        assert not original.exists()

    def test_decrypt_file(self, vault: MemoryVault, tmp_path: Path):
        """decrypt_file restores the original file."""
        original = tmp_path / "memory.json"
        original.write_bytes(b'{"test": true}')

        vault_path = vault.encrypt_file(original)
        restored = vault.decrypt_file(vault_path)

        assert restored.exists()
        assert restored.read_bytes() == b'{"test": true}'
        assert not vault_path.exists()

    def test_encrypt_memory_store(self, tmp_path: Path):
        """encrypt_memory_store encrypts all JSON files."""
        for layer in ("short-term", "long-term"):
            d = tmp_path / layer
            d.mkdir()
            (d / "mem1.json").write_bytes(b'{"id": 1}')
            (d / "mem2.json").write_bytes(b'{"id": 2}')

        count = encrypt_memory_store(tmp_path, "test-pass")
        assert count == 4
        assert len(list(tmp_path.rglob("*.vault"))) == 4
        assert len(list(tmp_path.rglob("*.json"))) == 0

    def test_decrypt_memory_store(self, tmp_path: Path):
        """decrypt_memory_store decrypts all vault files."""
        d = tmp_path / "memories"
        d.mkdir()
        vault = MemoryVault("test-pass")
        for i in range(3):
            f = d / f"mem{i}.json"
            f.write_bytes(f'{{"id": {i}}}'.encode())
            vault.encrypt_file(f)

        count = decrypt_memory_store(d, "test-pass")
        assert count == 3
        assert len(list(d.rglob("*.json"))) == 3


class TestIsEncrypted:
    """Test encrypted file detection."""

    def test_encrypted_file_detected(self, vault: MemoryVault, tmp_path: Path):
        """is_encrypted returns True for vault files."""
        f = tmp_path / "test.json"
        f.write_bytes(b'{"data": 1}')
        vf = vault.encrypt_file(f)
        assert vault.is_encrypted(vf) is True

    def test_plain_file_not_detected(self, vault: MemoryVault, tmp_path: Path):
        """is_encrypted returns False for plain JSON."""
        f = tmp_path / "plain.json"
        f.write_bytes(b'{"data": 1}')
        assert vault.is_encrypted(f) is False
