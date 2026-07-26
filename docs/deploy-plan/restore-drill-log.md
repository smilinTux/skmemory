# skmem-pg restore drill log

Record of every skmem-pg cold-restore drill. The drill exercises the full
`pg_dump -Fc` -> `pg_restore` -> functional-verify path in THROWAWAY containers so
it never touches the live `skmem-pg` (localhost:5432) or the live backups.

Ceremony and scripts:
- Backup (now off-box): `deploy/ops/skmem-pg-backup.sh`
- Restore + verify: `scripts/skmem-pg-restore.sh <dump>`
- Safe synthetic drill harness: `scripts/skmem-pg-restore-drill.sh`
- Ceremony doc: `docs/deploy-plan/skmemory-bulletproof-deploy.md` (section "Cold-machine recovery ceremony")

---

## 2026-07-26 - Drill #1 (synthetic dump, automated harness)

- Operator: swarm work-agent (card 9cdf164d)
- Host: local dev box (Docker 29.1.3)
- Image: `skmem-pg:pg17-bm25-age` (already built locally; no image build time in this run)
- Method: `scripts/skmem-pg-restore-drill.sh` - stands up a throwaway SOURCE
  container, loads the vendored `deploy/skmem-pg/schema.sql`, seeds 25 synthetic
  memories + a 1-graph AGE graph (`drilltest_knowledge`, 2 vertices + 1 edge),
  dumps it with `pg_dump -Fc` (exactly like `skmem-pg-backup.sh`), then hands the
  dump to `scripts/skmem-pg-restore.sh` which restores into a SECOND throwaway
  container and verifies.
- Live safety: SOURCE container `skmem-pg-drill-src` (no published port), RESTORE
  container `skmem-pg-drill-restore` on `127.0.0.1:15477`. The live `skmem-pg`
  container and port 5432 were never referenced. Both throwaway containers were
  torn down (`docker rm -fv`) on completion.

### Result: PASS

| Metric | Value |
| --- | --- |
| Synthetic dump size | 304 KB (`pg_dump -Fc`, full schema + data) |
| Restore + verify elapsed | 7 s (image pre-built) |
| `memories` rows restored | 25 |
| `docs` rows restored | 0 (none seeded) |
| `hybrid_search_*` functions present | 2 (`hybrid_search_docs`, `hybrid_search_memories`) |
| `hybrid_search_memories('synthetic', NULL)` | 10 rows returned |
| AGE graphs restored | 1 (`drilltest_knowledge`) |

Notes / findings:
- A FULL `pg_dump -Fc` DOES carry the `ag_catalog.ag_graph` registry rows
  (verified: the dump SQL contains `COPY ag_catalog.ag_graph ... FROM stdin`),
  so a real restore brings the AGE graph back. This is the key result: the
  earlier README caveat ("a schema-only dump does not restore the ag_graph
  registry") does NOT apply to the daily backup, which is a full dump.
- `pg_restore` returns non-zero on an AGE/pg_search dump (benign notices about
  already-present extension objects). The restore script therefore does not use
  `--exit-on-error`; the functional VERIFY block is the source of truth.
- BM25 (ParadeDB) ignores English stopwords, so the verify samples a content
  token of length >= 5 to avoid a false 0-row result on words like "the"/"this".

### Caveats for the LIVE drill (operator follow-up)

This run used a small synthetic dump. Timing on a real restore will be larger
and is dominated by data volume, not the ceremony:
- The live `skmemory` DB carries real memories/docs plus the ~33k-node AGE graph;
  `pg_restore` data load + HNSW/BM25 index rebuilds will take proportionally
  longer than 7 s. Record the real elapsed below when drilled.
- If the image is not yet built on the cold machine, add the one-time image build
  (AGE is compiled from source: several minutes). See the ceremony doc.

To drill against a REAL dump (read-only, still ephemeral, still never touches the
live DB), copy a dump off .158 and run:

```sh
# from the repo root, image already built:
scripts/skmem-pg-restore.sh /path/to/skmem-pg-skmemory-YYYYMMDD-HHMMSS.dump
# -> spins skmem-pg-restore on 127.0.0.1:15432, restores, verifies, tears down.
# Record elapsed + row counts as "Drill #2" below.
```

---

## Template for future drills

## YYYY-MM-DD - Drill #N (<real|synthetic> dump)
- Operator / host / image:
- Dump source + size:
- Restore + verify elapsed:
- memories rows / docs rows:
- hybrid_search functions / hybrid rows:
- AGE graphs:
- Result: PASS/FAIL + notes
