### Added

- **Write-time conflict verdict (SKMEM-P3-02).** `MemoryStore.snapshot(kind=..., reader=..., supersedes_id=...)`.
  For `fact`, `rule` and `preference` the new memory is compared with the strongest of the top-5 similar open
  same-kind memories the reader may see. A dedupe-safe restatement (new content words are a subset of the old
  and no negation word differs) is not written and the existing memory is returned; otherwise a related fact
  is written with an `EXTENDS` edge (`parent_relation: extends`). A record is closed (`supersedes_id`,
  SUPERSEDES edge, old `valid_to` set to the new `valid_from`) only for an explicit `supersedes_id` (always
  honoured after the reader check, following the chain to its open head) or an LLM verdict (skgateway
  `sk-default`, only when `SKMEMORY_TRIAGE_USE_LLM` is set) that returns strict JSON with a verbatim quote
  from each side AND passes a same-subject guard and a declarative-text guard. Rules alone never close
  anything. Stored text reaches the LLM only inside delimited data blocks. Every doubt, failure or
  disagreement resolves to `extends` or a plain write and is logged in `metadata.possible_conflict_with`;
  a triage failure never loses the write. `heal_supersessions` (run from the promotion sweep) closes a record
  left open by a crash between the new write and the close, and adopts a closure found only in a Syncthing
  conflict copy. Conflict copies (`*.sync-conflict-*.json`) are no longer listed or indexed as live records.
  Closing rewrites the old flat file: `integrity_hash` does not cover `valid_to`, so the hash and fortress
  `verify_all` stay valid; the edit is recorded in `metadata.superseded_by` and the WAL. Calls without `kind`
  are unchanged. `SKMEMORY_WRITE_TRIAGE=off` disables triage.
  A sealed record (detached-signature sidecar) that the store cannot re-sign is never closed: the write
  becomes `extends` with `possible_conflict_with`. A record that exists only as a Syncthing conflict copy
  stays visible (and loadable); divergent copies with a canonical file stay hidden; both are counted in
  `drift_check()["conflict_copies"]`. `heal_supersessions` reads `supersedes_id` from the SQLite index and
  will not adopt a conflict copy's closure when the canonical record was edited after it.
