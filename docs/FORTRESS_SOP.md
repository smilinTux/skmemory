# Memory Fortress — Standard Operating Procedure

**Owner:** skmemory maintainers
**Status:** active (cron'd verify shipped 2026-05-10)
**Threat model:** memory poisoning, tampering, silent drift of the agent's
recall surface — sharpened by [Souly et al. 2025](https://arxiv.org/abs/2510.07192),
which showed ~250 poisoned docs can compromise an LLM regardless of clean-data
scale. Continuous integrity verification is the floor, not the ceiling.

---

## What the Fortress actually does

Three layers, each independently verifiable:

| Layer | Mechanism | CLI |
|-------|-----------|-----|
| **Integrity hashes** | SHA-256 of canonical memory JSON, sealed on write, verified on read | `skmemory fortress verify` |
| **Audit trail** | Chain-hashed JSONL log of every store/recall/delete | `skmemory fortress audit`, `verify-chain` |
| **At-rest encryption** | Optional PGP wrap of memory files | (advanced — opt-in) |

The integrity-hash layer is the one this SOP automates. Hashes are written
the moment a memory is stored; verification at any later time confirms the
file on disk still hashes to the same value. Any drift = tamper or corruption.

---

## Daily verify (automated)

A per-agent systemd timer runs `skmemory fortress verify` at **03:00 local
time**, randomized ±5 min. Pass: silent. Fail or tamper: log + optional
Telegram alert.

### Install

```bash
cd ~/clawd/skcapstone-repos/skmemory
scripts/install-systemd.sh
```

Interactive prompts will ask:
- Which agents to install for (e.g. `lumina,opus,jarvis`)
- Whether to install the sync timer (every 6h)
- Whether to install the fortress-verify timer (daily 3 AM)
- Whether to wire the Telegram alert hook

Non-interactive:

```bash
scripts/install-systemd.sh --agents lumina,opus --fortress --telegram-hook
```

To remove:

```bash
scripts/install-systemd.sh --uninstall --agents lumina
```

### Verify install

```bash
systemctl --user list-timers 'skmemory-fortress-*'
```

Expect one line per agent showing next-fire ~next 03:00 local.

---

## On-demand verify

```bash
# Direct CLI (current agent from $SKAGENT)
skmemory fortress verify

# Force a systemd run for a specific agent
systemctl --user start skmemory-fortress-verify@lumina.service

# JSON for tooling
skmemory fortress verify --json
```

---

## Outputs and where they live

| Artifact | Path | Purpose |
|---|---|---|
| Log (append-only) | `~/.skcapstone/agents/<agent>/logs/fortress-verify.log` | Every run, timestamped |
| Last result (JSON) | `~/.skcapstone/agents/<agent>/fortress/last-verify.json` | Machine-readable summary |
| Last result (text) | `~/.skcapstone/agents/<agent>/fortress/last-verify.txt` | `OK|TAMPER|FAIL <ts> <summary>` |
| Audit chain | `~/.skcapstone/agents/<agent>/memory/audit.jsonl` | All store/recall/delete ops |

---

## Alerting

The verify driver (`scripts/fortress-verify.sh`) calls an alert hook on
`TAMPER` or `FAIL` (not on `OK`). Hook resolution:

1. `$SKMEMORY_FORTRESS_ALERT_CMD` env var (set in the systemd service unit if you want it)
2. `~/.skenv/bin/skmemory-fortress-alert` (auto-detected if executable)

A sample Telegram hook ships at `scripts/fortress-alert-telegram.sh`. It
pulls `TELEGRAM_BOT_TOKEN` from `~/.hermes/.env` and sends to
`$SKMEMORY_FORTRESS_ALERT_CHAT_ID` (or the first `TELEGRAM_ALLOWED_USERS` ID
as fallback). Symlink it via the installer's `--telegram-hook` flag, or
manually:

```bash
ln -sf ~/clawd/skcapstone-repos/skmemory/scripts/fortress-alert-telegram.sh \
       ~/.skenv/bin/skmemory-fortress-alert
```

To write a different alert hook (Slack, email, ntfy, Signal, etc.): any
executable that reads JSON on stdin and exits 0 works. JSON schema:

```json
{
  "ts": "2026-05-10T18:00:00Z",
  "agent": "lumina",
  "total": 7251,
  "passed": 7251,
  "tampered": [],
  "unsealed": ["mem-id-1", "mem-id-2"],
  "verify_rc": 0
}
```

---

## Response to a TAMPER alert

The alert tells you which memory IDs failed. The procedure:

1. **Don't write to the store** until investigation is complete. New writes
   may overwrite forensic state.
2. **Snapshot the audit log:**
   ```bash
   cp ~/.skcapstone/agents/<agent>/memory/audit.jsonl /tmp/audit-snapshot-$(date +%s).jsonl
   ```
3. **Pull the affected memory:**
   ```bash
   skmemory show <mem-id>
   ```
4. **Verify the audit chain itself:**
   ```bash
   skmemory fortress verify-chain
   ```
   A broken chain means the audit log was edited — much more serious than
   a single tampered memory.
5. **Cross-reference against backup.** Syncthing replicas on other nodes
   may hold a clean copy. Compare hashes.
6. **Investigate the injection surface** (per Souly threat model): system
   prompt (`SOUL.md`, `LUMINA.md`), `USER PROFILE` block, recent SKWhisper
   curation runs. A poisoned upstream often shows up as tampered downstream.
7. **Document in `~/.skcapstone/agents/<agent>/journal.md`** with timestamp,
   IDs, root cause, remediation.

---

## Response to a FAIL alert

`FAIL` means the verify command itself didn't return clean output (store
unreachable, skmemory binary missing, etc.). Usually environmental, not
adversarial. Check the log:

```bash
tail -50 ~/.skcapstone/agents/<agent>/logs/fortress-verify.log
```

Common causes: skmemory not installed in `~/.skenv/bin`, agent home dir
missing, file backend path misconfigured.

---

## Phantom-field hygiene

The `intent` field on `Memory` was added during AMK integration. As of
2026-05-10, only **4 of 7,251** short-term memories on Lumina's index have
a populated intent — the field is being declared but not written. The
fortress integrity hash doesn't depend on intent being populated, so this
is a separate hygiene concern: either backfill from the message context at
save time, or remove the field. Tracked separately from the verify cron.

---

## Health metrics worth watching

- **Tamper count** — must be 0. Anything else is an incident.
- **Unsealed count** — memories without a hash. Should trend to 0 as
  `Memory.seal()` is universal on write. A rising number means a writer is
  bypassing `FortifiedMemoryStore`.
- **Audit chain length** — should only grow. A shrinking length means the
  log was truncated.
- **Last-verify timestamp** — `cat ~/.skcapstone/agents/<agent>/fortress/last-verify.txt`.
  If it's >36h old, the timer didn't fire.

---

## Related

- `skmemory fortress --help` — full CLI surface
- `~/clawd/skcapstone-repos/skmemory/skmemory/fortress.py` — implementation
- `systemd/README.md` — unit template install docs
- AMK origin: Jonathan Clements' Agent Memory Kernel framework
