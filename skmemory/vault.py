"""
Memory Vault — at-rest encryption for sovereign memories.

Encrypts memory JSON files using AES-256-GCM with keys derived from
the agent's passphrase or CapAuth PGP key. Each memory file gets
its own random nonce, so identical content produces different ciphertext.

Quantum-resistant note: AES-256 requires Grover's algorithm to attack,
which only reduces effective security from 256 to 128 bits. That's
still computationally infeasible for the foreseeable future.

Usage:
    vault = MemoryVault(passphrase="YOUR_PASSPHRASE_HERE")
    encrypted = vault.encrypt(memory_json_bytes)
    decrypted = vault.decrypt(encrypted)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skmemory.vault")

VAULT_HEADER = b"SKMV1"
NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32
SALT_SIZE = 16
KDF_ITERATIONS = 600_000


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a passphrase using PBKDF2.

    Args:
        passphrase: The encryption passphrase.
        salt: 16-byte random salt.

    Returns:
        32-byte AES key.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        KDF_ITERATIONS,
        dklen=KEY_SIZE,
    )


class MemoryVault:
    """Encrypt and decrypt memory files at rest.

    Uses AES-256-GCM for authenticated encryption. Each encrypt()
    call generates a fresh random nonce and salt, so the same
    plaintext never produces the same ciphertext.

    File format:
        SKMV1 || salt(16) || nonce(12) || ciphertext || tag(16)

    Args:
        passphrase: Secret used to derive the encryption key.
            Can be the agent's CapAuth passphrase or a dedicated vault key.
    """

    def __init__(self, passphrase: str) -> None:
        self._passphrase = passphrase

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data with AES-256-GCM.

        Args:
            plaintext: Raw bytes to encrypt (typically memory JSON).

        Returns:
            Encrypted bytes with header, salt, nonce, ciphertext, and tag.

        Raises:
            ImportError: If cryptography package is not installed.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(SALT_SIZE)
        nonce = os.urandom(NONCE_SIZE)
        key = _derive_key(self._passphrase, salt)

        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, VAULT_HEADER)

        return VAULT_HEADER + salt + nonce + ciphertext

    def decrypt(self, encrypted: bytes) -> bytes:
        """Decrypt data encrypted with encrypt().

        Args:
            encrypted: Bytes from encrypt() — header + salt + nonce + ciphertext + tag.

        Returns:
            Decrypted plaintext bytes.

        Raises:
            ValueError: If the header doesn't match or decryption fails.
            ImportError: If cryptography package is not installed.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        header_len = len(VAULT_HEADER)
        if encrypted[:header_len] != VAULT_HEADER:
            raise ValueError("Not an SKMemory vault file (bad header)")

        offset = header_len
        salt = encrypted[offset : offset + SALT_SIZE]
        offset += SALT_SIZE
        nonce = encrypted[offset : offset + NONCE_SIZE]
        offset += NONCE_SIZE
        ciphertext = encrypted[offset:]

        key = _derive_key(self._passphrase, salt)
        aesgcm = AESGCM(key)

        return aesgcm.decrypt(nonce, ciphertext, VAULT_HEADER)

    def encrypt_file(self, path: Path) -> Path:
        """Encrypt a file in-place, adding .vault extension.

        Args:
            path: Path to the plaintext file.

        Returns:
            Path to the encrypted file.
        """
        plaintext = path.read_bytes()
        encrypted = self.encrypt(plaintext)
        vault_path = path.with_suffix(path.suffix + ".vault")
        vault_path.write_bytes(encrypted)
        path.unlink()
        return vault_path

    def decrypt_file(self, path: Path) -> Path:
        """Decrypt a .vault file, restoring the original.

        Args:
            path: Path to the encrypted .vault file.

        Returns:
            Path to the decrypted file.
        """
        encrypted = path.read_bytes()
        plaintext = self.decrypt(encrypted)
        original_path = Path(str(path).replace(".vault", ""))
        original_path.write_bytes(plaintext)
        path.unlink()
        return original_path

    def is_encrypted(self, path: Path) -> bool:
        """Check if a file is vault-encrypted.

        Args:
            path: Path to check.

        Returns:
            True if the file has a valid vault header.
        """
        try:
            data = path.read_bytes()
            return data[:len(VAULT_HEADER)] == VAULT_HEADER
        except OSError:
            return False


def encrypt_memory_store(
    memory_dir: Path,
    passphrase: str,
) -> int:
    """Encrypt all memory JSON files in a directory tree.

    Args:
        memory_dir: Root memory directory (e.g., ~/.skcapstone/memories/).
        passphrase: Encryption passphrase.

    Returns:
        Number of files encrypted.
    """
    vault = MemoryVault(passphrase)
    count = 0

    for json_file in memory_dir.rglob("*.json"):
        if json_file.suffix == ".vault":
            continue
        try:
            vault.encrypt_file(json_file)
            count += 1
        except Exception as exc:
            logger.warning("Failed to encrypt %s: %s", json_file, exc)

    return count


def decrypt_memory_store(
    memory_dir: Path,
    passphrase: str,
) -> int:
    """Decrypt all vault files in a directory tree.

    Args:
        memory_dir: Root memory directory.
        passphrase: Decryption passphrase.

    Returns:
        Number of files decrypted.
    """
    vault = MemoryVault(passphrase)
    count = 0

    for vault_file in memory_dir.rglob("*.vault"):
        try:
            vault.decrypt_file(vault_file)
            count += 1
        except Exception as exc:
            logger.warning("Failed to decrypt %s: %s", vault_file, exc)

    return count
