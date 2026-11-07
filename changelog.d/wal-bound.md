### Fixed
- index.db WAL is bounded: journal_size_limit and wal_autocheckpoint on every connection (backend, FTS, intentions, access bump), a throttled PASSIVE checkpoint after index writes, and memory_health reports WAL size with a degraded threshold of 256 MB (SKMEM-P0-21).
