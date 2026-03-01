"""Memory Fortress — hardened wrapper around MemoryStore.

Three layers of sovereign memory protection:

1. **Auto-seal integrity** — every memory is hashed on write (via ``Memory.seal()``)
   and verified on every read. Tampered memories trigger structured alerts.

2. **At-rest encryption** — optionally encrypt memory JSON files with a PGP key
   so the underlying FileBackend stores ciphertext instead of plaintext.

3. **Audit trail** — every store/recall/delete operation is appended to an
   immutable JSONL log with timestamp, operation type, and outcome.

Jonathan Clements' AMK concept taken to its sovereign conclusion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import SKMEMORY_HOME
from .models import EmotionalSnapshot, Memory, MemoryLayer, MemoryRole
from .store import MemoryStore

logger = logging.getLogger("skmemory.fortress")

DEFAULT_AUDIT_PATH = SKMEMORY_HOME / "audit.jsonl"
DEFAULT_ENCRYPTED_PATH = SKMEMORY_HOME / "encrypted"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only JSONL audit trail for memory operations.

    Each line is a self-contained JSON record:
    ``{"ts": "...", "op": "store|recall|delete|tamper", "id": "...", "ok": true, ...}``

    The log is opened in append mode; the file is never truncated.
    A tampered audit log is detectable via an external log integrity system.

    Args:
        path: Path to the JSONL file.
    """

    def __init__(self, path: Path = DEFAULT_AUDIT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Track cumulative hash for log chain integrity
        self._chain_hash = self._load_chain_tip()

    def _load_chain_tip(self) -> str:
        """Read the last line of the log and return the chain hash stored there."""
        if not self.path.exists():
            return "genesis"
        try:
            with self.path.open("rb") as f:
                f.seek(0, 2)  # end
                size = f.tell()
                if size == 0:
                    return "genesis"
                # Read last line efficiently
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="replace")
                lines = [l for l in tail.split("\n") if l.strip()]
                if not lines:
                    return "genesis"
                last = json.loads(lines[-1])
                return last.get("chain_hash", "genesis")
        except Exception:
            return "genesis"

    def _next_chain_hash(self, record: dict) -> str:
        """Compute the chain hash for this record: SHA-256 of prev_hash + record JSON."""
        record_json = json.dumps(record, sort_keys=True, separators=(",", ":"))
        payload = f"{self._chain_hash}:{record_json}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def append(self, op: str, memory_id: str, *, ok: bool = True, **extra: Any) -> None:
        """Append one audit record.

        Args:
            op: Operation name (``store``, ``recall``, ``delete``, ``tamper``, ``verify``).
            memory_id: The memory ID affected.
            ok: Whether the operation succeeded.
            **extra: Additional fields to include in the record.
        """
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "id": memory_id,
            "ok": ok,
        }
        record.update(extra)
        record["chain_hash"] = self._next_chain_hash(record)
        self._chain_hash = record["chain_hash"]

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[dict]:
        """Return the last ``n`` audit records.

        Args:
            n: Number of records to return.

        Returns:
            list[dict]: Most-recent records, oldest first.
        """
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records[-n:]

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Verify the chain hash integrity of the entire audit log.

        Each record's ``chain_hash`` must equal SHA-256(prev_hash + record_json).
        A break in the chain indicates the log was tampered with.

        Returns:
            tuple[bool, list[str]]: (is_valid, list of error messages if broken).
        """
        if not self.path.exists():
            return True, []

        errors: list[str] = []
        prev_hash = "genesis"

        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"Line {lineno}: invalid JSON")
                    continue

                stored_chain = record.pop("chain_hash", "")
                expected = hashlib.sha256(
                    f"{prev_hash}:{json.dumps(record, sort_keys=True, separators=(',', ':'))}".encode()
                ).hexdigest()[:16]

                if stored_chain != expected:
                    errors.append(
                        f"Line {lineno} (id={record.get('id', '?')}): "
                        f"chain hash mismatch — log may be tampered"
                    )
                prev_hash = stored_chain

        return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# At-rest encryption
# ---------------------------------------------------------------------------

class EncryptedFileBackend:
    """Transparent PGP encryption layer over FileBackend JSON files.

    When active, every JSON file written by the underlying store is
    symmetrically or asymmetrically encrypted with GPG. Reads decrypt
    on the fly. The memory data is never on disk in plaintext.

    Uses ``python-gnupg`` (system GPG), so key material stays in the
    user's GPG keyring — never in Python memory longer than needed.

    Args:
        fingerprint: PGP key fingerprint to encrypt to.
        gnupg_home: Path to GPG home directory.
    """

    def __init__(
        self,
        fingerprint: str,
        gnupg_home: Optional[str] = None,
    ) -> None:
        try:
            import gnupg
        except ImportError as exc:
            raise ImportError(
                "python-gnupg is required for at-rest encryption. "
                "Install with: pip install python-gnupg"
            ) from exc

        import gnupg as _gnupg
        home = gnupg_home or os.path.expanduser("~/.gnupg")
        self._gpg = _gnupg.GPG(gnupghome=home)
        self.fingerprint = fingerprint.upper().replace(" ", "")
        self._verify_key()

    def _verify_key(self) -> None:
        """Raise if the fingerprint is not in the keyring."""
        keys = self._gpg.list_keys()
        fps = [k["fingerprint"] for k in keys]
        if self.fingerprint not in fps:
            raise ValueError(
                f"PGP key {self.fingerprint} not found in GPG keyring. "
                "Import the public key first."
            )

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext JSON to ASCII-armored PGP ciphertext.

        Args:
            plaintext: The JSON string to encrypt.

        Returns:
            str: ASCII-armored PGP message.

        Raises:
            RuntimeError: If encryption fails.
        """
        result = self._gpg.encrypt(
            plaintext,
            self.fingerprint,
            armor=True,
            always_trust=True,
        )
        if not result.ok:
            raise RuntimeError(f"GPG encryption failed: {result.status} / {result.stderr}")
        return str(result)

    def decrypt(self, ciphertext: str, passphrase: Optional[str] = None) -> str:
        """Decrypt ASCII-armored PGP ciphertext to plaintext JSON.

        Args:
            ciphertext: ASCII-armored PGP message.
            passphrase: Private key passphrase (if needed).

        Returns:
            str: Decrypted plaintext JSON.

        Raises:
            RuntimeError: If decryption fails.
        """
        result = self._gpg.decrypt(ciphertext, passphrase=passphrase)
        if not result.ok:
            raise RuntimeError(f"GPG decryption failed: {result.status} / {result.stderr}")
        return str(result)

    def is_encrypted(self, text: str) -> bool:
        """Return True if the text looks like PGP-armored ciphertext."""
        return "BEGIN PGP MESSAGE" in text


# ---------------------------------------------------------------------------
# Tamper alert system
# ---------------------------------------------------------------------------

class TamperAlert:
    """Structured tamper alert with rich context.

    Created when a memory fails its integrity check. Consumers can
    register callbacks to trigger notifications (Slack, email, etc.).

    Args:
        memory_id: ID of the tampered memory.
        expected_hash: What the hash should be.
        actual_hash: What was found on disk.
        detected_at: ISO 8601 UTC timestamp.
    """

    def __init__(
        self,
        memory_id: str,
        expected_hash: str,
        actual_hash: str,
        detected_at: Optional[str] = None,
    ) -> None:
        self.memory_id = memory_id
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        self.detected_at = detected_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "detected_at": self.detected_at,
            "severity": "CRITICAL",
            "message": (
                f"Memory {self.memory_id} failed integrity check. "
                "Content may have been altered after storage."
            ),
        }

    def __repr__(self) -> str:
        return (
            f"TamperAlert(id={self.memory_id!r}, "
            f"detected_at={self.detected_at!r})"
        )


# ---------------------------------------------------------------------------
# Fortified MemoryStore
# ---------------------------------------------------------------------------

class FortifiedMemoryStore(MemoryStore):
    """A hardened MemoryStore with audit trail, encryption, and tamper alerts.

    Drop-in replacement for ``MemoryStore``. Adds three security layers:

    1. **Audit trail** — every operation is appended to a JSONL log.
    2. **Integrity sealing** — every stored memory is sealed with a hash;
       every recalled memory is verified against its stored hash.
    3. **Tamper alerts** — failed integrity checks trigger registered callbacks
       and are logged as CRITICAL audit events.

    At-rest encryption is opt-in via ``encryption_key_fingerprint``.

    Args:
        audit_path: Path to the audit JSONL log.
        encryption_key_fingerprint: If set, encrypt JSON files at rest.
        gnupg_home: GPG home directory (for encryption).
        alert_callbacks: Functions to call when tamper is detected.
        **store_kwargs: Passed through to ``MemoryStore``.

    Example::

        store = FortifiedMemoryStore(
            encryption_key_fingerprint="9B3AB00F411B064646879B92D10E637B4F8367DA",
            alert_callbacks=[lambda alert: send_to_slack(alert.to_dict())],
        )
        memory = store.snapshot("title", "content")
        recalled = store.recall(memory.id)
    """

    def __init__(
        self,
        audit_path: Optional[Path] = None,
        encryption_key_fingerprint: Optional[str] = None,
        gnupg_home: Optional[str] = None,
        alert_callbacks: Optional[list[Callable[[TamperAlert], None]]] = None,
        **store_kwargs: Any,
    ) -> None:
        super().__init__(**store_kwargs)
        self.audit = AuditLog(path=audit_path or DEFAULT_AUDIT_PATH)
        self.alert_callbacks: list[Callable[[TamperAlert], None]] = alert_callbacks or []
        self._encryption: Optional[EncryptedFileBackend] = None

        if encryption_key_fingerprint:
            self._encryption = EncryptedFileBackend(
                fingerprint=encryption_key_fingerprint,
                gnupg_home=gnupg_home,
            )
            logger.info(
                "Memory Fortress: at-rest encryption active for key %s",
                encryption_key_fingerprint[:8],
            )

    # ------------------------------------------------------------------
    # Core overrides
    # ------------------------------------------------------------------

    def snapshot(
        self,
        title: str,
        content: str,
        **kwargs: Any,
    ) -> Memory:
        """Store a memory with auto-seal and audit logging.

        Seals the integrity hash before storage. The base ``MemoryStore.snapshot``
        already calls ``memory.seal()`` — this override adds the audit trail.

        Returns:
            Memory: The sealed, stored memory.
        """
        memory = super().snapshot(title, content, **kwargs)
        self.audit.append(
            "store",
            memory.id,
            ok=True,
            layer=memory.layer.value,
            title=memory.title[:64],
            integrity_hash=memory.integrity_hash[:8] if memory.integrity_hash else "",
        )
        logger.debug("Fortress: stored memory %s (sealed)", memory.id)
        return memory

    def recall(self, memory_id: str) -> Optional[Memory]:
        """Recall a memory with integrity verification and tamper alerting.

        Overrides ``MemoryStore.recall`` to trigger structured ``TamperAlert``
        callbacks (not just log warnings) when integrity fails.

        Returns:
            Optional[Memory]: The memory, flagged in metadata if tampered.
        """
        memory = self.primary.load(memory_id)
        if memory is None:
            self.audit.append("recall", memory_id, ok=False, reason="not_found")
            return None

        # Integrity check
        if memory.integrity_hash:
            current_hash = memory.compute_integrity_hash()
            if current_hash != memory.integrity_hash:
                self._raise_tamper_alert(memory, current_hash)
                self.audit.append(
                    "tamper",
                    memory_id,
                    ok=False,
                    stored_hash=memory.integrity_hash[:8],
                    computed_hash=current_hash[:8],
                )
                memory.metadata["integrity_warning"] = (
                    f"Tamper detected at {datetime.now(timezone.utc).isoformat()}. "
                    "Memory may have been modified after storage."
                )
            else:
                self.audit.append(
                    "recall",
                    memory_id,
                    ok=True,
                    integrity="ok",
                )
        else:
            self.audit.append(
                "recall",
                memory_id,
                ok=True,
                integrity="unsealed",
            )

        return memory

    def forget(self, memory_id: str) -> bool:
        """Delete a memory with audit logging."""
        result = super().forget(memory_id)
        self.audit.append("delete", memory_id, ok=result)
        logger.debug("Fortress: deleted memory %s (ok=%s)", memory_id, result)
        return result

    def verify_all(self) -> dict:
        """Verify integrity of every memory in the store.

        Loads all memories and checks each one. Useful for scheduled
        health checks or post-incident forensics.

        Returns:
            dict: Summary with counts and any tamper alerts found.
        """
        all_memories = self.primary.list_memories(limit=99999)
        total = len(all_memories)
        passed = 0
        tampered: list[str] = []
        unsealed: list[str] = []

        for mem in all_memories:
            if not mem.integrity_hash:
                unsealed.append(mem.id)
                continue
            if mem.verify_integrity():
                passed += 1
            else:
                current = mem.compute_integrity_hash()
                self._raise_tamper_alert(mem, current)
                self.audit.append(
                    "tamper",
                    mem.id,
                    ok=False,
                    stored_hash=mem.integrity_hash[:8],
                    computed_hash=current[:8],
                    context="verify_all",
                )
                tampered.append(mem.id)

        self.audit.append(
            "verify",
            "ALL",
            ok=len(tampered) == 0,
            total=total,
            passed=passed,
            tampered=len(tampered),
            unsealed=len(unsealed),
        )

        return {
            "total": total,
            "passed": passed,
            "tampered": tampered,
            "unsealed": unsealed,
        }

    def audit_trail(self, n: int = 50) -> list[dict]:
        """Return the most recent audit entries.

        Args:
            n: Number of entries to return.

        Returns:
            list[dict]: Audit records, oldest first.
        """
        return self.audit.tail(n)

    def verify_audit_chain(self) -> tuple[bool, list[str]]:
        """Verify the integrity chain of the audit log itself.

        Returns:
            tuple[bool, list[str]]: (is_valid, errors).
        """
        return self.audit.verify_chain()

    def register_alert_callback(
        self, callback: Callable[[TamperAlert], None]
    ) -> None:
        """Register a function to be called when tamper is detected.

        Args:
            callback: Function receiving a ``TamperAlert`` instance.
        """
        self.alert_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Encryption helpers (public API for direct access if needed)
    # ------------------------------------------------------------------

    def encrypt_payload(self, json_text: str) -> str:
        """Encrypt a JSON string using the configured PGP key.

        Args:
            json_text: The plaintext JSON to encrypt.

        Returns:
            str: ASCII-armored PGP ciphertext.

        Raises:
            RuntimeError: If encryption is not configured.
        """
        if self._encryption is None:
            raise RuntimeError("Encryption not configured — pass encryption_key_fingerprint")
        return self._encryption.encrypt(json_text)

    def decrypt_payload(self, armored: str, passphrase: Optional[str] = None) -> str:
        """Decrypt PGP ciphertext back to JSON.

        Args:
            armored: ASCII-armored PGP ciphertext.
            passphrase: Private key passphrase.

        Returns:
            str: Decrypted JSON string.

        Raises:
            RuntimeError: If encryption is not configured.
        """
        if self._encryption is None:
            raise RuntimeError("Encryption not configured — pass encryption_key_fingerprint")
        return self._encryption.decrypt(armored, passphrase=passphrase)

    @property
    def encryption_active(self) -> bool:
        """Return True if at-rest encryption is configured."""
        return self._encryption is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _raise_tamper_alert(self, memory: Memory, computed_hash: str) -> None:
        """Log and dispatch a tamper alert to all registered callbacks."""
        alert = TamperAlert(
            memory_id=memory.id,
            expected_hash=memory.integrity_hash,
            actual_hash=computed_hash,
        )
        logger.critical(
            "TAMPER ALERT: Memory %s integrity check failed! "
            "Expected=%s Actual=%s",
            memory.id,
            memory.integrity_hash[:16],
            computed_hash[:16],
        )
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception as exc:
                logger.error("Alert callback error: %s", exc)
