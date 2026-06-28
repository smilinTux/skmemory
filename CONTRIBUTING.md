# Contributing to SKMemory

Thanks for helping build sovereign AI memory. This guide covers the branch model,
commit convention, the test gate, and the review path. It complements the
[README](./README.md) §Contributing/Development and the [SOP](./SOP.md).

---

## Ground rules

- **Flat files are the source of truth.** SQLite, ChromaDB, pgvector, and the graph
  are derived projections — never introduce a change that makes an index the master.
- **Additive + safe for LIVE.** The cross-node memory/sync paths run on live boxes
  (.158/.41). New behaviour must be **gated** (flag/config) and must not break
  existing flat-file, SQLite, or vector paths.
- **Honest claims.** Don't add a capability/security claim to docs without backing
  evidence (a test, a `skmemory health` line, or a cited spec). See the honest-claims
  gate in [SECURITY.md](./SECURITY.md) and
  [sk-standards](https://github.com/smilinTux/sk-standards).

---

## Branch model

- `main` is always releasable.
- Branch per change: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `security/<slug>`.
- Never commit directly to `main`; open a PR.

---

## Commit convention

- Conventional-style subject (`feat:`, `fix:`, `docs:`, `test:`, `chore:`,
  `security:`).
- End every commit message with the trailer:

  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```

  (Match the actual author model; the SK ecosystem standardises this trailer so
  agent-authored work is attributable.)

---

## Test gate (must pass before merge)

```bash
pip install -e ".[dev,all]"
pytest                 # unit + backend tests
ruff check skmemory/
skmemory health        # backends report live as claimed
```

- **TDD where there is logic:** write/extend a test in `tests/` first for any new
  behaviour (promotion rules, audience gating, decomposition, backend contracts).
  Docs-only changes are exempt from new tests but must keep claims accurate to code.
- New backends implement the `BaseBackend` ABC (`skmemory/backends/base.py`) and ship
  a contract test.

---

## Review path

1. Open a PR against `main` with the per-repo compliance checklist (see
   [sk-standards SK_REPO_DOC_STANDARD §6](https://github.com/smilinTux/sk-standards)).
2. Confirm the §"Test gate" is green and any LIVE/tier/deploy claim is reproducible
   from `skmemory health`.
3. A maintainer reviews for sovereignty (no cloud egress, no inlined secrets),
   correctness, and honest claims, then merges.

---

## Releasing

See [SOP.md §5](./SOP.md). In short: bump `pyproject.toml` + `package.json`, add a
dated `CHANGELOG.md` entry, pass the gate, tag `vX.Y.Z`, CI publishes to PyPI + npm.
</content>
