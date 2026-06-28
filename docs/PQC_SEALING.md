# At-Rest Sealing Seam (PQC-ready)

`skmemory/sealing.py` is an **additive scaffold** that introduces a pluggable
*sealing* seam for the flat-file memory store, mirroring cloud9's Stage-1
sealing pattern (`cloud9/sealing.py`). It lets skmemory move from today's
classical at-rest behaviour to a real post-quantum **detached signature** as a
*configuration change*, not a rewrite.

**Stage-2 (this change) wires the seam into `FileBackend`'s real save/load
path — but only when explicitly opted in.** With the default (classical)
backend, `FileBackend.save` / `load` remain byte-for-byte unchanged and the
live skchat / skcomms daemons are untouched: the classical sealer produces no
signature, so **no sidecar is written** and nothing is verified. A PQC sidecar
is produced *only* when an `sk_pgp` backend is explicitly selected **and** a
signing key is present; otherwise it honestly falls back to classical. See
"Stage-2 wiring" below.

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

## Stage-2 wiring — `FileBackend` sidecars (write side + verify-on-read)

The seam is now wired into the **real** save/load path of
`skmemory.backends.file_backend.FileBackend`, gated and additive:

```python
from skmemory.backends.file_backend import FileBackend

# Default: classical, byte-for-byte unchanged — no sidecar, no verification.
be = FileBackend(base_path="...")

# Opt-in PQC sealing (sign on save, verify on read):
cfg = {"backend": "sk_pgp", "key": "/path/agent-key.asc", "password": "…"}
be = FileBackend(base_path="...", seal_config=cfg)            # writes <id>.json.sig
be = FileBackend(base_path="...", seal_config=cfg, strict_verify=True)  # reject tamper
```

- **`save(memory)`** writes the memory JSON exactly as before, then calls
  `sealing.write_seal(memory, path, config=seal_config)`. The classical default's
  `sign()` returns `None`, so **no sidecar is written** and the on-disk body is
  byte-for-byte today's output. Only a ready `sk_pgp` backend emits a
  `<id>.json.sig` armored composite detached signature. Signing that raises
  (e.g. wrong passphrase) is swallowed — persistence never fails because of
  sealing.
- **`load(memory_id)`** reads the memory, then calls
  `sealing.verify_seal(raw_bytes, path, config=seal_config)` over the **exact
  on-disk bytes**. No sidecar → verdict is `None` and behaviour is unchanged.
  The verdict is exposed on `FileBackend.last_verdict`; an explicit
  `FileBackend.verify_at_rest(memory_id)` returns it on demand. In
  `strict_verify=True` mode a *failed* signature (`signature_ok is False`, i.e. a
  tampered file) makes `load` return `None`; an *unverifiable* signature
  (`signature_ok is None`, e.g. a sidecar with no cert/key to check it) is
  **never** a rejection.
- **`delete(memory_id)`** also removes any `<id>.json.sig` sidecar. The
  `*.json` globs in `list_memories` / `search_text` ignore `.sig` files.

`sealing.get_verifier()` is the read-side resolver: laxer than `get_sealer()`
(it needs only a public cert, not a usable secret key), still defaulting to
classical. `store.py` constructs `FileBackend()` with **no** seal config, so the
live skmemory store stays fully classical until a deployment opts in.

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

- **T0 — Scaffold (DONE).** Interface, classical bit-for-bit sealer, gated
  `SkPgpSealer`, resolver with honest fallback, TDD (classical bit-for-bit +
  checksum + tri-state verify + fallback paths + real sk_pgp sign/verify
  roundtrip when the extra is installed). Unwired from the live path.
- **T1 — Opt-in sidecar writer + verify-on-read (DONE, this change).**
  `FileBackend` now writes `<id>.json.sig` on `save` and verifies it on `load`
  when an `sk_pgp` backend is *explicitly* configured + ready; `strict_verify`
  rejects a *failed* signature. Still additive: the classical default writes no
  sidecar and is byte-for-byte unchanged; classical/legacy files keep loading
  with `signature_ok=None`. TDD: `tests/test_sealing_stage2.py` (classical
  bit-for-bit on the real path, gated sign+verify roundtrip, tamper →
  `signature_ok=False`, strict rejection, unverifiable-is-honest, explicit-cert
  verify, sk_pgp-absent fallback, sign-failure-never-breaks-save).
- **T2 — Key lifecycle (NOT STARTED).** Source the signing key from CapAuth /
  gpg-agent instead of an env-var path; rotation + revocation story; per-agent
  signing identity (see the skcomms agent-signing-key lesson — agents must sign
  as themselves, not the operator).
- **T3 — Verify-on-read *enforcement* + audit (PARTIAL).** `strict_verify`
  exists at the `FileBackend` layer (a failed signature → `load` returns
  `None`); still **not started**: piping a tamper verdict into the Fortress
  audit chain and migration tooling to back-seal existing memories.

`store.py` builds `FileBackend()` with **no** seal config, so by default
**memories are NOT PQC-signed** — do not describe them as such. PQC signing is
dormant until a deployment explicitly opts in via `seal_config` / `SKMEMORY_SEAL_*`
with a real key present.
