### Added

- **Write-time conflict verdict (SKMEM-P3-02).** `MemoryStore.snapshot(kind=..., reader=...)`: for `fact`,
  `rule` and `preference` the new memory is compared with the top-5 similar same-kind memories the reader
  may see. An agreed restatement is not written (the existing memory is returned); a related, still-true
  fact is written with an `EXTENDS` edge (`parent_relation: extends`); a fact that makes the old one false
  is written with `supersedes_id` and a `SUPERSEDES` edge, and the old record's `valid_to` is closed at the
  new `valid_from`. Rules decide first, the LLM (skgateway `sk-default`, only when
  `SKMEMORY_TRIAGE_USE_LLM` is set) only when rules are inconclusive and must answer the same way twice.
  Every doubt, failure or disagreement resolves to `extends` or a plain write, never `changes`; a triage
  failure never loses the write. Closing the old record rewrites its flat file: `integrity_hash` does not
  cover `valid_to`, so the hash stays valid, and the edit is recorded in `metadata.superseded_by` and the
  WAL. Calls without `kind` are unchanged. `SKMEMORY_WRITE_TRIAGE=off` disables it.
