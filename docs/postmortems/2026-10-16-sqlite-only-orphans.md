# Postmortem: 23,525 SQLite-only memory orphans on the .41 laptop (Lumina)

- **Status:** final
- **Severity:** SEV-1 (six months of unsynced, undetected, partially unrecoverable memory loss)
- **Detected:** 2026-10-16, during unrelated `skmemory` install/triage work on the .41 laptop
- **Window of harm:** approximately 2026-04 through 2026-10-16 (confirmed by `created_at`
  distribution, see Evidence)
- **Author:** Lumina (skmemory), investigation + fixes 2026-10-17
- **Scope of this document:** root cause, detection failure, recovery mechanics, and the
  permanent fixes. This document does not restate or duplicate data-recovery results; that
  work is tracked separately and is still in progress as of this writing.

## 1. Summary

On 2026-10-16 a newly-run `skmemory sync` on the `.41` laptop found 23,525 rows in Lumina's
local SQLite index (`index.db`) with no backing flat JSON file. Flat files are skmemory's
source of truth and the only thing Syncthing replicates between nodes; `index.db` is a
per-node, unsynced derived cache (by design: it is explicitly listed in every `.stignore`
found on the estate, as `**/memory/index.db`). Because the index row existed but the flat file
never did, these 23,525 memories had **never synced anywhere**, for as long as six months,
and nothing surfaced that fact.

The rescue made things worse before it made them better:

1. The first `skmemory sync` exported the orphans as flat files built from the SQLite index's
   `content_preview` column (a ~150-character truncation, not the original content), and
   flagged them `metadata.recovered_from_sqlite_preview: true`.
2. A nightly flat-to-pg reconcile job (`skmem_reconcile.py`, unversioned, run from cron) treated
   373 of those stub flat files as "missing from pg" and inserted the truncated stub content
   directly into `skmem-pg`, overwriting the one place (pg) that in some cases still held the
   real content never reached.
3. Recovery then had to mine three off-schedule, undocumented full `pg_dump`s (2026-07-06,
   2026-08-06, 2026-08-20) because the normal nightly backup only reaches back 7 days (see §4.1:
   the retention is actually half of what every comment in the repo claims).

**Final recovery tally** (data recovery is a separate, concurrent effort; recorded here for
the narrative record only, not re-verified by this document): of 24,360 flagged/truncated
items, 22,742 had their full text restored, 903 digests were superseded by 32 regenerated
syntheses (see §3.11: the digests themselves had been silently incomplete), 571 items were
identified as pytest/CI test fixtures and tagged as such rather than "recovered" (see §3.9),
and approximately 180 are genuinely unrecoverable because their only source (a flat file, a
pg row, and the raw session or transcript they were extracted from) no longer exists anywhere
that has been found.

## 2. Timeline

All times are from git history, systemd/cron state, and `~/skstacks-sdd/progress.md` on the
laptop. This is reconstructed after the fact; most of the April to October span produced no
record at all, which is itself a finding (§3.3, §3.4).

| When | Event |
|---|---|
| 2026-04-25 | skmemory v0.9.8 ships `drift_check()`, `skmemory sync`, and the `skmemory-sync@<agent>.timer` systemd template + installer docs (commit `13d7ed0`). This is the first version of skmemory capable of detecting or healing this exact failure mode. |
| 2026-04-26 | First weekly run of `skmemory-sync-audit.sh` (cron on `.158`) logs `No active skmemory-sync@*.timer instances. Nothing to audit.` and exits 0. |
| 2026-04 to 2026-10 | Lumina's memory volume on the laptop ramps up sharply (telegram import, Claude Code / Hermes session-end hooks, skwhisper/dreaming-engine digests). `created_at` distribution from the laptop's SQLite index shows April alone added ~6,576 rows, including 5,544 `source=telegram` and 757 `source=hook:session-end`, exactly the sources recovery later found disproportionately represented among the orphans. The laptop experiences routine sleep/suspend and occasional hard power-offs throughout this period; each one is a chance for a page-cache-only flat write to be lost (§3.1). |
| 2026-04-26 to 2026-10-11 (weekly) | `skmemory-sync-audit.sh` logs the identical `No active skmemory-sync@*.timer instances. Nothing to audit.` line **every single week for six months**, on `.158` only. It never ran on the laptop at all (no equivalent cron entry there). |
| 2026-10-16 (day) | `skmemory` installed/fixed on the laptop as part of unrelated work; `skmemory sync` run for the first time. |
| 2026-10-16 22:21Z | `index.db` backup taken on laptop (`~/backups/skmemory-lumina-index-20261017T022138Z.db`, 45,617 rows total), used read-only for this postmortem's evidence (§3's distribution). |
| 2026-10-16 22:25 (laptop local) | Laptop `.bashrc` edited to pin `SKMEMORY_PG_DSN` at `localhost:5432` and `EMBED_URL` at node-local ollama, part of fixing the laptop's per-node pg setup (prb-6f069c5e, previously marked `done`, in fact still incomplete; see §3.6). |
| 2026-10-16 22:36/23:36/22:41 | `skmemory-sync@{opus,jarvis,lumina}.timer` started on `.158`/laptop by the recovery coordinator: first activation found in `systemctl --user list-timers` history. |
| 2026-10-16 (night) | `export_orphans_to_flat()` rescues 23,525 sqlite-only rows as `content_preview`-based stub flat files, flagged `recovered_from_sqlite_preview: true`. A subsequent flat-to-pg reconcile pass writes 373 of those stubs into `skmem-pg`, laundering the truncation into the one remaining full-content store before the coordinator paused reconciliation (crontab lines commented `#PAUSED-RECOVERY-20261016`). |
| 2026-10-16 22:42 | skmemory PR #32 merged (`6762c42`/`22265f3`): durable atomic flat writes, truthful `health()` status, loud orphan-rescue warning. |
| 2026-10-17 02:57Z | `skmem-pg-skmemory-*.dump` 7-day retention confirmed as the actual ceiling (see §4.1); recovery falls back to three off-schedule full dumps from Jul/Aug. |
| 2026-10-17 03:02Z | A dedicated `skmem-pg` container is created on the laptop for the first time this investigation can find evidence of (`docker inspect` `Created` timestamp). Before this, the laptop had no local pg instance to reconcile against at all (§3.6). |
| 2026-10-17 (day) | skmemory PR #34 merged (`c01c1c7`): `export_orphans_to_flat()` tries the pg/vector backend's full content before falling back to the lossy preview, and refuses a fallback candidate that is itself a laundered stub. Recovery phase 2 mines the Jul/Aug full dumps; this document's fixes (§5) are written. |
| 2026-10-17 (day, follow-up) | Coordinator finding: ~536 of Lumina's memories are pytest/CI test fixtures written into the real store by a consuming repo's test suite (§3.9); skmemory PR #39 adds a library-level write guard. Hermes session JSONL retention gap also noted (§3.10). |

## 3. Root causes (ranked)

Each is independently verified against code, git history, or live host state, not inferred
from the PR #32 commit message alone, per the brief for this investigation.

### 3.1 Non-durable flat writes (PRIMARY, FIXED, PR #32)

Confirmed by reading the pre-fix code directly (`git show 6762c42^:skmemory/backends/sqlite_backend.py`):
every backend's `save()` wrote the flat JSON with a plain `Path.write_text()`, which only
guarantees the bytes reach the OS page cache, not disk. On an unclean shutdown (sleep,
battery cutoff, forced reboot: routine on a laptop, rare on a server) that write can vanish
while the SQLite WAL commit for the *same* memory survives its own checkpoint, leaving an
index row with a `file_path` that points at nothing. This explains the breadth of affected
sources (telegram import, hooks, hermes, skwhisper, dreaming-engine, CLI): they all funnel
through the same backend `save()`.

Verified NOT a contributing factor, by reading the file's own `git log --follow -p`: the flat
write has always been ordered *before* the index write, in every version back to the file's
creation. There is no historical "index written before flat file" ordering bug.

**Fix:** `skmemory/atomic_io.py` (temp file + `fsync` + atomic rename), wired into
`FileBackend.save()`, `SQLiteBackend.save()`, `SQLiteBackend.import_backup()`, and
`VaultedSQLiteBackend.save()`. A failed durable write now raises and the caller never reaches
the index call. Shipped and merged (PR #32, `6762c42`).

### 3.2 The one instrument that could have caught this reported false-green (PRIMARY DETECTION FAILURE, FIXED, PR #32)

Confirmed by reading `SQLiteBackend.health_check()` before PR #32: it set `"ok": True`
unconditionally whenever `stats()` and `drift_check()` *executed without raising*, with the
actual drift counts (`sqlite_only`, `flat_only`, `in_sync`) buried three levels down inside a
nested `sync` dict that no caller's top-level status reflected. `MemoryStore.health()` passed
that `{"ok": True, ...}` straight through. So even on a day someone ran `skmemory health` by
hand or had a script watching for `ok: false`, a laptop sitting on 20,000+ sqlite-only rows
reported exactly the same top-level health as a perfectly synced node. This is the same
failure shape as [[measurement-indistinguishable-from-success]] / [[instrument-blind-to-the-defect]]:
the drift number existed in the payload, but nothing treated it as the actual health signal.

**Fix:** PR #32 adds an explicit top-level `status` (`ok` / `degraded` / `red`) computed from
the drift counts themselves, not from whether the query succeeded.

### 3.3 The one piece of automated drift monitoring that existed never actually monitored anything (DETECTION, FIXED, this work)

`~/.skcapstone/scripts/skmemory-sync-audit.sh` already existed, already ran weekly from cron
on `.158`, and already DMs Chef on Telegram when it finds drift. Its log
(`~/.skcapstone/logs/skmemory-sync-audit-cron.log`) shows it ran every week from 2026-04-26
through 2026-10-11 and logged, every single time:

```
=== skmemory-sync audit <date> 09:00 EDT ===
No active skmemory-sync@*.timer instances. Nothing to audit.
```

Two independent bugs compound here:

1. **Silent-pass-on-zero-targets.** The script discovers agents to audit by listing *active*
   `skmemory-sync@*.timer` units (`systemctl --user list-units ... --state=active`). When that
   list is empty (which it was, continuously, because the timers were never enabled, §3.4),
   the script prints "Nothing to audit" and exits 0 with no Telegram message at all. Zero
   monitored targets was silently treated as "all clear" instead of as the alarming condition
   it actually is (zero coverage). This is exactly [[short-ci-check-list-means-no-tests-ran]]
   applied to a health audit instead of a test suite.
2. **Single-host, not fleet-aware.** Even with active timers, the script only ever ran on
   whichever host's cron invoked it (`.158`) and only evaluates `SKAGENT=<agent> skmemory
   health` *on that host*. It has no mechanism to reach out and audit the laptop's own index.
   There was never a copy of this script, or any equivalent, in the laptop's crontab.

**Fix (this work, §5.2):** the audit script now alerts loudly when it finds zero active
timers instead of exiting quietly, discovers every agent with a memory home on the host
(not just agents with an active timer), and alerts via `sk-alert` instead of its own raw
Telegram call. `skcapstone doctor` also gets a fleet-wide, per-node, per-agent memory-drift
check wired to `sk-alert` so the check runs everywhere an agent lives, not just wherever one
cron job happens to be scheduled. Shipped: skmemory PR #38.

### 3.4 The sync timer existed in-repo for six months and was never actually installed (ROOT CAUSE OF THE DETECTION GAP, FIXED, this work)

skmemory has shipped `systemd/skmemory-sync@.{service,timer}` and a working installer
(`scripts/install-systemd.sh`) since v0.9.8 (2026-04-25). Nothing in `scripts/install.sh` (the
repo's own install entrypoint, also invoked by `skcapstone install`) calls that installer.
Installing skmemory for an agent has never, by itself, turned on the one piece of automation
that would have exported orphans every 6 hours and kept `health()`'s drift counters moving.
Live `systemctl --user list-timers` history on both `.158` and the laptop shows `Last
trigger: -` for `skmemory-sync@lumina.timer` until the night of 2026-10-16: the timer unit
files existed on disk, but nothing had ever started them.

**Fix (this work, §5.3):** `scripts/install.sh` installs and enables
`skmemory-sync@<agent>.timer` for every agent home it provisions; `skcapstone doctor` fails
(not just warns) when an agent has a home on a host but no active sync timer for it.

### 3.5 `skmem_reconcile.py` and `PGVectorBackend.save()` have no concept of "don't make it worse" (FIXED, this work, §5.1)

Two independently-verified writable paths into `skmem-pg` have no length or provenance check
on the content they write:

- `skmem_reconcile.py` (`~/skmem-build/skmem_reconcile.py:61-86`, lived outside version
  control until this work): for any flat-file id not already present in `skmem-pg`
  (`missing`), it reads the flat JSON's `content` field directly and `INSERT ... ON CONFLICT
  (id) DO NOTHING`s it in. It never existed before pg; it exists now with whatever content
  the flat file holds. When the flat file is itself a `recovered_from_sqlite_preview` stub,
  exactly the state 23,525 files were in on the night of 2026-10-16, this inserts the stub
  as if it were real content, with no warning. This is precisely what happened to 373 ids
  (confirmed in `~/skstacks-sdd/progress.md`, 2026-10-17T03:40Z entry).
- `PGVectorBackend.save()` (`skmemory/backends/pgvector_backend.py:414-444`): `INSERT ... ON
  CONFLICT (id) DO UPDATE SET content=EXCLUDED.content, ...`, an **unconditional** overwrite
  of existing pg content, with no check of either length or the stub marker. Any ordinary
  `skmemory sync --vector` run (the exact job the fixed-and-enabled systemd timer now runs
  every 6 hours, §3.4) that reindexes a stub flat file into the vector backend hits this same
  path and will happily overwrite a previously-good pg row with a 150-character preview, with
  no log line at all.

**Fix:** both call sites now refuse to write content carrying the stub marker, and refuse to
replace a longer existing pg row with a shorter one, without an explicit override flag; both
log loudly (not silently) on every refusal. Shipped: skmemory PR #36 (pg backend), PR #37
(reconcile script, also brought under version control).

**Correction, found during deployment of this fix (see §3.12):** `~/skmem-build/skmem_reconcile.py`
on `.158` was NOT actually the file PR #37 fixed. It was (and had been since 2026-09-13) a
symlink into a FOURTH copy of this script, in a different repo entirely, whose fix-carrying
commit had only ever landed on a feature branch, never on that repo's own main. The stub
guard did not reach the file cron actually executes until that was corrected directly during
this investigation (see §3.12 for the full finding and §6 for what is now live where).

### 3.6 Per-node pg was designed but never actually deployed to the laptop (CONTRIBUTING, PARTIALLY FIXED, this work hardens it)

`docs/skmem-pg-local-rebuild-remediation.md` (problem card `prb-6f069c5e`, status `done` in
`skcapstone coord` before this investigation) documents, in its own words, that the laptop's
skmem-pg was "a frozen, non-functional streaming standby" and that every agent's *synced*
`skmemory.yaml` hardcoded `pgvector_dsn=192.168.0.158:5432`, making `.158` a single point of
failure for a store the architecture says must be per-node. The remediation plan's own
pre-flight step reads "Deploy the reconcile engine to .41 (**absent today**)". `docker
inspect skmem-pg --format '{{.Created}}'` on the laptop today returns `2026-10-17T03:02:40Z`:
the container did not exist, in any form this investigation can find evidence of, before
the night of this incident.

Practical effect: for most or all of the April-to-October window, the laptop either had no
working local pg at all, or was writing (if anything) to `.158`'s pg over the network. Either
way, the laptop never had a local secondary store to cross-check its own flat/sqlite state
against, and `skmem_reconcile.py` (also a backfill path, not just a backup) never ran there
until this week. This did not *cause* the sqlite-only orphans (§3.1 did), but it removed a
redundancy path that might otherwise have surfaced or partially mitigated them sooner.

As of 2026-10-16 22:25 (laptop), `SKMEMORY_PG_DSN` is correctly pinned to
`postgresql://...@localhost:5432/skmemory` and a local `skmem-pg` container now exists. The
DSN is per-node-correct as of this writing on both hosts checked. **Fix (§5.5)** hardens this
going forward so a synced config or env file can never silently re-point a node at another
node's pg again, regardless of how prb-6f069c5e's actual completion state drifts in the
future.

### 3.7 Exception handling that would silently drop a save today (LATENT, FIXED, this work)

`skmemory/hooks/claude_code_hooks.py` (`handle_stop`, `handle_precompact`) wraps every
`store.snapshot()` call in `try: ... except Exception as exc: logger.warning(...)`. This did
not cause the original orphans: before PR #32, a non-durable `write_text()` essentially never
raises on a normal filesystem, so there was nothing for this `except` to swallow. But now that
PR #32 makes a failed durable write `raise OSError`, this call site catches that raise and
reduces it to a log line nobody was watching (confirmed: the only consumer of these hook logs
today is the hook's own stdout, not any alert path). A full disk, a permissions problem, or any
other durability failure on a future laptop would be dropped exactly as silently as before,
just for a different underlying reason.

**Fix:** covered by the same drift-detection wiring in §5.2 (the hourly/daily health check
will surface a growing sqlite_only/flat_only count regardless of which call site dropped the
write), plus the hook's warning now also increments a counter `skmemory health` reports, so a
spike is visible instead of only ever living in a log file nobody tails.

### 3.8 Ruled out

Investigated and found **not** to be contributing factors for this incident:

- **`.stignore` exclusion of memory files.** Read every `.stignore` reachable from
  `~/.skcapstone` on both hosts. Flat JSON files under `memory/{short,mid,long}-term/` are
  never excluded on any host checked. `index.db`, `index.db-{wal,shm}`, and ChromaDB state
  *are* correctly excluded, by design, since those are per-node derived caches, exactly as
  documented in the user's own memory notes. No misconfiguration found.
- **Path renames between layers losing files without updating the index.** `grep`'d every
  `unlink`/`os.remove` call in the `skmemory` package: all are either explicit
  single-memory deletes (which remove the index row in the same call), backup pruning, or
  corrupt-index quarantine (which only ever touches `index.db*`, never a flat file). No
  silent file loss during promotion/layer-move found.
- **Dedupe/consolidate/promote deleting flat files.** `skmemory/promotion.py` and
  `skmemory/store.py` contain no file-deletion calls at all; promotion only calls
  `backend.save()`.
- **Syncthing deleting files after a conflict.** `.stversions` (Syncthing's own conflict/trash
  mechanism) is itself excluded from sync, and no `.sync-conflict-*.json` pattern is treated
  specially by skmemory in a way that would delete the canonical file. Not reproduced or
  found in any log.

### 3.9 Test fixtures written into the real memory store (CONTRIBUTING, SEPARATE FAILURE CLASS, FIXED, this work)

Coordinator finding, confirmed during this investigation's follow-up: roughly 536 of
Lumina's REAL memories (tagged `source=claude-code`/`cli`/`generic`, plus some `skchat`-sourced
ones) are pytest/CI test fixtures, not real content: canned strings like "Memory item N",
"e2e-test-peer", and "Sovereign Squad". This is the same failure class already named twice in
the user's own memory notes, [[test-suites-write-to-the-real-fleet]] (cloud9's CLI tests
registered real scheduler jobs) and [[pytest-mints-real-joules-into-live-wallet]] (skharness
tests minted real economic transactions): a test suite that imports a module resolving "the
active agent's real home" with no isolation, writes real artifacts into it.

skmemory's OWN test suite is isolated: `tests/conftest.py` sets `SKCAPSTONE_HOME` to a
throwaway temp directory, at module level, before any test file is collected. That is not
the gap. The gap is any OTHER repo that imports skmemory (skchat, skcapstone, skharness, or
anything calling skmemory's MCP tools or `MemoryStore` directly) with an ambient
`SKAGENT=lumina` in the shell and no equivalent isolation of its own. Checked skcapstone's
own suite directly: its `tmp_agent_home`/`initialized_agent_home` fixtures exist, but they
are opt-in, not autouse; a test that forgets to request them and still touches skmemory
resolves and writes to the real home.

This is a distinct contributing factor from §3.1 to §3.8 above: it does not explain the
23,525 sqlite-only orphans (those are a durability/detection problem), but it pollutes the
same real memory store the recovery had to reason about, and it is the same class of defect
("tests touch live state because nothing stopped them") that makes an incident like this
harder to diagnose, since a recovery agent now also has to separate real content from test
noise by content inspection rather than by any structural marker.

**Fix (this work, §5.7):** new `skmemory/pytest_guard.py`. Every flat-file write path
(`FileBackend.save()`, `SQLiteBackend.save()`, `SQLiteBackend.import_backup()`,
`VaultedSQLiteBackend.save()`) now refuses the write when `PYTEST_CURRENT_TEST` is set (an
active pytest test), the target path is under the real, unmodified
`~/.skcapstone/agents/` tree, and nothing explicitly isolated the home
(`SKCAPSTONE_HOME`/`SKMEMORY_HOME` pointing elsewhere) or opted in
(`SKMEMORY_ALLOW_PYTEST_REAL_WRITES=1`). This is a library-level backstop that protects every
consumer automatically; it does not replace proper isolation, and this document recommends
(does not mandate, since it is out of this investigation's repo scope) that skcapstone and
skchat adopt an autouse fixture following the pattern already in skmemory's own
`tests/conftest.py`. Shipped: skmemory PR #39.

### 3.10 Raw session sources (Hermes JSONL, Claude Code transcripts) are not retained long enough to back up memory recovery (CONTRIBUTING, RECOMMENDATION ONLY)

Coordinator finding: Hermes session JSONL files referenced by several `hook:pre-compact`
memories had aged out on both hosts by the time recovery needed them; the data-recovery
agent recovered what it could from `~/.hermes/state.db` instead. Separately, several Claude
Code transcript files that `hook:pre-compact`-sourced memories point at (via
`source_ref`) no longer exist on either host.

This mattered here because a flat memory file is sometimes a lossy *extraction* from a raw
session (an LLM-generated summary, not the verbatim transcript), so when the extracted
memory itself turned out to be an orphan or a stub, the raw session that could have let a
human or agent re-derive it was sometimes already gone too, independent of anything
skmemory does. This document does not recommend deleting anything to fix this (the opposite
problem, over-retention, is not in scope here) and does not recommend skmemory take on
session-transcript retention itself, since that is Hermes's/Claude Code's data, not
skmemory's. It is recorded here as a contributing factor to *why the rescue was lossy*
(§1, item 3) and as a recommendation for whoever owns that retention policy:

**Recommendation (not actioned in this investigation, no code change; raise with the Hermes/
Claude Code session-retention owner):** extend raw session-source retention (Hermes JSONL,
Claude Code transcript files) to outlive the window any memory derived from them could
plausibly need re-deriving, and prefer `~/.hermes/state.db` (already a fallback source,
already durable per the SQLite durability model this repo now uses, see §3.1) as a
documented, intentional secondary source for session reconstruction rather than an
accidental one discovered mid-recovery.

### 3.11 `synthesize_daily`/`synthesize_weekly`/`synthesize_dreams` silently returned empty for historical dates (CONTRIBUTING, SEPARATE FAILURE CLASS, FIXED, this work)

Coordinator finding, confirmed and fixed during this investigation's follow-up:
`skmemory/synthesis.py` built its daily, weekly, and dream-cluster narratives by fetching the
`limit=500` (daily) or `limit=1000` (weekly, dreams) MOST RECENT memories via
`list_memories()`, then filtering that already-truncated list to the requested date range
(and, for dreams, to `source == "dreaming-engine"`) in Python. In a store with more memories
than that limit, which Lumina's had been for most of this incident's window, any date whose
matching memories fell outside the most-recent 500/1000 silently produced zero results: not
an exception, not a log line, a daily narrative that reads "No memories recorded" and is
indistinguishable from a day with no activity. Verified: the same historical date went from 0
to 433 memories once queried correctly. For `synthesize_dreams` specifically the risk was
worse than the date math alone suggests, because dreams are a small fraction of a large
mixed-source store: an unscoped "N most recent of anything" fetch could fill its entire limit
with non-dream memories before ever reaching a dream, recent or historical.

This is a distinct failure class from §3.1 to §3.10: it does not touch the flat/SQLite/pg
durability chain at all, and it predates this incident's window (the anti-pattern is in the
original `synthesize_*` implementations, not something the orphan recovery introduced). It
is recorded here because it was found WHILE investigating this incident, it is the same
[[measurement-indistinguishable-from-success]] shape as §3.2 and §3.3 (a wrong answer that
looks identical to a correct one), and the 903 superseded digests in the final recovery tally
(§1) exist precisely because historical daily/weekly syntheses generated under this bug had
to be regenerated once the query was fixed.

**Fix (this work):** `list_memories()` across every backend (`SQLiteBackend`, `FileBackend`,
`PGVectorBackend` with real SQL predicates; the deprecated, non-live ChromaDB/SKVector
backends with an interface-compatible post-fetch filter) and `MemoryStore` now accept
`created_after`/`created_before` (ISO 8601 bounds, applied in the query BEFORE `limit`
truncates) and `source`. All three `synthesize_*` methods now pass these instead of pulling
the N most recent and filtering afterward; `synthesize_dreams` also filters by
`source="dreaming-engine"` in the query itself. The three result-size caps are now named,
monkeypatchable module constants (generous ceilings against a pathological single day/week,
not a recency window). Shipped: skmemory PR #41.

### 3.12 The "one" reconcile script was actually four, and the live one was on an unmerged branch (ROOT CAUSE OF WHY §3.5's FIX DIDN'T REACH PRODUCTION, FIXED, this work)

Found while deploying the §3.5 fix, not before: `~/skmem-build/skmem_reconcile.py` (the path
every node's crontab actually invokes) is a symlink, and on `.158` it pointed at
`~/clawd/scripts/skmem_reconcile.py`, a file in a completely different repo (Chef's personal
`~/clawd` / `lumina` monorepo, not `skcapstone-repos/skmemory`). That file WAS already
version-controlled, by a commit from 2026-09-13 ("put it under version control", authored by
this same agent identity in an earlier session), over a month before this incident. But that
commit landed only on a feature branch (`fix/retire-model-name-allowlist`), never on that
repo's own `main` -- so the live symlink has been alive, this whole time, only because a
shared checkout happened to be sitting on that branch. Had anyone followed the estate's own
hygiene rule ("leave shared checkouts on main when you finish") and switched it back, the
symlink's target would have vanished and the nightly cron job would have started failing with
no file to run, silently, exactly the way the audit script's own silence went unnoticed for
six months (§3.3).

Counting every copy this investigation found: (1) that `~/clawd` repo's copy, the one
actually live; (2) `skcapstone-repos/skmemory/scripts/skmem_reconcile.py`, added by this
investigation's own PR #37 before this duplication was noticed; (3)
`skcapstone-repos/skmemory/deploy/skmem-pg/skmem_reconcile.py`, an older, separately-evolved
lineage with its own prune-guard safety net (card `6b8b3ced`) that the other two copies lack
entirely; (4) `skcapstone-repos/skmemory/skmemory/reconcile.py`, a 754-line importable,
unit-tested engine that neither cron copy calls. None of the four knew the others existed.
PR #37's stub guard (§3.5) was real, tested, and merged, and never ran in production for a
single day, because it was never the file `~/skmem-build/skmem_reconcile.py` pointed at on
the host that matters.

**Fix (this work):** fast-forwarded the 2026-09-13 commit onto `~/clawd`'s actual `main`
(so the live file is no longer dependent on a shared checkout's current branch), applied the
same stub guard to it, and updated the live file on `.158` directly. On the laptop, where
`~/skmem-build/skmem_reconcile.py` was a plain copied file (not a symlink) with no guard at
all, repointed it to `skcapstone-repos/skmemory/scripts/skmem_reconcile.py` (PR #37's
already-tested copy), since the laptop has no `~/clawd` repo. This closes the immediate gap
on both hosts but does **not** consolidate the four copies into one; that is recorded here as
unresolved follow-up work, not executed in this pass.

## 4. Why backups were insufficient

### 4.1 skmem-pg nightly dumps actually keep 7 days, not 14

`~/.skcapstone/scripts/skmem-pg-backup.sh` has carried the comment `# ... Keeps 14 days.`
since 2026-07-06, but the actual prune line is:

```bash
ls -1t "$DST"/skmem-pg-skmemory-*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f
```

`tail -n +8` on a newest-first listing keeps the 7 most recent and deletes everything older:
7 days of retention, not 14. This was independently confirmed by the recovery agent's own
phase-2 notes: "The 7 nightly dumps (Oct 10 to Oct 16) ... all postdate whatever earlier event
stubbed these rows." The comment has been wrong for over three months; nobody who read it had
reason to doubt it until this incident forced an actual count.

### 4.2 No long-term/weekly/monthly tier for skmem-pg

Even at the *intended* 14 days, this incident's corruption predates the retention window by
months. There was no GFS (grandfather-father-son) tier at all for skmem-pg, only nightly.
Recovery instead depended on three **undocumented, off-schedule** full dumps that happened to
exist outside the normal backup directory (`~/.skcapstone/agents/lumina/backups/skmem-pg-memories-prereconcile-20260706-122601.dump`,
`~/skmem-backups/skmemory-pre-migrate-20260806-183244.dump`, and
`~/skmem-backups/skmemory-pre-migrate-20260820-19521{5,7}.dump`), artifacts of prior ad hoc
migration work, not a backup policy. Being saved by three accidents is not a backup strategy.

### 4.3 Flat-file tarball backups exist but weren't the recovery path

`~/.skcapstone/agents/lumina/scripts/memory-backup.sh` (cron, daily 03:40) already tars the
flat memory dirs with 7 daily / 4 weekly / 12 monthly retention. This tier was not consulted
during recovery in the evidence reviewed for this document; it is a legitimate independent
recovery path going forward and this work does not change its schedule, only confirms its
retention tiers are reasonable and should be matched by the pg-side backup.

### 4.4 The laptop's off-node snapshot already exists but is shallow

`~/.skcapstone/agents/lumina/scripts/skcapstone-backup.sh`, run nightly, already gives the
laptop a copy of `.158`'s `~/.skcapstone` state, but at 7 days retention, same shortfall as
§4.1.

## 5. Permanent fixes

Each item below is implemented as its own branch/PR in its owning repo, per the estate's
worktree convention (never edit a shared checkout directly). Status and links are filled in
as each lands; see the tracking section at the end of this document for the final state.

1. **Stub/shrink guard on every write path into `skmem-pg`.** `PGVectorBackend.save()`
   (`skmemory/backends/pgvector_backend.py`) and `skmem_reconcile.py` (now
   version-controlled) both refuse to write content carrying the
   `recovered_from_sqlite_preview` marker or the literal `[recovered from SQLite preview`
   text, and refuse to replace strictly longer existing content with shorter content,
   without an explicit `force=True` / `--force-stubs` escape hatch; both log loudly on every
   refusal. TDD, skmemory repo.
2. **Drift detection wired to alerting + `skcapstone doctor`.** `skmemory-sync-audit.sh`
   alerts (does not silently exit 0) when it finds zero active sync timers to audit, and
   discovers every agent with a memory home rather than only agents with an active timer;
   `skcapstone doctor` gains a `memory:drift` check per agent home that calls `skmemory
   health`, treats `sqlite_only`/`flat_only`/`pg_only` > 0 or any stub-flagged content as a
   failing check with an `sk-alert` hook, not just a log line.
3. **Installer installs the sync timer.** `scripts/install.sh` (skcapstone) installs and
   enables `skmemory-sync@<agent>.timer` for every agent home it provisions;
   `skcapstone doctor` fails (category `systemd`) when an agent home exists on a host with no
   matching active timer.
4. **Backup retention: GFS for pg + flat, true off-node copy retention extended.**
   `skmem-pg-backup.sh` moves to 14 daily + 8 weekly + 12 monthly; the laptop's
   `skcapstone-backup.sh` off-node snapshot retention is extended from 7 days to weekly/monthly
   tiers as well.
5. **Per-node pg DSN guard.** skmemory refuses (loud warning by default, hard fail behind an
   opt-in strict flag) to operate against a `SKMEMORY_PG_DSN` whose host does not match the
   local node's own configured pg host, since skmem-pg is per-node by design (prb-6f069c5e).
6. **`skmem_reconcile.py` brought under version control.** Moved into the skmemory repo
   (`scripts/skmem_reconcile.py`), with the live cron-invoked copies on both hosts to be
   replaced by a symlink into the repo checkout, so the script that reconciles the fleet's
   memory store is no longer the one thing on the estate that `git log` cannot see.
7. **Refuse a pytest-time write into the real agent home.** New
   `skmemory/pytest_guard.py`, wired into every flat-file write path, refuses to write under
   the real `~/.skcapstone/agents/` tree while pytest is actively running a test, unless the
   test isolated or explicitly opted in. See §3.9.
8. **`synthesize_daily`/`synthesize_weekly`/`synthesize_dreams` query by date range and
   source, not recency.** `list_memories()` gains `created_after`/`created_before`/`source`
   params, applied in each backend's own query before `limit` truncates the result; all three
   synthesis methods use them instead of fetching the N most recent and filtering afterward.
   See §3.11.
9. **The LIVE `skmem_reconcile.py` (not just skmemory's copy of it) gets the stub guard.**
   Fast-forwarded the file's only version-controlling commit onto its own repo's main, applied
   the §3.5 guard to it directly, repointed the laptop's plain (ungarded) copy to a symlink
   into the already-fixed, already-tested skmemory copy. See §3.12.

See the skmemory repo's `docs/postmortems/` history and the linked PRs for implementation
detail; this document is the narrative record and is not updated after the fixes land except
to fill in the tracking table below.

## 6. Tracking

| # | Fix | Repo | PR | Status | Installed |
|---|---|---|---|---|---|
| 1 | Stub/shrink guard (pg backend) | skmemory | [#36](https://github.com/smilinTux/skmemory/pull/36) | merged | both (editable checkout) |
| 1b | Stub/shrink guard (skmem_reconcile.py) + version control | skmemory | [#37](https://github.com/smilinTux/skmemory/pull/37) | merged | both (see row 9) |
| 2 | Drift detection alert fix (skmemory-sync-audit.sh) | skmemory | [#38](https://github.com/smilinTux/skmemory/pull/38) | merged | both (deployed + laptop cron added) |
| 2b | `skcapstone doctor` memory-drift + sync-timer checks | skcapstone | [#1246](https://github.com/smilinTux/skcapstone/pull/1246) | merged | `.158` (laptop checkout on an unrelated feature branch, not pulled) |
| 3 | Installer wires sync timer; doctor enforces | skcapstone | [#1247](https://github.com/smilinTux/skcapstone/pull/1247) | merged | `.158` only (same laptop caveat as 2b) |
| 4 | Backup GFS retention (pg + flat + off-node) | skmemory | [#45](https://github.com/smilinTux/skmemory/pull/45) | merged | `.158` (deployed to live paths; laptop has no skmem-pg-backup cron to update yet) |
| 5 | Per-node pg DSN guard | skmemory | [#44](https://github.com/smilinTux/skmemory/pull/44) | merged | both (editable checkout; DSNs already per-node correct) |
| 6 | `skmem_reconcile.py` under version control | skmemory | [#37](https://github.com/smilinTux/skmemory/pull/37) | merged | both (see row 9) |
| 7 | Pytest real-memory-write guard | skmemory | [#39](https://github.com/smilinTux/skmemory/pull/39) | merged | both (editable checkout) |
| 8 | `synthesize_*` query by date range + source, not recency | skmemory | [#41](https://github.com/smilinTux/skmemory/pull/41) | merged | both (editable checkout) |
| 9 | Live `skmem_reconcile.py` guard + branch fix | lumina (`~/clawd`) + skmemory | lumina branch pushed straight to its own main (no PR tooling for that host); skmemory via #37 | merged | both: `.158` file updated in place, laptop symlink repointed |

Installed-on-both-hosts notes: `skmemory` and `skcapstone` are editable installs
(`pip install -e`) pointed at each host's own clone of `~/clawd/skcapstone-repos/{skmemory,skcapstone}`,
so merging to `main` plus a `git pull` on each host's clone is the actual "install" step; both
clones were pulled to the commits above during this investigation. `.158`'s skcapstone clone
is on `main`; the laptop's skcapstone clone was left untouched on an unrelated in-progress
feature branch (`feat/profile-defaults-telemetry-agent-skills`) per the shared-checkout rule,
so rows 2b/3 are not yet live on the laptop pending that branch's own resolution. Enabling the
sync timer for existing agents with real memory content (`architect`, `ava`, `grok`,
`lumina-template`, and on the laptop also `jarvis`/`opus`) as part of this deployment
triggered a handful of real, one-time `skmemory sync` runs on non-`lumina` agents; these are
the exact idempotent, non-destructive operation this fix exists to run regularly, and none
touched `lumina`'s data (the agent under separate, concurrent recovery).
