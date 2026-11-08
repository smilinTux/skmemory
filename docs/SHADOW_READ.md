# Shadow-read parity (SKMEM-R-11)

Gate for the Rust engine cutover: run both engines on every real search for 7 days on one node, serve Python, compare.

## Switches

| Variable | Meaning |
|---|---|
| `SKMEMORY_ENGINE` | `python` (default) serves the result. With `rust` the shadow is off (Rust already serves). |
| `SKMEMORY_SHADOW=rust` | Also run the Rust engine (`skmem_core`) on every `MemoryStore.search`. Any other value, or unset, is off. |
| `SKMEMORY_SHADOW_TIMEOUT_S` | Seconds before a Rust call that has not returned is abandoned and logged as `error: timeout` (default 5). |

Needs the `skmem_core` wheel (from `smilinTux/skmemory-rs`, maturin) importable by the same interpreter, built with the GIL release (skmemory-rs PR 19, `skmem_core.RELEASES_GIL is True`). A build without it would freeze the served Python search and the watchdog while Rust runs, so it is refused: one row with `error: skmem_core_holds_gil`, then the shadow is off for the process. Without it every search still works and logs `error: skmem_core_missing`.

## Guarantees

- The Python result is always what the caller gets. The Rust call runs on a daemon thread, started before the Python search so it adds no wait; a watchdog bounds it.
- Any Rust error, a missing wheel included, becomes a row with an `error` field and is swallowed. At most 4 shadow searches run at once; extras are logged `error: busy`.
- Read-only: the engine is opened with `read_only=True` and searched with `record_access=False`, so Rust writes no reinforcement, ledger or index row. Reinforcement stays Python's.
- `reader`, `tags`, `layer`, `source`, `context_tag` and `as_of` go to both engines unchanged; both read the reader ceiling from `SKMEMORY_READER`.
- Not shadowed: stores whose primary is not a plain SQLite index, and searches inside `retrieval_log.measurement()` (benchmarks).
- Both engines run at the same time, so each latency includes some contention. The comparison is fair, absolute numbers run a little high.

## The log

`<agent home>/logs/shadow.jsonl` (separate from `retrieval.jsonl` so arm liveness is untouched; same 0600 file, 5 MB rotation, 3 kept). One row per query, NO query text and NO memory content:

```json
{"ts":1.7e9,"q":"<12 hex hash>","qlen":4,"k":5,"py_ids":[...],"rs_ids":[...],
 "py_ms":41.2,"rs_ms":12.9,"py_arms":{"lexical_local":"ok"},"rs_arms":{"lexical_local":"ok"},
 "overlap":1.0}
```

`rs_ms` times the search only; the first query's engine open is in `rs_open_ms`. `overlap` is `|top5(py) & top5(rs)| / max(len)`, 1.0 when both are empty. On failure the row carries `error` (`timeout`, `busy`, `skmem_core_missing`, `skmem_core_holds_gil`, or the exception type name) and `overlap: null`; errors are excluded from the mean and counted separately.

## The 7-day report

```
python -m skmemory.shadow --days 7            # active agent (SKAGENT)
python -m skmemory.shadow --days 7 --json
python -m skmemory.shadow --file /path/to/shadow.jsonl
```

(`skmemory-shadow-report` after a reinstall.) Prints rows, compared, errors, mean top-5 overlap, p50 and p95 latency for each engine, and the divergence count with the query hash and both id lists of every divergent query. Targets from the card: mean overlap >= 0.99, Rust p95 <= Python p95. Each divergence becomes a fix card.
