# Security Policy — SKMemory

SKMemory is **sovereign by design**: your memories live as flat JSON files on
hardware you own, indexed locally, and never shipped to a third-party cloud,
embedding service, or KMS. This document states the threat model, the secret-handling
rules, the crypto posture (honestly), and how to report a vulnerability.

---

## Reporting a vulnerability

- **Private channel first.** Open a private security advisory on the GitHub repo
  (`smilinTux/skmemory` → Security → Report a vulnerability), or contact the
  smilinTux maintainers directly. Do **not** open a public issue for an unpatched
  vulnerability.
- Include: affected version (`skmemory --version` / `pyproject.toml`), the backend
  involved (SQLite / vaulted / Chroma / pgvector / graph), and a reproduction.
- We aim to acknowledge within a few days and to ship a fix or mitigation before any
  public disclosure.

---

## Threat model (summary)

| Asset | Threat | Mitigation |
|---|---|---|
| Flat-file memories (source of truth) | Disk/host compromise; exfiltration | Local-only storage on operator hardware; Syncthing over the operator's own LAN/tailnet; optional `VaultedSQLiteBackend` PGP-seals sensitive memory at rest. |
| Vault passphrase / GPG key | Leak into logs/transcripts | Passphrase supplied via gpg-agent only; **never** stored in repo or config; never logged. |
| Audience leakage (wrong reader) | A private memory surfaced to the wrong channel/person | KYA audience gating (`audience.py`, `data/audience_config.json`, `docs/admission_policy.md`) filters by channel + people trust. |
| Tampering with stored memory | Silent edit/forgery of history | `FortifiedMemoryStore` integrity seals + tamper detection + append-only audit log (`docs/FORTRESS_SOP.md`). |
| Dependency supply chain | Malicious/compromised dependency | Pinned deps in `pyproject.toml`; minimal default install; optional backends are opt-in extras. |

---

## Secret handling rules (hard rules)

- **Never** inline a live secret in the repo, docs, tests, or commit history.
- The vaulted backend seals at rest to the operator's **GPG key**; the passphrase is
  held by **gpg-agent**, not by skmemory.
- Vector/embed endpoints are LAN/local URLs — no API keys leave the box.
- Audience-gated (KYA) and vaulted content must **not** be echoed into unscoped logs.

---

## Cryptography posture (honest claims)

skmemory's only cryptographic surface is **memory-at-rest sealing** via classical
OpenPGP (the `VaultedSQLiteBackend` / `vault.py`), typically Ed25519/RSA key +
AES-256 symmetric. Maturity tier: **T0 — Classical**.

- skmemory performs **no** key exchange, KEM, or signature negotiation of its own.
- There is **no** hybrid post-quantum key establishment today. A hybrid combiner
  `HKDF(X25519 ‖ ML-KEM-768)` (ML-KEM per **FIPS 203**) is **not** integrated; the
  migration path is to seal via [sk_pgp](https://github.com/smilinTux/sk-pgp)/sk_pqc
  during the ecosystem PGP→PQC root cutover.
- We make **no** post-quantum claim. We do not use the words "quantum-proof",
  "quantum-safe", or "unbreakable". AES-256 at rest is **not** described as
  quantum-broken; a future PQ migration concerns the *asymmetric* sealing leg only.

This conforms to the smilinTux
[CRYPTOGRAPHY_STANDARD](https://github.com/smilinTux/sk-standards) honest-claim rules:
every security claim above is surface-scoped and backed by code (`vault.py`,
`fortress.py`, `audience.py`) or by `skmemory health` / the fortress audit log.

---

## Supported versions

Security fixes target the latest released `0.10.x`. Older lines are best-effort;
upgrade to the latest minor for fixes.
</content>
