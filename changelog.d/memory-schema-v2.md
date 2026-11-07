### Added

- **Memory schema v2 (SKMEM-P3-01).** `Memory` gains `kind`, `outcome`, `event_at`, `valid_from`,
  `valid_to`, `supersedes_id`, `source_tier` and `schema_version` (default 2), with `extra="allow"` so
  a node that loads and re-saves a newer file keeps unknown fields. v1 files load unchanged: defaults
  apply in memory only, nothing is rewritten on load, and `integrity_hash` / seals are unaffected
  because the hash covers only id, title, content and the emotional signature. SQLite gets the new
  columns via ALTER on open (tolerating a concurrent opener), the AGE `Memory` node mirrors them, and
  skmem-pg gets nullable typed columns through the new `06-memory-schema-v2.sql` migration (run
  `skmemory pg migrate`; unmigrated nodes keep working because `memory_json` carries every field).

- Stored memories are read leniently: an unknown `kind`, `outcome` or `source_tier` falls back to the
  default and the raw value is kept in `metadata["schema_v2_unknown"]`, instead of the file silently
  disappearing. A v1 file with no `schema_version` reports 1 on load (file untouched). Constructing
  a `Memory` directly stays strict. `promote()` no longer copies `supersedes_id` / `valid_to`.
