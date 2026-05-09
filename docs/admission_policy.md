# skmemory — admission policy

**Pairs with:** `skmemory/admission/constants.py`
**Drift test:** `tests/test_admission_policy_drift.py`

This document is the human-readable side of the admission contract. The
constants module is the machine-readable side. A drift test enforces
enumeration equality between the two — adding a class, outcome, or
reason in one place without the other fails CI.

For the full design + acceptance criteria see
[PROVENANCE_AND_CLOSURE_DESIGN.md](./PROVENANCE_AND_CLOSURE_DESIGN.md).

## Policy version

Current: **1.0.0**

Bump on any semantic change to admission rules — new reason, blocked
source, vocab change, recovery class added or split.

## Gate 1 — recovery classes

| Class | Outcome |
|---|---|
| `already_canonical` | `skip` |
| `legacy_bare_string` | `recover` |
| `dict_truncated` | `recover` |
| `dict_invalid_type` | `fail` |
| `null_or_empty` | `fail` |
| `deprecated_vocab` | `recover` |
| `zero_event_artifact` | `fail` |

`fail` rows are stored under `source_type = "gate1_unrecoverable"` and
excluded from default retrieval / ritual.

## Gate 2 — admission reasons

### Admit

| Reason | When |
|---|---|
| `admit_known_source` | Already canonical or legacy-bare-string with known vocab |
| `admit_recovered_dict` | Dict-truncated with parents recovered or empty |
| `admit_deprecated_remapped` | Deprecated vocab mapped to current vocab (policy permitting) |

### Refuse

| Reason | When |
|---|---|
| `refuse_gate1_failed` | Gate 1 returned `fail` (excluding zero-event artifacts) |
| `refuse_zero_event_artifact` | Zero-event artifact (debug / scratch / test seed) |
| `refuse_blocked_source` | Recovered source matches `blocked_source_types` |
| `refuse_collective_echo` | Row tags intersect `collective_echo_tags` |
| `refuse_no_rule_matched` | Defensive fallthrough (also: deprecated remap disabled by policy) |

## Re-run decision table

| Stored | New | Decision |
|---|---|---|
| (none) | admit / refuse | `first_evaluation` |
| admit | admit | `bump_only` |
| refuse | refuse | `bump_only` |
| admit | refuse | `apply` (auto, tightening) |
| refuse | admit | `block_and_review` (human-only, loosening) |

**Monotonic-in-tightness invariant:** re-runs without human review can
only tighten. Loosening is enqueued at
`~/.skcapstone/agents/<agent>/memory/.admission_review/queue.jsonl` and
applied only after explicit ratification per affected row.

## Source vocabularies

### Known sources

`manual`, `session`, `seed`, `import`, `telegram`, `notion`,
`claude-code-hook`, `consolidation`, `journal-synthesis`, `task-pack`,
`shared`.

### Deprecated → current mapping

| Deprecated | Current |
|---|---|
| `tg` | `telegram` |
| `telegram-export` | `telegram` |
| `notion-export` | `notion` |
| `claude-hook` | `claude-code-hook` |
| `claude_code_hook` | `claude-code-hook` |
| `task_pack` | `task-pack` |

### Blocked (always refused)

`collective`, `anonymous-aggregate`.

### Collective-echo tags (always refused)

`collective-echo`, `ambient`, `egregore`.

## Where this does NOT apply

Live skmemory writes (`save_memory`, ritual snapshots, song-anchor
resonance updates) **never** route through admission. Live producers
know what they're producing. Admission exists for migration / external
ingest only.
