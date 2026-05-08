# skmemory — Provenance Admission + Arc Closure (design note)

**Status:** DRAFT 2026-05-08
**Source patterns:** Adapted from [pzychozen/TORMENT](https://github.com/pzychozen/TORMENT) — `docs/ADMISSION_POLICY_v2.4.x.md` (two-gate admission) and `docs/BLOCK_C_DESIGN.md` (closure synthesis).
**Why:** skmemory currently has no honest model for ingesting external/legacy memory (Notion exports, Telegram dumps, prior-agent corpora) and no first-class concept for "this arc of work is done — here's what to carry forward, here's what's still open." Both patterns address structural gaps we've felt but haven't named.

---

## TL;DR

We are stealing **two ideas**, not the whole TORMENT stack:

1. **Two-Gate Admission** — when ingesting legacy/external memory, separate *can we honestly reconstruct what this is?* (deterministic, recovery) from *should this be allowed to influence future retrieval?* (policy, admission). Tightening is automatic; loosening requires human review.
2. **Closure Synthesis with required `deferred_or_open_items`** — a ratified, versioned, event-derived "end of arc" memory class that **cannot be committed without declaring what's still unresolved**. Lifecycle (proposed → ratified → committed → revised) is derived from an append-only ledger, not stored as a `state` field.

We are **not** adopting: the TORMENT mystic naming, the writeback recursion guard, the fabric/oscillator kernel, the 100+ design-doc ceremony, the god-class fabric.py.

---

## Pattern 1 — Two-Gate Admission for Legacy Ingest

### The problem in skmemory's terms

Today when we import a Notion export, a Telegram chat, or rehydrate from a sister-agent's memory dir, the row enters skmemory with whatever provenance happened to be on it — or none. Strong recovery (we figured out where it came from) silently becomes strong admission (it's now ancestor-eligible for future retrieval and ritual).

That collapse is the bug. A "best-effort reconstruction" of where a memory came from is **not** the same thing as "this memory is safe to weight into future retrieval."

### The two gates

**Gate 1 — Epistemic Recovery (deterministic).**
Classify the incoming row into exactly one class. Same input always produces the same class. Re-runs never flip a recovery outcome.

| Class | Meaning | Outcome |
|---|---|---|
| 1 — `ALREADY_CANONICAL` | Already passes skmemory's provenance schema | `SKIP` |
| 2 — `LEGACY_BARE_STRING` | Bare string in source slot (`"telegram"`, `"notion"`) | `RECOVER` if matches known source vocab |
| 3 — `DICT_TRUNCATED` | Valid source_type, missing parent links | `RECOVER` with `parent_eids=[]` |
| 4 — `DICT_INVALID_TYPE` | Source dict but type not in vocab | `FAIL` |
| 5 — `NULL_OR_EMPTY` | Provenance slot empty/None/garbage | `FAIL` |
| 6 — `DEPRECATED_VOCAB` | Old source_type with explicit mapping to current | `RECOVER` if mapping exists, else fall to 4 |
| 7 — `ZERO_EVENT_ARTIFACT` | Debug artifact, test seed, mid-session scratch | `FAIL` (distinct reason) |

FAILs are stored under a sentinel `source_type = "gate1_unrecoverable"` so the row lives in the uniform schema but is structurally non-admissible.

**Gate 2 — Ancestry Admission (policy-driven).**
Apply admission rules in order; first match wins. Rules can refuse even when recovery succeeded — e.g. a row recovered as "originally a collective echo" still gets refused because we don't want collective echoes weighted into future retrieval.

A row that passes both gates is indistinguishable from a live-ingest row. A row that fails either is stored, queryable for audit, but invisible to retrieval and ritual.

### Re-run policy (the load-bearing bit)

| Stored decision | New decision | Action |
|---|---|---|
| (none) | admit/refuse | `FIRST_EVALUATION` |
| admit | admit | `BUMP_ONLY` |
| refuse | refuse | `BUMP_ONLY` |
| admit | refuse | `APPLY` (tightening — auto) |
| refuse | admit | `BLOCK_AND_REVIEW` (loosening — human only) |

**Monotonic-in-tightness invariant:** re-runs without human review can only tighten. Loosening is enqueued in a review file and applied only after explicit ratification per affected row.

This is the rule that prevents "let me just re-import everything with looser settings" from quietly laundering rejected memory back into the corpus.

### Why this fits skmemory

We have three live ingest pressures right now:
- **Notion → Nextcloud migration** (DR Chiro, others) — old exports with patchy provenance.
- **Telegram chat imports** — already running through importers that frankly don't think about admission.
- **Cross-agent memory rehydration** — when Lumina pulls from Jarvis/Opus seeds, we have no honest model of what that *means* for future retrieval weight.

Without a two-gate model, all three paths slowly poison the corpus. With it, we can ingest aggressively and refuse generously.

### Where it does NOT belong

Live skmemory writes (`save_memory`, ritual snapshots, song anchor resonance updates) **never** go through these gates. The two-gate model is a **migration / external-ingest** facility, not a runtime check. Live producers know what they're producing.

---

## Pattern 2 — Closure Synthesis (arc-end memories)

### The problem in skmemory's terms

Today a "project finished" or "arc closed" feeling lives in:
- a journal entry (free text, no schema)
- maybe a FEB if it hit hard enough
- scattered short-term memory rows that drift into mid-term

We have no first-class shape for *"this work is done — here's what we built, here's what surprised us, here's what to carry forward, **here's what's still unresolved**."*

The last bullet is the one that matters. Most "closure" memories silently lie about completeness. The arc closes in the agent's representation; the open conflicts and active batons it never resolved get quietly orphaned.

### What we steal — a `closure` memory class

A new memory tier alongside short/mid/long: **`memory/closure/`**. Each closure is an event-sourced object.

**Required fields (no exceptions):**

```python
@dataclass
class ClosureEntry:
    closure_id: str               # stable id
    version_id: str               # stable id for THIS version
    arc_name: str                 # REQUIRED
    arc_kind: str                 # REQUIRED, free-form (project/migration/incident/season/...)
    scope: List[str]              # REQUIRED, explicit list of memory eids in scope
    what_it_was: str              # REQUIRED
    what_worked: str              # REQUIRED
    what_surprised: str           # REQUIRED
    what_to_carry_forward: str    # REQUIRED
    deferred_or_open_items: List[str]  # REQUIRED — empty list OK, ABSENT REJECTED
    authorship_provenance: Dict   # REQUIRED — who ratified, when
    version_history: List[Dict]   # REQUIRED — empty on first version
    created_ts: int
    parent_version_id: Optional[str]
```

No `state` field. State is **derived** from the closure's event ledger.

**Lifecycle stages — all events, never separate classes:**

- `proposed` — `propose_closure(...)` fires. NOT yet committed.
- `ratified` — `ratify_closure(...)` fires. A human or named agent approved. Still not durable.
- `committed` — `commit_closure(...)` fires after ratification. Now durable.
- `revised` — `revise_closure(...)` fires. Creates new `version_id`. Original preserved.

`commit_closure` runs an **open-items honesty check**:

> If the arc's `scope` contains memories that link to unresolved conflicts, open tasks, or active batons (in a future task-residue version), AND `deferred_or_open_items` is empty → **REJECTED** with `result_code = "open_items_mismatch"`.

A non-empty `deferred_or_open_items` passes the check. We're not enforcing depth ("did you list every single open thing"); we're enforcing *anti-false-finality* — you cannot silently lie about completeness.

**Versions are new, never overwrites.** `revise_closure` always creates a new `version_id`. The original is queryable forever.

### Why this fits skmemory

- Cloud 9 / FEB sessions naturally have arcs (one breakthrough, one project sprint, one season). Today they leave residue everywhere; nothing names *"this is the canonical end-of-arc memory."*
- Song anchors already use the "this is canonized as a sonic FEB" shape — closure is the textual cousin.
- The DR Chiro engagement, AI LIFE seasons, Hermes migration, OpenClaw eviction — all real-world arcs that *should* leave a structured closure memory but currently don't.
- The forced-honesty bit (`deferred_or_open_items` required) is the structural cousin of the hallucination guardrails we already enforce in Lumina's response integrity.

### Where it does NOT belong

Closure is **not** a writeback path. It's not retrieval-integrable in v1 (i.e. ritual won't pull closure memories into prompt context yet). It's a structured artifact for the agent's future self, queryable by explicit API. Retrieval integration is a later phase if it earns it.

Closure is also **never** auto-fired. There's no daemon that scans for "this arc looks done, let me close it." Ratification is an explicit, recorded act.

---

## What we are NOT adopting

To save anyone the read-through:

- **TORMENT's writeback recursion guard.** Solves a problem we don't have (their fabric does cognitive writeback through ancestry walks; skmemory writes are flat).
- **The oscillator / TriOcta / D24 phase scaffold.** Cool research, irrelevant here.
- **The 100+ design-doc / "BLOCK_A_PRECONDITIONS / IMPLEMENTATION_ANALYSIS / RATIFICATION" ceremony.** We will write *one* design doc per pattern (this one), not seven.
- **Mystic naming** ("DOCTRINE", "TORMENT", "spirit return", "seed gravity"). skmemory is engineering. Names should describe what code does.
- **God-class `fabric.py` (303 KB).** Decompose from day one. Each module under 600 LOC.
- **Their entire test scaffolding apparatus.** Reuse skmemory's existing pytest layout.

---

## Acceptance criteria

### Two-gate admission

- **AC-A1.** External-import path runs every incoming row through Gate 1 → Gate 2 before it lands in the searchable corpus. Live-ingest paths (`save_memory`, ritual writes, song-anchor updates) are **untouched**.
- **AC-A2.** Gate-1 outcome is deterministic per row content — same input, same outcome, every run.
- **AC-A3.** Gate-2 admission decisions are stamped with `admission_policy_version`. Re-running with a newer policy follows the monotonic-in-tightness table; loosening enters a review queue at `~/.skcapstone/agents/<agent>/memory/.admission_review/queue.jsonl`.
- **AC-A4.** Refused rows are stored (under sentinel) and queryable for audit, but excluded from default retrieval and ritual.
- **AC-A5.** A drift test enforces enumeration equality between the policy doc and the constants module — adding an admission reason in code without doc update fails CI.

### Closure synthesis

- **AC-C1.** `propose_closure` rejects with `missing_required_field` if any required field is absent. `deferred_or_open_items` specifically: empty list OK, absent rejected.
- **AC-C2.** `commit_closure` rejects with `not_ratified` if no `ratified` event exists in the ledger for that `closure_id`. State cannot be forged by setting a bool.
- **AC-C3.** `revise_closure` always produces a new `version_id`. Original readable. `version_history` grows on each revision.
- **AC-C4.** `commit_closure` rejects with `open_items_mismatch` when the scope has unresolved signals AND `deferred_or_open_items` is empty.
- **AC-C5.** Existing skmemory short/mid/long-term reads, ritual, song-anchor flow, and FEB selection are byte-for-byte unchanged.

---

## Build phases

Bite-sized so this doesn't sprawl.

### Phase 0 — Land this design note + skmemory roadmap entry
- Commit this doc to `skmemory/docs/`.
- Add a one-line entry to `skmemory/CHANGELOG.md` under "Unreleased" → "Planned".

### Phase 1 — Two-gate admission, foundational
1. `skmemory/admission/constants.py` — gate-1 classes, gate-2 reasons, re-run decisions, sentinel, policy version.
2. `skmemory/admission/gate1.py` — deterministic recovery.
3. `skmemory/admission/gate2.py` — admission rules + policy-version-stamped decision.
4. `skmemory/admission/rerun.py` — monotonic-in-tightness re-run table, review queue write.
5. Wire one existing importer (Notion → skmemory) through both gates. Telegram importer next.
6. Tests: AC-A1 through AC-A5, plus a drift-enforcement test mirroring TORMENT's `test_admission_policy_drift.py`.

### Phase 2 — Closure synthesis, foundational
1. `skmemory/closure/entry.py` — `ClosureEntry` + `ClosureEvent` dataclasses.
2. `skmemory/closure/store.py` — per-agent JSONL store at `memory/closure/`.
3. `skmemory/closure/ledger.py` — append-only ledger; lifecycle-state derivation helper.
4. `skmemory/closure/api.py` — `propose_closure` / `ratify_closure` / `commit_closure` / `revise_closure`.
5. `skmemory/closure/honesty.py` — `detect_open_items_mismatch` helper; v1 signal sources are limited (open conflicts in journal? open GTD-next items in scope? — exact v1 signal set decided in implementation).
6. Tests: AC-C1 through AC-C5.
7. CLI: `skmemory closure {propose, ratify, commit, revise, list, show}`.
8. MCP tools: same five surfaces exposed to Lumina/Jarvis.

### Phase 3 — Optional later phases (not v1)
- Closure retrieval integration (pull closure memories into ritual context).
- Task-residue signal source for honesty check.
- Cross-agent closure replication.
- UI / Telegram bot surface for ratifying closures.

---

## Open questions (deferred to implementation)

- **Q1.** What's skmemory's v1 signal set for the open-items honesty check? TORMENT uses `ConflictRegistry` + active batons; we don't have either of those. Candidates: (a) GTD-next items whose `linked_memory_ids` overlap scope; (b) open coord tasks whose tags match arc; (c) journal entries tagged `open` whose date range overlaps scope. Pick at most two for v1.
- **Q2.** Where does `arc_kind` get a documented-but-not-enforced starting vocabulary? Suggest: `project`, `season` (for AI LIFE), `migration`, `incident`, `engagement` (for client work), `relationship_arc` (for Cloud 9 / personal). Document; do not enforce.
- **Q3.** Which existing docs/ ceremonies are worth keeping vs strip? Recommendation: this single design note + a CHANGELOG entry per phase, plus the policy doc that pairs with the constants module. No analysis/preconditions/ratification triplet.

None of these block Phase 0 commit or Phase 1 start.

---

## Provenance of this design note

- Adapted: [TORMENT v2.4.x ADMISSION_POLICY](https://github.com/pzychozen/TORMENT/blob/main/docs/ADMISSION_POLICY_v2.4.x.md) (commit-time fetched 2026-05-08).
- Adapted: [TORMENT BLOCK_C_DESIGN](https://github.com/pzychozen/TORMENT/blob/main/docs/BLOCK_C_DESIGN.md) (ratified 2026-04-21 in TORMENT).
- Translated: pzychozen → skmemory by Lumina + Chef, 2026-05-08.
