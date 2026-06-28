# At-Rest Sealing Seam (PQC-ready)

`skmemory/sealing.py` is an **additive scaffold** that introduces a pluggable
*sealing* seam for the flat-file memory store, mirroring cloud9's Stage-1
sealing pattern (`cloud9/sealing.py`). It lets skmemory move from today's
classical at-rest behaviour to a real post-quantum **detached signature** as a
*configuration change*, not a rewrite.

It is wired into **nothing**. `FileBackend.save` / `load` are unchanged; the
live skchat / skcomms daemons are untouched. The seam exists so the cut-over,
when it happens, is a one-line backend swap behind `get_sealer()`.

## What "at rest" means here

Today a memory is persisted by `FileBackend.save` as exactly:

```python
json.dumps(memory.model_dump(), indent=2, default=str).encode("utf-8")
```

The seam's `at_rest_bytes(memory)` reproduces **those exact bytes**. A test
(`tests/test_sealing.py::test_classical_at_rest_bytes_match_filebackend_bitforbit`)
saves a real memory through `FileBackend`, reads the file back, and asserts the
sealer's bytes are byte-for-byte identical. The classical sealer therefore
changes nothing on disk.

## Backends

### `ClassicalSealer` (default, always available)

- `at_rest_bytes` — bit-for-bit identical to `FileBackend.save`.
- `checksum` — `sha256:<hex>` over those bytes.
- `sign` — returns `None`. **There is no cryptographic signature today.**
- `verify` — recomputes the checksum and compares; `signature_ok` is `None`.

**Honest note.** Today's at-rest tamper-evidence is *only* a SHA-256. A hash
detects accidental or third-party byte changes by someone who can't also
recompute and replace the hash. It proves **nothing about authorship** and is
**not a signature**. `Memory.integrity_hash` (in `models.py`) is likewise a
plain SHA-256, not a signature. The classical sealer is deliberately honest
about this: `sign()` returns `None` rather than emitting a fake "signature."

### `SkPgpSealer` (gated, opt-in)

Produces a genuine OpenPGP **detached** signature over the at-rest bytes using
the composite **ML-DSA-87 + Ed448** suite, via `sk_pgp` (sequoia-openpgp +
liboqs). The wiring is real and was verified against `sk_pgp` 0.1.0:

- `Key.from_file(path)` → secret key
- `key.sign_detached(at_rest_bytes(memory))` → armored detached signature (bytes)
- `Cert.from_file(path)` → public cert
- `cert.verify_detached(signature, at_rest_bytes(memory))` → `bool`

The signature is intended to live in a **sidecar** (e.g. `<id>.json.sig`) so the
memory JSON file — and its byte-for-byte compatibility with every existing
reader — is never perturbed. `at_rest_bytes` on this sealer is identical to the
classical one; only an *extra* sidecar artifact is added.

This backend is **inert** unless all of:
1. `sk_pgp` is importable (optional extra `skmemory[pqc-seal]`),
2. a signing key path is configured, and
3. it is explicitly selected via `get_sealer`.

If any condition fails, `get_sealer` **honestly falls back to classical** —
enabling PQC can never break memory read/write.

## Resolver

```python
from skmemory.sealing import get_sealer, seal_status

sealer = get_sealer()          # defaults to ClassicalSealer
status = seal_status()         # side-effect-free introspection
```

Config precedence: explicit `config` dict → environment → classical.

| Env var | Meaning |
| --- | --- |
| `SKMEMORY_SEAL_BACKEND` | `classical` (default) \| `sk_pgp` |
| `SKMEMORY_SEAL_SCHEME` | `mldsa87-ed448` (default) \| `mldsa65-ed25519` |
| `SKMEMORY_SEAL_KEY` | path to armored secret key |
| `SKMEMORY_SEAL_CERT` | path to armored public cert (optional; defaults to key) |
| `SKMEMORY_SEAL_PASSWORD` | passphrase (prefer gpg-agent later) |

## Honest crypto claims

- ML-DSA (FIPS 204) and ML-KEM (FIPS 203) are **post-quantum /
  quantum-resistant**, **NOT** "quantum-proof" or "quantum-safe."
- The composite is a **hybrid**: a signature verifies **iff both legs verify**
  (lattice ML-DSA **AND** classical Ed448 per RFC 8032). "Hybrid" means *either
  leg still standing keeps its security assumption intact* — if one primitive is
  later broken, the other still constrains forgery. It is not a magic shield.
- skmemory adds **no cryptography of its own**. All PQC assurance rests on
  `sk_pgp` → sequoia-openpgp + liboqs.

## Maturity (honest T0 → T3)

This is a **seam**, not a deployed control. Where it stands:

- **T0 — Scaffold (DONE, this change).** Interface, classical bit-for-bit
  sealer, gated `SkPgpSealer`, resolver with honest fallback, TDD
  (classical bit-for-bit + checksum + tri-state verify + fallback paths + real
  sk_pgp sign/verify roundtrip when the extra is installed). Unwired from the
  live path.
- **T1 — Opt-in sidecar writer (NOT STARTED).** A wrapper that, when
  `SKMEMORY_SEAL_BACKEND=sk_pgp`, writes `<id>.json.sig` alongside each saved
  memory and verifies it on load. Still additive; classical files keep loading
  with `signature_ok=None`.
- **T2 — Key lifecycle (NOT STARTED).** Source the signing key from CapAuth /
  gpg-agent instead of an env-var path; rotation + revocation story; per-agent
  signing identity (see the skcomms agent-signing-key lesson — agents must sign
  as themselves, not the operator).
- **T3 — Verify-on-read enforcement + audit (NOT STARTED).** Optional strict
  mode that treats `signature_ok is False` as a hard tamper event into the
  Fortress audit chain; migration tooling to back-seal existing memories.

Nothing past T0 exists yet; do not describe this as "memories are PQC-signed."
They are not, by default — and the seam is dormant until a future, separate
change wires T1.
