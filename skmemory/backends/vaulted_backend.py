"""
VaultedSQLiteBackend — transparent AES-256-GCM at-rest encryption.

Every memory JSON file is stored as encrypted bytes on disk.
The SQLite index stores metadata in plaintext for fast queries;
only the full JSON files are encrypted.

The ``SKMV1`` vault header lets the backend auto-detect encrypted vs.
plain files, so you can safely migrate an existing unencrypted store
by calling ``seal_all()``.

File format on disk (per memory file):
    SKMV1 || salt(16) || nonce(12) || AES-GCM(json_bytes) || tag(16)

Usage:
    backend = VaultedSQLiteBackend(passphrase="sovereign-key")
    store = MemoryStore(primary=backend, use_sqlite=False)
    mem = store.snapshot("title", "content")
    recalled = store.recall(mem.id)  # transparent decrypt
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import Memory, MemoryLayer
from ..vault import VAULT_HEADER, MemoryVault
from .sqlite_backend import SQLiteBackend
from .sqlite_backend import DEFAULT_BASE_PATH


class VaultedSQLiteBackend(SQLiteBackend):
    """SQLiteBackend with transparent AES-256-GCM at-rest encryption.

    Subclasses :class:`SQLiteBackend` and overrides all file I/O to
    transparently encrypt on write and decrypt on read.  The SQLite
    index is unencrypted so queries remain fast.

    Args:
        passphrase: Secret used to derive the AES-256 key via PBKDF2-SHA256
            with 600 000 iterations. Use a strong, unique passphrase.
        base_path: Root directory for memory files and index.

    Raises:
        ImportError: If the ``cryptography`` package is not installed.

    Example::

        backend = VaultedSQLiteBackend(passphrase="my-secret")
        store = MemoryStore(primary=backend, use_sqlite=False)
        m = store.snapshot("Private thought", "End-to-end encrypted on disk")
        r = store.recall(m.id)  # decrypted on the fly
    """

    def __init__(self, passphrase: str, base_path: str = DEFAULT_BASE_PATH) -> None:
        self._vault = MemoryVault(passphrase)
        super().__init__(base_path=base_path)

    # ------------------------------------------------------------------
    # Core I/O overrides
    # ------------------------------------------------------------------

    def save(self, memory: Memory) -> str:
        """Encrypt JSON bytes before writing to disk.

        The SQLite index is updated with plaintext metadata so that
        queries still work without decryption.

        Args:
            memory: Memory to persist.

        Returns:
            str: The memory ID.
        """
        path = self._file_path(memory)
        path.parent.mkdir(parents=True, exist_ok=True)
        json_bytes = json.dumps(memory.model_dump(), indent=2, default=str).encode("utf-8")
        path.write_bytes(self._vault.encrypt(json_bytes))
        self._index_memory(memory, path)
        return memory.id

    def load(self, memory_id: str) -> Optional[Memory]:
        """Decrypt and parse a memory file.

        Handles both encrypted (``SKMV1`` header) and plaintext files
        so a partially-migrated store keeps working.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found, None otherwise.
        """
        path = self._find_file(memory_id)
        if path is None:
            return None
        return self._read_memory_file(path)

    def _row_to_memory(self, row: sqlite3.Row) -> Optional[Memory]:
        """Load a full Memory object, decrypting if needed.

        Args:
            row: SQLite row with ``file_path``.

        Returns:
            Optional[Memory]: Full memory or None if file missing / unreadable.
        """
        path = Path(row["file_path"])
        if not path.exists():
            return None
        return self._read_memory_file(path)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def reindex(self) -> int:
        """Rebuild the SQLite index by scanning and decrypting all JSON files.

        Returns:
            int: Number of memories re-indexed.
        """
        conn = self._get_conn()
        conn.execute("DELETE FROM memories")
        conn.commit()

        count = 0
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    memory = self._read_memory_file(json_file)
                    if memory is not None:
                        self._index_memory(memory, json_file)
                        count += 1
                except Exception:
                    continue
        return count

    def export_all(self, output_path: Optional[str] = None) -> str:
        """Export all memories (decrypted) to a JSON backup file.

        Args:
            output_path: Destination path. Defaults to
                ``~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json``.

        Returns:
            str: Path to the written backup file.
        """
        from .. import __version__

        if output_path is None:
            backup_dir = self.base_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(backup_dir / f"skmemory-backup-{date.today().isoformat()}.json")

        memories: list[dict] = []
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in sorted(layer_dir.glob("*.json")):
                try:
                    memory = self._read_memory_file(json_file)
                    if memory is not None:
                        memories.append(memory.model_dump())
                except Exception:
                    continue

        payload = {
            "skmemory_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "memory_count": len(memories),
            "base_path": str(self.base_path),
            "memories": memories,
        }
        Path(output_path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------
    # Vault management
    # ------------------------------------------------------------------

    def seal_all(self) -> int:
        """Encrypt all plaintext JSON files in the store.

        Safe to call multiple times — already-encrypted files are skipped.

        Returns:
            int: Number of files newly encrypted.
        """
        count = 0
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    raw = json_file.read_bytes()
                    if raw[: len(VAULT_HEADER)] == VAULT_HEADER:
                        continue  # already encrypted
                    json_file.write_bytes(self._vault.encrypt(raw))
                    count += 1
                except Exception:
                    continue
        return count

    def unseal_all(self) -> int:
        """Decrypt all vault-encrypted JSON files in the store.

        Returns:
            int: Number of files decrypted.
        """
        count = 0
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    raw = json_file.read_bytes()
                    if raw[: len(VAULT_HEADER)] != VAULT_HEADER:
                        continue  # not encrypted
                    json_file.write_bytes(self._vault.decrypt(raw))
                    count += 1
                except Exception:
                    continue
        return count

    def vault_status(self) -> dict:
        """Scan memory files and report encryption coverage.

        Returns:
            dict: ``{total, encrypted, plaintext, coverage_pct}``.
        """
        total = encrypted = 0
        header_len = len(VAULT_HEADER)
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                total += 1
                try:
                    with json_file.open("rb") as fh:
                        header = fh.read(header_len)
                    if header == VAULT_HEADER:
                        encrypted += 1
                except Exception:
                    pass
        plaintext = total - encrypted
        pct = (encrypted / total * 100) if total else 100.0
        return {
            "total": total,
            "encrypted": encrypted,
            "plaintext": plaintext,
            "coverage_pct": round(pct, 1),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_memory_file(self, path: Path) -> Optional[Memory]:
        """Read a file and parse to Memory, decrypting if needed.

        Args:
            path: Path to the JSON file (may be encrypted).

        Returns:
            Optional[Memory]: Parsed memory or None on failure.
        """
        try:
            raw = path.read_bytes()
            if raw[: len(VAULT_HEADER)] == VAULT_HEADER:
                raw = self._vault.decrypt(raw)
            data = json.loads(raw.decode("utf-8"))
            return Memory(**data)
        except Exception:
            return None
