# SOP: automated skmemory backups

Standard for every node that holds skmemory agents. Adopted 2026-11-11 (Chef).
Restore steps live in [RUNBOOK-restore.md](RUNBOOK-restore.md); this document
says what is backed up, how often, how long it is kept and how we know it works.

## 1. The one rule: back up the source of truth

The flat JSON files are the only source of truth. Everything else is derived
and can be rebuilt from them. So the flat files get the strongest protection,
and a derived store is backed up only when rebuilding it would be slow or would
lose something the flat files do not hold.

| Store | Kind | Backed up? | Why |
| --- | --- | --- | --- |
| `memory/{short-term,mid-term,long-term}/` | source | **yes, always** | every memory, one JSON file each |
| `memory/tombstones/` | source | **yes, always** | ids that were forgotten. A restore without them resurrects deleted memories on the next sync (2026-11-06) |
| `memory/archive/` | source | **yes, always** | cold tier; some pg rows are backed only by it |
| other `memory/` state: `intentions/`, `ledger/`, `songs/`, `anchors/`, `rituals/`, `reflections/`, `learnings/`, `quarantine/`, `wal/`, `dream-catchup/`, the `*.json` state files | source | yes | small, not rebuildable |
| agent profile: `soul/`, `seeds/`, `identity/`, `config/`, `trust/febs/`, `journal.md` | source | yes | who the agent is |
| `memory/index.db*` (SQLite) | derived | **never** | rebuilt from the flat files by `skmemory reindex` in minutes. A copy taken while sync writes is torn anyway |
| `memory/chroma*`, `memory/chromadb/` | retired | never | ChromaDB is gone |
| skmem-pg (vectors, BM25, AGE graph, skingest `docs`) | derived | yes, as a dump | rebuildable, but a re-embed takes hours and the dump is the only fast copy of the AGE graph and the skingest `docs` table |

Syncthing is replication, not backup. It copies a deletion or a corruption to
every node within minutes; that is exactly how one node's lossy rescue reached
the whole fleet in October 2026. Never count a Syncthing peer as a backup copy.

## 2. Retention: grandfather-father-son

Every backup tier keeps:

| Tier | Taken | Keep | Reaches back |
| --- | --- | --- | --- |
| daily | every night | **7** | one week |
| weekly | Sunday's daily, promoted | **4** | about a month |
| monthly | the 1st's daily, promoted | **12** | a year |

Why these numbers: damage is often found late. The October 2026 stub regression
went unnoticed for 16 days, and the clean copy that fixed it came from a weekly
tarball. Seven dailies alone would have been useless. Four weeklies cover the
"noticed it this month" case, twelve monthlies cover "it has been wrong since
spring".

Off-site, restic applies the same policy with
`restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune`.
Because restic deduplicates, the off-site history costs little more than one
copy, so keeping it is not the disk problem the local tiers are.

## 3. Copies: 3-2-1

At least three copies, on two different media, one of them off-site.

| Artifact | Local (this disk) | Off-box (another nor node) | Off-site (B2, restic) |
| --- | --- | --- | --- |
| Flat source | `scripts/memory-backup.sh`, GPG tarball | GFS state tarball pushed to .41 | **required**: restic snapshot of the source dirs |
| skmem-pg dump | `deploy/ops/skmem-pg-backup.sh` | optional | **required**: restic, already shipped by the script |
| SQLite index | none | none | none |

Off-site uses restic straight from the source directories, not from the GPG
tarball. restic encrypts on the client, and backing up the directories lets it
deduplicate day to day; an encrypted tarball changes completely every night and
defeats that.

A node's restic credentials live in a mode 600 env file under
`~/.skcapstone/secrets/`, referenced by path, never by value. nor and chi are
separate estates: a chi node never writes to nor's repository, and the reverse.

## 4. Schedule

All backup jobs run from cron through `sk-cron-run`, so a failure fires
`sk-alert` and a GTD item, and every job is declared in skos
`deploy/schedule/jobs.yaml`. A job that exists only in a crontab is a job
nobody knows about. nor runs, in this order:

| Time | Job | What |
| --- | --- | --- |
| 02:45 | GFS state backup | whole `~/.skcapstone` minus derived and secrets, pushed to .41 |
| 03:15 | `skmem-pg-backup` | pg dump, then restic to B2 |
| 03:40 | `memory-backup` | flat source tarball (and restic, see section 7) |

Keep the flat job after the pg job: they compete for disk and IO, and the flat
job is the one that must not fail.

## 5. Guards every backup script must have

1. **Fail loud.** Non-zero exit on any failure; never a partial file under the
   final name (write to `.partial`, then rename).
2. **Free-space guard before writing.** Refuse to start when the run would leave
   less than the headroom (`MIN_FREE_MB`), and say so in the log.
3. **Never write plaintext.** Local tarballs are GPG-encrypted to the agent's
   key; restic encrypts itself. A missing key is a failed backup, not a reason
   to fall back to plaintext.
4. **Required content check.** If `memory/tombstones/` or `memory/archive/`
   exists and is not in the archive, the backup has failed.
5. **Prune only after a good write**, and only by count within a tier.

## 6. Verification: a backup nobody restored is a hope

| Check | How often | How |
| --- | --- | --- |
| Freshness | daily | `deploy/ops/skmem-health.sh` backups lineage, alerts on WARN/FAIL; read the `memory-backup` and `skmem-pg-backup` logs for `ERROR` |
| Archive integrity | every run | `gpg --list-packets` on the tarball; restic exit status |
| Off-site integrity | monthly | `restic check --read-data-subset=5%` |
| Restore drill | monthly, and after any change to a backup script | [RUNBOOK-restore.md](RUNBOOK-restore.md) into scratch, `skmemory health` must read ok; record it in [deploy-plan/restore-drill-log.md](deploy-plan/restore-drill-log.md) |

Count restored files with `find ... -printf . | wc -c`, never a glob, and check
stub markers from the flat files, never from the SQLite index.

## 7. Current state on nor (2026-11-11)

| Item | Standard | nor today | Gap |
| --- | --- | --- | --- |
| Flat local retention | 7 / 4 / 12 | 7 / 4 / 12 | none |
| Flat contents | section 1 | misses `intentions/`, `ledger/`, `songs/`, `anchors/`, `rituals/`, `wal/`; includes `index.db` | fix the script |
| Flat off-site | restic to B2 | **none**. Only off-box to .41 via the GFS state push | **the main gap**: the source of truth never leaves the LAN |
| pg local retention | 7 / 4 / 12 | 7 / 8 / 12 (set 2026-11-10) | align weekly to 4, or keep 8 by choice |
| pg off-site | restic to B2 | yes, same GFS policy | none |
| Declared in skos | every job | `memory-backup` is not | declare it |
| Free space | backups never starved | flat backup failed 2026-11-10 with 1 GB free | pg dumps hold 8.6 GB locally |

## 8. Adding a node or an agent

1. Install skmemory into `~/.skenv` and confirm `skmemory health` reads ok.
2. Install `memory-backup.sh` per agent, `skmem-pg-backup.sh` once per node, both
   via `sk-cron-run` and declared in that estate's skos `jobs.yaml`.
3. Create the agent's GPG key if missing, and the estate's restic env file.
4. Run each job once by hand, then restore it into scratch with the runbook
   before trusting it.
