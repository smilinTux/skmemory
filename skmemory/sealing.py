"""
skmemory.sealing -- pluggable at-rest *sealing* seam for flat-file memories.

This module is an **additive scaffold**, mirroring cloud9's Stage-1 sealing
pattern (``cloud9/sealing.py``). It does NOT change how memories are written or
read today: :class:`skmemory.backends.file_backend.FileBackend` keeps writing
exactly ``json.dumps(memory.model_dump(), indent=2, default=str)`` and reading
it straight back. **Nothing here is wired into the live save/load path.**

What it adds is a **seam**: one :class:`Sealer` interface plus a config-driven
resolver (:func:`get_sealer`) so the move from today's classical at-rest
behaviour to a real post-quantum *detached signature* (via ``sk_pgp``'s
composite **ML-DSA-87 + Ed448**) becomes a configuration change later, not a
rewrite of the backend.

Backends
--------
- ``classical`` (default, always available) -- reproduces the current at-rest
  behaviour **bit-for-bit**: the on-disk byte string is exactly what
  ``FileBackend.save`` writes, and the integrity check is a SHA-256 over those
  bytes. **Honest note:** today's tamper-evidence is *only* a hash. A SHA-256
  proves the bytes weren't altered by an accident or a third party who can't
  also recompute the hash; it proves **nothing** about authorship and is not a
  signature. ``Memory.integrity_hash`` is likewise a plain SHA-256, not a
  cryptographic signature.
- ``sk_pgp`` (gated, opt-in) -- produces a genuine OpenPGP **detached**
  signature over the exact at-rest bytes using the composite ML-DSA-87 + Ed448
  suite (FIPS 204 ML-DSA + RFC 8032 Ed448). The signature lives in a
  **sidecar** (e.g. ``<id>.json.sig``) so the memory JSON file -- and its
  byte-for-byte compatibility with every existing reader -- is never perturbed.
  This backend is inert unless ``sk_pgp`` is importable **and** a signing key is
  configured **and** it is explicitly selected via :func:`get_sealer`;
  otherwise the resolver falls back to ``classical``.

Honest-claims discipline (see docs/PQC_SEALING.md)
--------------------------------------------------
ML-DSA / ML-KEM are **post-quantum / quantum-resistant**, NOT "quantum-proof"
or "quantum-safe." The composite is a **hybrid** (lattice ML-DSA AND classical
Ed448): it verifies **iff both legs verify**, so "hybrid" means *either leg
still standing keeps the classical security assumption intact* -- it is not a
magic shield. All PQC assurance ultimately rests on sequoia-openpgp + liboqs
via ``sk_pgp``; skmemory adds no cryptography of its own.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# At-rest serialisation -- identical to FileBackend so bytes match bit-for-bit
# --------------------------------------------------------------------------- #

CHECKSUM_PREFIX = "sha256:"

# Sealing schemes a sealer may advertise.
SCHEME_CLASSICAL = "classical"                            # sha256 over at-rest bytes
SCHEME_SKPGP_MLDSA87_ED448 = "sk_pgp:mldsa87-ed448"       # composite L5 detached sig
SCHEME_SKPGP_MLDSA65_ED25519 = "sk_pgp:mldsa65-ed25519"   # composite L3 detached sig


def at_rest_bytes(memory_like: Any) -> bytes:
    """Return the exact byte string a memory occupies *at rest*.

    This MUST stay identical to ``FileBackend.save``, which writes::

        json.dumps(memory.model_dump(), indent=2, default=str).encode("utf-8")

    Accepts a ``Memory`` (anything with ``model_dump``), a plain ``dict``, or
    raw ``bytes``/``str`` (already-serialised). Reproducing those bytes exactly
    is what lets the ``classical`` sealer be bit-for-bit compatible and what the
    ``sk_pgp`` sealer signs over.
    """
    if isinstance(memory_like, (bytes, bytearray)):
        return bytes(memory_like)
    if isinstance(memory_like, str):
        return memory_like.encode("utf-8")
    if hasattr(memory_like, "model_dump"):
        data = memory_like.model_dump()
    elif isinstance(memory_like, dict):
        data = memory_like
    else:  # pragma: no cover - defensive
        raise TypeError(f"cannot serialise {type(memory_like)!r}")
    return json.dumps(data, indent=2, default=str).encode("utf-8")


def content_checksum(memory_like: Any) -> str:
    """The shared ``sha256:`` checksum over the at-rest bytes (all backends)."""
    return CHECKSUM_PREFIX + hashlib.sha256(at_rest_bytes(memory_like)).hexdigest()


# --------------------------------------------------------------------------- #
# Verdict + interface
# --------------------------------------------------------------------------- #


@dataclass
class SealVerdict:
    """Result of verifying a memory's at-rest seal.

    ``checksum_ok`` is the tamper-evidence that exists today. ``signature_ok``
    is tri-state: ``True`` (a real cryptographic signature verified), ``False``
    (a signature was present but failed), or ``None`` (no cryptographic
    signature to check -- the classical case). A classical/legacy memory
    therefore yields ``checksum_ok=True, signature_ok=None`` and is still
    treated as valid, never broken.
    """

    scheme: str
    checksum_ok: bool
    signature_ok: Optional[bool] = None
    fingerprint: Optional[str] = None
    is_post_quantum: bool = False
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # Honest gate: checksum must hold; a *present* signature must verify.
        return self.checksum_ok and self.signature_ok is not False


@runtime_checkable
class Sealer(Protocol):
    """Pluggable at-rest sealing backend. Implementations are drop-in swappable."""

    scheme: str

    def available(self) -> bool: ...

    def at_rest_bytes(self, memory_like: Any) -> bytes: ...

    def checksum(self, memory_like: Any) -> str: ...

    def sign(self, memory_like: Any) -> Optional[bytes]:
        """Return a detached signature (armored bytes), or ``None`` if the
        backend produces no cryptographic signature (the classical case)."""

    def verify(self, memory_like: Any, signature: Optional[bytes], *,
               expected_checksum: Optional[str] = None) -> SealVerdict: ...


# --------------------------------------------------------------------------- #
# Classical backend -- the working path today (default)
# --------------------------------------------------------------------------- #


class ClassicalSealer:
    """Reproduces today's at-rest behaviour. Always available, no new deps.

    ``at_rest_bytes`` is byte-for-byte what ``FileBackend.save`` writes, so
    swapping reads/writes through this sealer changes nothing on disk. ``sign``
    returns ``None`` because there is no cryptographic signature today -- only a
    SHA-256 checksum, which this sealer is honest about.
    """

    scheme = SCHEME_CLASSICAL

    def available(self) -> bool:
        return True

    def at_rest_bytes(self, memory_like: Any) -> bytes:
        return at_rest_bytes(memory_like)

    def checksum(self, memory_like: Any) -> str:
        return content_checksum(memory_like)

    def sign(self, memory_like: Any) -> Optional[bytes]:  # noqa: ARG002 - by design
        return None

    def verify(self, memory_like: Any, signature: Optional[bytes], *,
               expected_checksum: Optional[str] = None) -> SealVerdict:
        actual = content_checksum(memory_like)
        checksum_ok = (expected_checksum == actual) if expected_checksum else True
        notes = []
        if expected_checksum is None:
            notes.append("no expected checksum supplied; integrity unverified")
        if signature:
            notes.append(
                "classical sealer ignores signatures; SHA-256 is not a signature"
            )
        return SealVerdict(
            scheme=self.scheme,
            checksum_ok=checksum_ok,
            signature_ok=None,          # no cryptographic signature in this backend
            notes=notes,
        )


# --------------------------------------------------------------------------- #
# sk_pgp backend -- gated, opt-in PQC detached signatures (the future swap)
# --------------------------------------------------------------------------- #


def _sk_pgp():
    """Import sk_pgp lazily; return the module or ``None`` if unavailable."""
    try:
        import sk_pgp  # type: ignore
        return sk_pgp
    except Exception:  # pragma: no cover - environment dependent
        return None


# Map our scheme tag -> sk_pgp cipher-suite string.
_SKPGP_SUITES = {
    SCHEME_SKPGP_MLDSA87_ED448: "mldsa87-ed448",
    SCHEME_SKPGP_MLDSA65_ED25519: "mldsa65-ed25519",
}


class SkPgpSealer:
    """Composite ML-DSA + Ed448/Ed25519 **detached** signing over at-rest bytes.

    Scaffold status: the wiring is real (verified against ``sk_pgp`` 0.1.0:
    ``Key.from_file`` / ``Key.sign_detached`` / ``Cert.verify_detached``), but
    this backend is INERT unless (a) ``sk_pgp`` imports, (b) a signing key is
    configured, and (c) it is explicitly selected via :func:`get_sealer`.
    Signatures are produced over :func:`at_rest_bytes` and are intended to live
    in a **sidecar** (e.g. ``<id>.json.sig``) so the memory JSON itself -- and
    its existing checksum / reader compatibility -- is never perturbed.

    Honest: a composite signature verifies iff **both** legs (lattice ML-DSA per
    FIPS 204 AND classical Ed448 per RFC 8032) verify; sequoia enforces the AND.
    This is *post-quantum / quantum-resistant*, not "quantum-proof."
    """

    def __init__(
        self,
        scheme: str = SCHEME_SKPGP_MLDSA87_ED448,
        *,
        secret_key_path: Optional[str] = None,
        cert_path: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        if scheme not in _SKPGP_SUITES:
            raise ValueError(f"unsupported sk_pgp scheme: {scheme!r}")
        self.scheme = scheme
        self.suite = _SKPGP_SUITES[scheme]
        self.secret_key_path = secret_key_path
        self.cert_path = cert_path
        self._password = password

    def available(self) -> bool:
        return _sk_pgp() is not None and bool(self.secret_key_path)

    def at_rest_bytes(self, memory_like: Any) -> bytes:
        # On-disk bytes stay identical across backends -- never diverge.
        return at_rest_bytes(memory_like)

    def checksum(self, memory_like: Any) -> str:
        return content_checksum(memory_like)

    def _load_key(self):
        sk = _sk_pgp()
        if sk is None or not self.secret_key_path:
            raise RuntimeError(
                "sk_pgp backend not ready: package missing or no key configured"
            )
        return sk.Key.from_file(self.secret_key_path)  # type: ignore[attr-defined]

    def sign(self, memory_like: Any) -> Optional[bytes]:
        key = self._load_key()
        data = at_rest_bytes(memory_like)
        if self._password is not None:
            return key.sign_detached(data, password=self._password)
        return key.sign_detached(data)

    def verify(self, memory_like: Any, signature: Optional[bytes], *,
               expected_checksum: Optional[str] = None) -> SealVerdict:
        sk = _sk_pgp()
        actual = content_checksum(memory_like)
        checksum_ok = (expected_checksum == actual) if expected_checksum else True
        if sk is None:
            return SealVerdict(
                scheme=self.scheme, checksum_ok=checksum_ok, signature_ok=None,
                notes=["sk_pgp unavailable; cannot verify PQC signature"],
            )
        if not signature:
            return SealVerdict(
                scheme=self.scheme, checksum_ok=checksum_ok, signature_ok=None,
                notes=["no detached signature supplied"],
            )
        cert_src = self.cert_path or self.secret_key_path
        cert = sk.Cert.from_file(cert_src)  # type: ignore[attr-defined]
        sig_ok = bool(cert.verify_detached(signature, at_rest_bytes(memory_like)))
        fp = getattr(cert, "fingerprint", None)
        return SealVerdict(
            scheme=self.scheme,
            checksum_ok=checksum_ok,
            signature_ok=sig_ok,            # True iff BOTH composite legs verify
            fingerprint=fp,
            is_post_quantum=bool(getattr(cert, "is_post_quantum", True)),
        )


# --------------------------------------------------------------------------- #
# Resolver -- config is the only signal; default is always classical
# --------------------------------------------------------------------------- #

ENV_BACKEND = "SKMEMORY_SEAL_BACKEND"     # "classical" (default) | "sk_pgp"
ENV_SCHEME = "SKMEMORY_SEAL_SCHEME"       # e.g. "mldsa87-ed448" (sk_pgp only)
ENV_KEY = "SKMEMORY_SEAL_KEY"             # path to armored secret key
ENV_CERT = "SKMEMORY_SEAL_CERT"           # path to armored public cert (optional)
ENV_PASSWORD = "SKMEMORY_SEAL_PASSWORD"   # passphrase (prefer gpg-agent later)

_SCHEME_BY_SUITE = {
    "mldsa87-ed448": SCHEME_SKPGP_MLDSA87_ED448,
    "mldsa65-ed25519": SCHEME_SKPGP_MLDSA65_ED25519,
}


def get_sealer(config: Optional[Dict[str, Any]] = None) -> Sealer:
    """Resolve the active sealer from config/env. **Defaults to classical.**

    Resolution order: explicit ``config`` dict -> environment -> classical. If
    the sk_pgp backend is requested but not actually ready (package missing or
    no key), this *honestly* falls back to the classical sealer rather than
    failing -- so enabling PQC can never break memory read/write.
    """
    config = config or {}
    backend = (config.get("backend") or os.environ.get(ENV_BACKEND) or "classical").lower()

    if backend in ("sk_pgp", "skpgp", "pqc"):
        suite = (config.get("scheme") or os.environ.get(ENV_SCHEME) or "mldsa87-ed448").lower()
        scheme = _SCHEME_BY_SUITE.get(suite, SCHEME_SKPGP_MLDSA87_ED448)
        sealer = SkPgpSealer(
            scheme=scheme,
            secret_key_path=config.get("key") or os.environ.get(ENV_KEY),
            cert_path=config.get("cert") or os.environ.get(ENV_CERT),
            password=config.get("password") or os.environ.get(ENV_PASSWORD),
        )
        if sealer.available():
            return sealer
        # Honest gate: requested but not ready -> stay on the working path.
    return ClassicalSealer()


def seal_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Introspection helper for CLI/diagnostics. Side-effect free."""
    sealer = get_sealer(config)
    sk = _sk_pgp()
    return {
        "active_scheme": sealer.scheme,
        "active_is_post_quantum": sealer.scheme.startswith("sk_pgp:"),
        "classical_available": True,
        "sk_pgp_importable": sk is not None,
        "sk_pgp_version": getattr(sk, "__version__", None) if sk else None,
        "key_configured": bool(os.environ.get(ENV_KEY) or (config or {}).get("key")),
        "note": (
            "post-quantum / quantum-resistant via composite ML-DSA + EdDSA "
            "(FIPS 203/204) -- NOT quantum-proof; hybrid = valid iff both legs verify"
        ),
    }


__all__ = [
    "Sealer",
    "SealVerdict",
    "ClassicalSealer",
    "SkPgpSealer",
    "get_sealer",
    "seal_status",
    "content_checksum",
    "at_rest_bytes",
    "SCHEME_CLASSICAL",
    "SCHEME_SKPGP_MLDSA87_ED448",
    "SCHEME_SKPGP_MLDSA65_ED25519",
]
