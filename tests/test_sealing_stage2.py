"""Stage-2/3 tests: detached-signature sidecars wired into FileBackend.

Stage-1 (``test_sealing.py``) proved the *seam* in isolation. Stage-2 wires it
into the **actual** save/load path of :class:`skmemory.backends.file_backend.FileBackend`
and asserts the migration's core promises:

  - the **classical default** is byte-for-byte unchanged -- no sidecar, the
    memory JSON on disk is identical to today's ``FileBackend.save``, and
    ``load`` round-trips exactly as before;
  - when the gated ``sk_pgp`` backend is *explicitly* enabled AND a key is
    present, ``save`` additionally writes a ``<id>.json.sig`` composite
    (ML-DSA-87 + Ed448) detached signature, which ``load``'s verify-on-read
    confirms;
  - tampering with the memory file after signing yields ``signature_ok=False``
    (and, in strict mode, ``load`` rejects it by returning ``None``);
  - if ``sk_pgp`` is absent the resolver falls back to classical and nothing
    signs -- persistence never breaks.

``sk_pgp`` is an optional dependency; the cryptographic round-trip tests skip
cleanly when it isn't importable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory import sealing
from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer

sk_pgp = sealing._sk_pgp()
requires_skpgp = pytest.mark.skipif(sk_pgp is None, reason="sk_pgp not installed")


def _mem() -> Memory:
    return Memory(
        title="Stage-2 sealing",
        content="A polaroid worth signing.",
        layer=MemoryLayer.SHORT,
        tags=["seal", "pqc", "stage2"],
    )


def _sidecar(path: str) -> Path:
    return Path(str(path) + sealing.SIDECAR_SUFFIX)


# --------------------------------------------------------------------------- #
# Classical default -- bit-for-bit unchanged on the real save/load path
# --------------------------------------------------------------------------- #


def test_classical_save_writes_no_sidecar(tmp_path):
    be = FileBackend(base_path=str(tmp_path))
    mem = _mem()
    be.save(mem)
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    assert on_disk.exists()
    assert not _sidecar(on_disk).exists()


def test_classical_memory_json_byte_for_byte_unchanged(tmp_path):
    """The on-disk body must equal exactly today's FileBackend serialisation."""
    mem = _mem()
    be = FileBackend(base_path=str(tmp_path))
    be.save(mem)
    on_disk = (tmp_path / mem.layer.value / f"{mem.id}.json").read_bytes()
    expected = json.dumps(mem.model_dump(), indent=2, default=str).encode("utf-8")
    assert on_disk == expected


def test_classical_load_roundtrips_and_no_verdict(tmp_path):
    be = FileBackend(base_path=str(tmp_path))
    mem = _mem()
    be.save(mem)
    loaded = be.load(mem.id)
    assert loaded is not None
    assert loaded.id == mem.id
    assert loaded.content == mem.content
    # No sidecar => verify-on-read produces no verdict.
    assert be.last_verdict is None
    assert be.verify_at_rest(mem.id) is None


def test_write_seal_classical_is_noop(tmp_path):
    mem = _mem()
    p = tmp_path / "m.json"
    p.write_bytes(sealing.at_rest_bytes(mem))
    info = sealing.write_seal(mem, p)  # classical default
    assert info is None
    assert not _sidecar(p).exists()


def test_verify_seal_returns_none_without_sidecar(tmp_path):
    mem = _mem()
    p = tmp_path / "m.json"
    p.write_bytes(sealing.at_rest_bytes(mem))
    assert sealing.verify_seal(mem, p) is None


# --------------------------------------------------------------------------- #
# Honest fallback -- requested PQC but not ready never breaks persistence
# --------------------------------------------------------------------------- #


def test_fallback_to_classical_when_pqc_requested_but_no_key(tmp_path):
    be = FileBackend(base_path=str(tmp_path), seal_config={"backend": "sk_pgp"})
    mem = _mem()
    be.save(mem)
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    assert not _sidecar(on_disk).exists()
    # Body still byte-for-byte classical.
    expected = json.dumps(mem.model_dump(), indent=2, default=str).encode("utf-8")
    assert on_disk.read_bytes() == expected


def test_fallback_when_skpgp_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(sealing, "_sk_pgp", lambda: None)
    cfg = {"backend": "sk_pgp", "key": str(tmp_path / "nope.asc")}
    be = FileBackend(base_path=str(tmp_path), seal_config=cfg)
    mem = _mem()
    be.save(mem)
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    assert not _sidecar(on_disk).exists()


def test_sign_failure_never_breaks_save(tmp_path, monkeypatch):
    """If the sealer raises mid-sign, the memory still persists; no sidecar."""

    class _BoomSealer:
        scheme = sealing.SCHEME_SKPGP_MLDSA87_ED448

        def sign(self, _):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(sealing, "get_sealer", lambda cfg=None: _BoomSealer())
    be = FileBackend(base_path=str(tmp_path), seal_config={"backend": "sk_pgp"})
    mem = _mem()
    assert be.save(mem) == mem.id
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    assert on_disk.exists()
    assert not _sidecar(on_disk).exists()


# --------------------------------------------------------------------------- #
# Gated PQC round-trip -- sign on save, verify on read
# --------------------------------------------------------------------------- #


@pytest.fixture
def skpgp_key(tmp_path):
    """Generate a real composite ML-DSA-87 + Ed448 secret key on disk."""
    key = sk_pgp.Key.generate("Lumina <lumina@skworld.io>", "mldsa87-ed448", password="pw")
    key_path = tmp_path / "agent-key.asc"
    key_path.write_text(key.to_armor(), encoding="utf-8")
    return key_path


@requires_skpgp
def test_pqc_save_writes_sidecar(tmp_path, skpgp_key):
    cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    be = FileBackend(base_path=str(tmp_path), seal_config=cfg)
    mem = _mem()
    be.save(mem)
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    sig_path = _sidecar(on_disk)
    assert sig_path.exists()
    assert "BEGIN PGP" in sig_path.read_text(encoding="utf-8")
    # The memory JSON body is untouched (sidecar is a separate artifact).
    expected = json.dumps(mem.model_dump(), indent=2, default=str).encode("utf-8")
    assert on_disk.read_bytes() == expected


@requires_skpgp
def test_pqc_roundtrip_verifies_on_read(tmp_path, skpgp_key):
    cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    be = FileBackend(base_path=str(tmp_path), seal_config=cfg)
    mem = _mem()
    be.save(mem)
    loaded = be.load(mem.id)
    assert loaded is not None and loaded.id == mem.id
    v = be.last_verdict
    assert v is not None
    assert v.signature_ok is True          # both composite legs verified
    assert v.checksum_ok is True
    assert v.ok is True
    assert v.is_post_quantum is True
    assert v.fingerprint
    # Explicit verify-on-read API agrees.
    v2 = be.verify_at_rest(mem.id)
    assert v2 is not None and v2.signature_ok is True


@requires_skpgp
def test_pqc_tamper_fails_signature(tmp_path, skpgp_key):
    cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    be = FileBackend(base_path=str(tmp_path), seal_config=cfg)
    mem = _mem()
    be.save(mem)
    # Tamper the memory body on disk after signing.
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    data = json.loads(on_disk.read_text(encoding="utf-8"))
    data["content"] = "tampered after sealing"
    on_disk.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    # Non-strict: load still returns the memory, but the verdict is False.
    loaded = be.load(mem.id)
    assert loaded is not None
    assert be.last_verdict is not None
    assert be.last_verdict.signature_ok is False
    assert be.last_verdict.ok is False


@requires_skpgp
def test_pqc_strict_mode_rejects_tamper(tmp_path, skpgp_key):
    cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    be = FileBackend(base_path=str(tmp_path), seal_config=cfg, strict_verify=True)
    mem = _mem()
    be.save(mem)
    on_disk = tmp_path / mem.layer.value / f"{mem.id}.json"
    data = json.loads(on_disk.read_text(encoding="utf-8"))
    data["content"] = "tampered after sealing"
    on_disk.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    # Strict mode: a failed signature is a hard tamper event -> load returns None.
    assert be.load(mem.id) is None
    assert be.last_verdict is not None
    assert be.last_verdict.signature_ok is False


@requires_skpgp
def test_pqc_sidecar_present_but_unverifiable_is_honest(tmp_path, skpgp_key):
    """Signed, then read back with NO verify config: sidecar present but nothing
    to check against -> signature_ok=None (honest 'unverifiable'), never a
    rejection -- even in strict mode (only a *failed* signature is a tamper)."""
    sign_cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    signer = FileBackend(base_path=str(tmp_path), seal_config=sign_cfg)
    mem = _mem()
    signer.save(mem)
    # Fresh backend with no seal config and strict mode on.
    reader = FileBackend(base_path=str(tmp_path), strict_verify=True)
    loaded = reader.load(mem.id)
    assert loaded is not None                 # never rejected: unverifiable != failed
    v = reader.last_verdict
    assert v is not None
    assert v.signature_ok is None
    assert v.checksum_ok is True
    assert v.ok is True


@requires_skpgp
def test_pqc_verify_with_explicit_cert(tmp_path, skpgp_key):
    """Verification works from a public cert alone (no secret key needed)."""
    key = sk_pgp.Key.from_file(str(skpgp_key))
    cert_path = tmp_path / "agent-cert.asc"
    cert_path.write_text(key.cert.to_armor(), encoding="utf-8")

    sign_cfg = {"backend": "sk_pgp", "key": str(skpgp_key), "password": "pw"}
    signer = FileBackend(base_path=str(tmp_path), seal_config=sign_cfg)
    mem = _mem()
    signer.save(mem)

    verify_cfg = {"backend": "sk_pgp", "cert": str(cert_path)}
    reader = FileBackend(base_path=str(tmp_path), seal_config=verify_cfg)
    loaded = reader.load(mem.id)
    assert loaded is not None
    assert reader.last_verdict is not None
    assert reader.last_verdict.signature_ok is True
