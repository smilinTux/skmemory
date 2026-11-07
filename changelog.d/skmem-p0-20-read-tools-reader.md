### Fixed

- **Remaining read tools honour the reader ceiling (SKMEM-P0-20).** `memory_recall` (a hidden id
  is not-found, same as a missing one), `memory_list`, `memory_context`, `memory_check_duplicate`,
  `memory_health` (verdict flags only) and `memory_graph` (never expands or counts through a
  private node, filters related-claim support) apply `SKMEMORY_READER` and a per-call `reader`.
  `memory_export`, `memory_verify`, `memory_audit`, `memory_synthesize_*`, the `show-context` and
  `search-deep` CLI commands and a channel-less ritual are owner-only. `memory_promote` and
  `memory_forget` report a hidden id as not-found. Undeclared local owner sessions are unchanged.
- **Unknown or memberless ritual channels read as `@public`** (was `@chef-only`, the highest
  ceiling, which filtered nothing).
