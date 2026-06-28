"""
Tests for the additive at-rest sealing seam (skmemory.sealing).

Covers:
- ClassicalSealer reproduces FileBackend's on-disk bytes BIT-FOR-BIT.
- content_checksum is stable and matches a manual SHA-256 over those bytes.
- get_sealer defaults to classical and falls back to classical when the
  sk_pgp backend is requested but unavailable (no key / package missing).
- SkPgpSealer sign/verify roundtrip when sk_pgp is present (skipped otherwise),
  including tamper rejection and the honest tri-state verdict.

None of this touches the live save/load path: the seam is unwired.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.sealing import (
    SCHEME_CLASSICAL,
    SCHEME_SKPGP_MLDSA87_ED448,
    ClassicalSealer,
    SkPgpSealer,
    at_rest_bytes,
    content_checksum,
    get_sealer,
    seal_status,
)

_HAS_SK_PGP = importlib.util.find_spec("sk_pgp") is not None


def _make_memory() -> Memory:
    return Memory(
        title="Sealing test",
        content="A polaroid worth sealing.",
        layer=MemoryLayer.SHORT,
        tags=["seal", "pqc"],
    )


# --------------------------------------------------------------------------- #
# Classical: bit-for-bit fidelity with the live FileBackend
# --------------------------------------------------------------------------- #


def test_classical_at_rest_bytes_match_filebackend_bitforbit(tmp_path):
    """ClassicalSealer.at_rest_bytes == the exact bytes FileBackend writes."""
    mem = _make_memory()
    backend = FileBackend(base_path=str(tmp_path))
    backend.save(mem)

    on_disk = (tmp_path / mem.layer.value / f"{mem.id}.json").read_bytes()
    sealed = ClassicalSealer().at_rest_bytes(mem)

    assert sealed == on_disk  # byte-for-byte, no divergence


def test_at_rest_bytes_match_manual_serialisation():
    """The helper mirrors json.dumps(model_dump(), indent=2, default=str)."""
    mem = _make_memory()
    expected = json.dumps(mem.model_dump(), indent=2, default=str).encode("utf-8")
    assert at_rest_bytes(mem) == expected


def test_at_rest_bytes_roundtrips_back_to_memory(tmp_path):
    """Bytes the sealer produces still load back into an identical Memory."""
    mem = _make_memory()
    raw = ClassicalSealer().at_rest_bytes(mem)
    reloaded = Memory(**json.loads(raw.decode("utf-8")))
    assert reloaded.id == mem.id
    assert reloaded.content == mem.content


def test_content_checksum_is_sha256_over_at_rest_bytes():
    mem = _make_memory()
    expected = "sha256:" + hashlib.sha256(at_rest_bytes(mem)).hexdigest()
    assert content_checksum(mem) == expected


def test_classical_verify_tristate_and_tamper():
    sealer = ClassicalSealer()
    mem = _make_memory()
    cs = sealer.checksum(mem)

    good = sealer.verify(mem, None, expected_checksum=cs)
    assert good.checksum_ok is True
    assert good.signature_ok is None  # honest: no cryptographic signature today
    assert good.ok is True

    # Tamper: change content -> checksum no longer matches the stored one.
    mem.content = "altered after sealing"
    bad = sealer.verify(mem, None, expected_checksum=cs)
    assert bad.checksum_ok is False
    assert bad.ok is False


def test_classical_sign_returns_none():
    """ClassicalSealer is honest: no signature, only a hash."""
    assert ClassicalSealer().sign(_make_memory()) is None


# --------------------------------------------------------------------------- #
# Resolver: default + honest fallback
# --------------------------------------------------------------------------- #


def test_get_sealer_defaults_to_classical(monkeypatch):
    for var in (
        "SKMEMORY_SEAL_BACKEND",
        "SKMEMORY_SEAL_SCHEME",
        "SKMEMORY_SEAL_KEY",
        "SKMEMORY_SEAL_CERT",
        "SKMEMORY_SEAL_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    sealer = get_sealer()
    assert sealer.scheme == SCHEME_CLASSICAL
    assert isinstance(sealer, ClassicalSealer)


def test_get_sealer_falls_back_when_sk_pgp_requested_without_key(monkeypatch):
    """Requesting sk_pgp with no key must NOT break -- fall back to classical."""
    monkeypatch.setenv("SKMEMORY_SEAL_BACKEND", "sk_pgp")
    monkeypatch.delenv("SKMEMORY_SEAL_KEY", raising=False)
    sealer = get_sealer()
    assert sealer.scheme == SCHEME_CLASSICAL


def test_get_sealer_falls_back_when_sk_pgp_missing(monkeypatch):
    """If sk_pgp can't import, even a configured key falls back to classical."""
    import skmemory.sealing as sealing

    monkeypatch.setattr(sealing, "_sk_pgp", lambda: None)
    sealer = sealing.get_sealer({"backend": "sk_pgp", "key": "/nonexistent.key"})
    assert sealer.scheme == SCHEME_CLASSICAL


def test_seal_status_is_honest_and_side_effect_free():
    status = seal_status()
    assert status["active_scheme"] == SCHEME_CLASSICAL
    assert status["classical_available"] is True
    assert "quantum-proof" not in status["note"].lower().replace("not quantum-proof", "")
    assert "NOT quantum-proof" in status["note"]


def test_skpgp_sealer_unsupported_scheme_rejected():
    with pytest.raises(ValueError):
        SkPgpSealer(scheme="sk_pgp:rsa4k")


def test_skpgp_sealer_inert_without_key():
    """Constructed but keyless -> not available -> verify is honest about it."""
    sealer = SkPgpSealer(scheme=SCHEME_SKPGP_MLDSA87_ED448)
    assert sealer.available() is False
    # at-rest bytes still identical to classical even on the PQC sealer
    mem = _make_memory()
    assert sealer.at_rest_bytes(mem) == ClassicalSealer().at_rest_bytes(mem)


# --------------------------------------------------------------------------- #
# sk_pgp: real composite sign/verify roundtrip (gated on package availability)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAS_SK_PGP, reason="sk_pgp not installed (optional extra)")
def test_skpgp_sign_verify_roundtrip(tmp_path):
    import sk_pgp  # type: ignore

    key = sk_pgp.Key.generate("Seal Test <seal@skworld.io>", "mldsa87-ed448", password=None)
    key_path = tmp_path / "seal.key"
    key_path.write_text(key.to_armor())

    sealer = SkPgpSealer(
        scheme=SCHEME_SKPGP_MLDSA87_ED448,
        secret_key_path=str(key_path),
    )
    assert sealer.available() is True

    mem = _make_memory()
    cs = sealer.checksum(mem)
    sig = sealer.sign(mem)
    assert sig is not None

    verdict = sealer.verify(mem, sig, expected_checksum=cs)
    assert verdict.checksum_ok is True
    assert verdict.signature_ok is True  # both composite legs verified
    assert verdict.is_post_quantum is True
    assert verdict.ok is True

    # Tamper the content -> composite signature must fail.
    mem.content = "tampered payload"
    tampered = sealer.verify(mem, sig, expected_checksum=cs)
    assert tampered.signature_ok is False
    assert tampered.ok is False


@pytest.mark.skipif(not _HAS_SK_PGP, reason="sk_pgp not installed (optional extra)")
def test_get_sealer_selects_skpgp_when_ready(tmp_path, monkeypatch):
    import sk_pgp  # type: ignore

    key = sk_pgp.Key.generate("Seal Test <seal@skworld.io>", "mldsa87-ed448", password=None)
    key_path = tmp_path / "seal.key"
    key_path.write_text(key.to_armor())

    monkeypatch.setenv("SKMEMORY_SEAL_BACKEND", "sk_pgp")
    monkeypatch.setenv("SKMEMORY_SEAL_KEY", str(key_path))
    sealer = get_sealer()
    assert sealer.scheme == SCHEME_SKPGP_MLDSA87_ED448
