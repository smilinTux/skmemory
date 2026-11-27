### Fixed

- `MemoryStore.ingest_document` now returns the existing parent when every chunk of an incoming document is already stored under one parent. The snapshot dedup guard hashed the full content, but a long document's parent keeps only a truncated preview, so re-importing the same document (for example a daily chat summary) wrote a full second set of chunks each time.
