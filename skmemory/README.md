# skmemory (package internals)

This document covers internal behaviors of the `skmemory` package that are not
part of the top-level CLI surface. For user-facing usage see the repo-root
[`README.md`](../README.md).

## Recall collection namespacing & consent gating (skcomms T9 + T10)

`recall_collections` (configured in `skmemory.yaml`) name *shared* corpora that
live in the central pgvector store (**skmem-pg**) and are searched in addition
to an agent's own memory. Because that store is shared across operators and
realms, recall-collection resolution is **realm-aware** and **consent-gated**.

`context_loader._load_recall_collections(config_dir)` resolves each configured
name. **This is pure namespacing/gating logic — it never queries or writes
skmem-pg.**

### Resolution rules

For each entry in `recall_collections`:

| Configured form                       | Resolves to                              | Notes |
|---------------------------------------|------------------------------------------|-------|
| `legal-corpus` (bare)                 | `<operator>.<realm>/legal-corpus`        | Auto-prefixed with **own** operator namespace. |
| `chef.skworld/legal-corpus`           | `chef.skworld/legal-corpus` (unchanged)  | Already operator-qualified — trusted as-is. |
| `peer:acme.world/secret-corpus`       | `acme.world/secret-corpus` **only if consented**, else dropped | **Foreign** reference — consent-gated. |

- **own operator / realm** come from `cluster.json` (search order:
  `/etc/skcapstone/cluster.json`, then `~/.skcapstone/cluster.json`; keys
  `operator`, `realm`) — mirroring `capauth.agent_identity`.
- If `cluster.json` is absent/malformed (no realm), bare names **pass through
  untouched** (we can't namespace without a realm) and foreign refs still
  **fail closed**.

### Foreign references (`peer:`) — consent gating

A `peer:<operator>.<realm>/<collection>` reference points at *another*
operator's collection. It is **rejected (dropped, with a logged warning)**
unless a matching, unexpired consent token grants read on that exact
`<operator>.<realm>/<collection>` to the running agent's **fqid**.

**Fail-CLOSED:** if the consent file is missing, empty, or malformed, **all**
foreign refs are dropped.

The running agent's fqid is resolved via `capauth.resolve_agent_identity().fqid`
when available, otherwise derived locally from `SKAGENT` (env) + `cluster.json`
operator/realm. When the fqid is unknowable, foreign refs fail closed.

### Consent-file schema

Read from `${SKCOMMS_HOME:-~/.skcomms}/recall_collections_consent.json`
(the `SKCOMMS_HOME` env var overrides the default `~/.skcomms`):

```json
{
  "tokens": [
    {
      "collection": "<operator>.<realm>/<name>",
      "granted_to": "<fqid>",
      "granted_by": "<fqid>",
      "expires":    "<iso8601>",
      "signature":  "<pgp armor>"
    }
  ]
}
```

A token **grants read** when, for the foreign collection in question:

1. `collection` matches the exact `<operator>.<realm>/<collection>` string, **and**
2. `granted_to` equals the running agent's fqid, **and**
3. `expires` is in the future (parsed as ISO-8601; naive timestamps treated as UTC), **and**
4. the signature verifies — see below.

> **Signature verification (T10, wired).** `_verify_consent_signature(token)`
> now delegates to **`skcomms.grants.verify_grant(token)`**, which checks the
> detached PGP signature over the token's canonical bytes against the granter's
> public key, **TOFU-trusts** that key for `granted_by` (rejecting fingerprint
> conflicts), and re-checks expiry. The granter's public key is resolved by
> skcomms from its own TOFU/peer store — `${SKCOMMS_HOME:-~/.skcomms}/known_fingerprints.json`
> (cached `pubkey`), falling back to `<SKCOMMS_HOME>/peers/<granted_by>.asc`.
> skmemory never opens a private keyring.
>
> **sk-integration dual-mode / fail-closed.** skcomms is an *optional* import.
> If `from skcomms.grants import verify_grant` raises `ImportError` (standalone
> skmemory), or the verifier raises, or it returns `valid=False` (bad/tampered/
> wrong-key signature, untrusted/unknown granter key, fingerprint conflict, or
> expiry), the foreign reference is **dropped** and a one-line reason is logged.
> skmemory never crashes when skcomms is absent — it just enforces consent
> when skcomms is present and fails closed when it is not.

> This consent file is **produced by skcomms** (`skcomms.grants.mint_grant` /
> `accept_grant`); skmemory only *reads* and *verifies* it.

### Related config

- `_load_recall_graphs` / `_load_shared_corpora` (cross-graph search) are
  unchanged by T9 and are not consent-gated here.
