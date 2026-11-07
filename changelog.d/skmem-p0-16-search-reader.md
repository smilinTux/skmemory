### Added

- **Search enforces the reader's audience (SKMEM-P0-16).** `MemoryStore.search(reader=...)`, the
  `memory_search` and `memory_auto_context` MCP tools (`reader` argument) and `skmemory search
  --reader` derive the allowed `context_tag` set from the audience model and push it into every
  retrieval arm. Readers: `@public`, `@community`, `@work-circle`, `@inner-circle`, `@work:<x>`,
  `channel:<id>`; an unparseable or non-string reader is `@public`. No reader declared stays the
  owner, so local owner sessions are unchanged. `SKMEMORY_READER` declares a whole process as a
  non-owner and is a ceiling that a per-call reader can only narrow.
