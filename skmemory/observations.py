"""Observations: evidence-grounded, consolidated beliefs derived from memories.

This is skmemory's take on what Hindsight (vectorize-io) calls "observations":
instead of treating every memory as an isolated record, an :class:`Observation`
is a single-sentence belief backed by a list of :class:`ObservationEvidence`
items, each one a citation into a specific memory. The design goal is the
opposite of an LLM summary: every claim has to point at exact, checkable
quotes, and the module never invents, infers, or calls out to a model to
decide whether a belief is true.

Three properties are deliberately NOT stored on disk, only computed:

``proof_count``
    How many pieces of evidence back the statement. Trivial, but exposed as
    a property so callers never have to remember ``len(obs.evidence)``.

``trend``
    Whether the belief is being reinforced, going quiet, or just getting
    started. Computed purely from the *distribution of evidence timestamps*
    (see :func:`compute_trend`) -- no LLM, no heuristic text analysis. This
    is deliberately NOT persisted in the JSON file: trend is a function of
    "now" versus the evidence, so freezing it at write time would make an
    Observation go stale the moment the clock moves, even with zero new
    evidence. Recomputing it on read keeps the stored JSON itself fully
    deterministic (see "Determinism" below) while trend still reflects the
    real world.

``time_span``
    The (earliest, latest) evidence timestamp, or ``None`` when there is no
    evidence yet.

Verification is not optional
-----------------------------
An observation's whole value proposition is "every claim is backed by an
exact quote you can go check". A fabricated citation -- a quote that isn't
actually in the memory it claims to be from -- is strictly worse than having
no observation at all, because it *looks* grounded while lying. This module
never trusts a quote just because an ``Observation`` carries one:
:func:`validate_observation` / :func:`find_unverifiable_evidence` re-check
every quote against the actual memory content before anyone relies on it.

Refine, don't overwrite
------------------------
New evidence for an existing statement should strengthen that belief, not
replace it. :func:`merge_evidence` and :func:`upsert_observations` both dedupe
on ``(memory_id, quote)`` so that applying the exact same evidence twice never
doubles ``proof_count`` -- re-running the same ingestion job is always safe.

Determinism
------------
Given the same ``Observation`` data, :func:`to_json` always produces the
same bytes: evidence is sorted by ``(timestamp, memory_id, quote)`` before
serialization, dict keys are sorted, and timestamps are rendered as UTC ISO
8601 strings. No randomness, no wall-clock dependence, no LLM, no network.

Persistence
------------
Observations live as JSON files under the agent's memory home, in a new
``memory/observations/`` subdirectory, written through
:mod:`skmemory.atomic_io` (temp file + fsync + atomic rename) exactly like
every other flat-file write in this repo. See
``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` section 3.1 for why a
plain ``Path.write_text`` is not an acceptable substitute here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .agents import get_agent_paths
from .atomic_io import atomic_write_text
from .invalid_records import require_memory_id
from .models import Memory
from .pytest_guard import assert_write_allowed

# =============================================================================
# TREND CLASSIFICATION
# =============================================================================

#: The five trends a belief can have. Any other string returned by
#: :func:`compute_trend` is a bug.
TREND_VALUES: frozenset[str] = frozenset({"stable", "strengthening", "weakening", "new", "stale"})

#: Default lookback window used to split evidence into "recent" and "prior"
#: buckets. Two weeks: long enough that a single session's worth of evidence
#: doesn't masquerade as a trend, short enough that a belief which has gone
#: quiet for a month reliably reads as ``stale`` rather than ``stable``.
DEFAULT_TREND_WINDOW: timedelta = timedelta(days=14)

#: How much higher (or lower) the recent evidence rate has to be than the
#: prior rate before the trend tips from ``stable`` into ``strengthening`` /
#: ``weakening``. 1.5x: a trend that is only marginally busier or quieter
#: than its own history is noise, not a trend.
TREND_STRENGTHEN_RATIO: float = 1.5


def _require_aware_utc(dt: datetime, *, label: str) -> datetime:
    """Return *dt* as a tz-aware UTC datetime, or raise if it is naive."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{label} must be a tz-aware datetime, got a naive one: {dt!r}")
    return dt.astimezone(timezone.utc)


def compute_trend(
    timestamps: Sequence[datetime],
    *,
    now: datetime | None = None,
    window: timedelta = DEFAULT_TREND_WINDOW,
    strengthen_ratio: float = TREND_STRENGTHEN_RATIO,
) -> str:
    """Classify a belief's trend purely from the distribution of *timestamps*.

    Pure arithmetic, no LLM: every timestamp is bucketed as "recent" (within
    ``window`` of ``now``) or "prior" (everything older), then the recent
    bucket's rate (count / ``window`` length) is compared against the prior
    bucket's rate (count / the span from the earliest prior timestamp up to
    the recent cutoff).

    Rules, applied in order:

    1. No timestamps at all -> ``"new"``. There is nothing to show a trend
       over yet; a belief freshly stated with zero evidence is as new as it
       gets.
    2. Nothing in the recent window, but some history before it ->
       ``"stale"``. The belief had evidence once but hasn't been reinforced
       in at least one full ``window``; it is going cold.
    3. Everything is inside the recent window, nothing older -> ``"new"``.
       There is no prior baseline to compare against, so however many
       pieces of evidence just arrived, this is a belief still forming.
    4. Otherwise both buckets are non-empty: compare rates.
       - recent rate >= prior rate * ``strengthen_ratio`` -> ``"strengthening"``
       - recent rate <= prior rate / ``strengthen_ratio`` -> ``"weakening"``
       - otherwise -> ``"stable"``

    Args:
        timestamps: Evidence timestamps. Each must be tz-aware; naive
            datetimes raise ``ValueError`` rather than being silently
            assumed to be UTC.
        now: The reference "now". Defaults to the current UTC time. Pass an
            explicit value for deterministic/reproducible classification
            (this is what the eval harness and the test suite do).
        window: Length of the "recent" bucket, and the denominator used for
            the recent rate. See :data:`DEFAULT_TREND_WINDOW`.
        strengthen_ratio: How large the rate ratio must be before the trend
            tips out of ``"stable"``. See :data:`TREND_STRENGTHEN_RATIO`.

    Returns:
        One of :data:`TREND_VALUES`.
    """
    if not timestamps:
        return "new"
    if window.total_seconds() <= 0:
        raise ValueError("window must be a positive timedelta")
    if strengthen_ratio <= 1.0:
        raise ValueError("strengthen_ratio must be > 1.0")

    now_utc = (
        _require_aware_utc(now, label="now") if now is not None else datetime.now(timezone.utc)
    )
    aware = [_require_aware_utc(ts, label="timestamp") for ts in timestamps]
    cutoff = now_utc - window

    recent = [ts for ts in aware if ts >= cutoff]
    prior = [ts for ts in aware if ts < cutoff]

    if not recent:
        return "stale"
    if not prior:
        return "new"

    window_days = window.total_seconds() / 86400.0
    recent_rate = len(recent) / window_days

    earliest_prior = min(prior)
    prior_span_days = max(1.0, (cutoff - earliest_prior).total_seconds() / 86400.0)
    prior_rate = len(prior) / prior_span_days

    if prior_rate == 0.0:
        # Can't happen (len(prior) > 0 implies prior_rate > 0), but guard
        # against a future refactor introducing a zero-division.
        return "strengthening" if recent_rate > 0 else "stable"

    if recent_rate >= prior_rate * strengthen_ratio:
        return "strengthening"
    if recent_rate <= prior_rate / strengthen_ratio:
        return "weakening"
    return "stable"


# =============================================================================
# MODELS
# =============================================================================


class ObservationEvidence(BaseModel):
    """One citation grounding an :class:`Observation` in a specific memory.

    ``quote`` is trusted by nothing in this module until it is run through
    :func:`verify_evidence` / :func:`validate_observation` against the real
    memory content -- carrying a quote here is a claim, not a fact.
    """

    memory_id: str
    quote: str
    timestamp: datetime
    relevance: str | None = Field(
        default=None,
        description="Optional free-text note on why this quote supports the statement.",
    )

    @field_validator("memory_id")
    @classmethod
    def _memory_id_not_empty(cls, v: str) -> str:
        return require_memory_id(v)

    @field_validator("quote")
    @classmethod
    def _quote_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("ObservationEvidence.quote cannot be empty")
        return v

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_aware_utc(cls, v: datetime) -> datetime:
        return _require_aware_utc(v, label="ObservationEvidence.timestamp")

    def dedupe_key(self) -> tuple[str, str]:
        """The key evidence is deduped on: same memory, same quote."""
        return (self.memory_id, self.quote)


class Observation(BaseModel):
    """A single-sentence, evidence-backed belief.

    ``evidence`` is not required to be pre-sorted or pre-deduped by callers;
    :func:`merge_evidence` and the persistence helpers normalize ordering.
    The raw field is left as a plain list so that constructing one directly
    (as the tests do) stays simple.
    """

    statement: str
    evidence: list[ObservationEvidence] = Field(default_factory=list)

    @field_validator("statement")
    @classmethod
    def _statement_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Observation.statement cannot be empty")
        return v.strip()

    @property
    def proof_count(self) -> int:
        """How many pieces of evidence back this statement."""
        return len(self.evidence)

    @property
    def time_span(self) -> tuple[datetime, datetime] | None:
        """(earliest, latest) evidence timestamp, or ``None`` if there is none."""
        if not self.evidence:
            return None
        timestamps = [e.timestamp for e in self.evidence]
        return (min(timestamps), max(timestamps))

    @property
    def trend(self) -> str:
        """The belief's trend, computed fresh from its evidence timestamps.

        See :func:`compute_trend` for the algorithm. This property always
        evaluates against the real wall-clock "now", which is the right
        behaviour in production: a belief with no new evidence genuinely
        does go stale as real time passes, even though nothing about the
        stored object changed. It is NOT persisted (see
        :func:`observation_to_payload`) for exactly that reason -- a frozen
        copy would describe the moment it was written, not the present.

        For deterministic, reproducible classification against a fixed
        reference time (what the test suite and the eval harness both do),
        call :func:`compute_trend` directly with an explicit ``now``
        instead of going through this property.
        """
        return compute_trend([e.timestamp for e in self.evidence])

    def sorted_evidence(self) -> list[ObservationEvidence]:
        """Evidence in the stable, deterministic order used for serialization."""
        return sorted(
            self.evidence,
            key=lambda e: (e.timestamp, e.memory_id, e.quote),
        )


# =============================================================================
# MERGE -- refine, don't overwrite
# =============================================================================


def merge_evidence(
    observation: Observation, new_evidence: Iterable[ObservationEvidence]
) -> Observation:
    """Return a NEW Observation with ``new_evidence`` merged into ``observation``.

    Dedupes on ``(memory_id, quote)``: evidence already present is left as
    is (first write wins), so a piece of evidence can never be counted
    twice. This makes the merge idempotent by construction -- calling it
    twice with the same ``new_evidence`` produces the exact same result as
    calling it once, because the second call's items all collide with keys
    already in the combined map.

    Args:
        observation: The existing observation (not mutated).
        new_evidence: Evidence to merge in.

    Returns:
        A new :class:`Observation` with the same statement and the merged,
        deduped, deterministically-ordered evidence list.
    """
    combined: dict[tuple[str, str], ObservationEvidence] = {}
    for ev in observation.evidence:
        combined.setdefault(ev.dedupe_key(), ev)
    for ev in new_evidence:
        combined.setdefault(ev.dedupe_key(), ev)

    merged = sorted(combined.values(), key=lambda e: (e.timestamp, e.memory_id, e.quote))
    return Observation(statement=observation.statement, evidence=merged)


def upsert_observations(
    existing: Sequence[Observation], updates: Sequence[Observation]
) -> list[Observation]:
    """Merge ``updates`` into ``existing`` by statement, refining rather than overwriting.

    An update whose statement already exists in ``existing`` extends that
    observation's evidence (via :func:`merge_evidence`, so it is also
    idempotent). An update with a new statement is added as a new
    observation. The result is sorted by statement for deterministic
    output.

    Args:
        existing: The current set of observations.
        updates: New observations (freshly built, or partial re-derivations)
            to fold in.

    Returns:
        The merged list, sorted by statement.
    """
    by_statement: dict[str, Observation] = {o.statement: o for o in existing}
    for obs in updates:
        if obs.statement in by_statement:
            by_statement[obs.statement] = merge_evidence(by_statement[obs.statement], obs.evidence)
        else:
            by_statement[obs.statement] = obs
    return [by_statement[key] for key in sorted(by_statement)]


# =============================================================================
# VERIFICATION -- never trust a quote
# =============================================================================

#: Something that can answer "what is the content of memory X": either a
#: plain mapping, or a callable (e.g. a bound ``MemoryStore.recall``-derived
#: lookup from :func:`content_lookup_from_store`).
ContentLookup = Mapping[str, str] | Callable[[str], "str | None"]


def _lookup_content(memory_id: str, content_lookup: ContentLookup) -> str | None:
    if callable(content_lookup):
        return content_lookup(memory_id)
    return content_lookup.get(memory_id)


def verify_evidence(evidence: ObservationEvidence, content_lookup: ContentLookup) -> bool:
    """True if ``evidence.quote`` is an exact substring of its cited memory's content.

    False if the memory cannot be found at all -- a citation to a memory
    that does not exist is exactly as unverifiable as a fabricated quote.

    Args:
        evidence: The evidence item to check.
        content_lookup: Either ``{memory_id: content}`` or a callable
            ``memory_id -> content | None``.
    """
    content = _lookup_content(evidence.memory_id, content_lookup)
    if content is None:
        return False
    return evidence.quote in content


def find_unverifiable_evidence(
    observation: Observation, content_lookup: ContentLookup
) -> list[ObservationEvidence]:
    """Return every evidence item on ``observation`` that fails verification.

    An empty list means every quote checked out. This is the guard against
    the failure mode this whole module exists to prevent: a belief whose
    citations cannot be confirmed is worse than no belief, so this function
    makes that condition checkable rather than assumed.
    """
    return [ev for ev in observation.evidence if not verify_evidence(ev, content_lookup)]


def validate_observation(observation: Observation, content_lookup: ContentLookup) -> bool:
    """True only if every piece of evidence on ``observation`` verifies.

    Convenience wrapper over :func:`find_unverifiable_evidence` for the
    common "is this whole observation trustworthy" check.
    """
    return not find_unverifiable_evidence(observation, content_lookup)


def content_lookup_from_store(store: Any) -> Callable[[str], str | None]:
    """Adapt a ``MemoryStore``-like object (anything with ``.recall(id)``)
    into the callable form of :data:`ContentLookup`.

    Args:
        store: An object exposing ``recall(memory_id: str) -> Memory | None``.

    Returns:
        A callable suitable for :func:`verify_evidence` / `validate_observation`.
    """

    def _lookup(memory_id: str) -> str | None:
        memory = store.recall(memory_id)
        return memory.content if memory is not None else None

    return _lookup


# =============================================================================
# BUILDING OBSERVATIONS FROM MEMORIES -- deterministic, no NLP
# =============================================================================


def _memory_timestamp(memory: Memory) -> datetime:
    dt = datetime.fromisoformat(memory.created_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _memory_group_keys(memory: Memory) -> set[str]:
    """Shared-grouping keys for one memory: its tags plus any extracted entities.

    Casefolded so "Chiap08" and "chiap08" land in the same group. Deliberately
    simple (no synonym table, no stemming) -- the point of this builder is
    determinism, not cleverness.
    """
    keys: set[str] = {tag.casefold() for tag in memory.tags if tag}
    decomposition = (
        memory.metadata.get("decomposition", {}) if isinstance(memory.metadata, dict) else {}
    )
    if isinstance(decomposition, dict):
        for entity in decomposition.get("entities", []) or []:
            if entity:
                keys.add(str(entity).casefold())
    return keys


def build_observations(
    memories: Sequence[Memory],
    *,
    min_group_size: int = 2,
) -> list[Observation]:
    """Group memories by shared tag/entity and build one Observation per group.

    Deterministic and intentionally dumb: no language model decides what
    the memories "mean". A group is a tag or extracted entity string shared
    by at least ``min_group_size`` memories; the statement names the shared
    key and how many memories mention it; each member memory becomes one
    piece of evidence, quoting its own full content (which is trivially a
    substring of itself, so it always verifies).

    Args:
        memories: Candidate memories to group.
        min_group_size: Minimum number of memories a shared key needs before
            it is promoted to an Observation. Below this, a "shared" key
            isn't actually shared by enough to be worth a belief.

    Returns:
        One :class:`Observation` per qualifying group, sorted by statement
        for deterministic output.
    """
    if min_group_size < 1:
        raise ValueError("min_group_size must be >= 1")

    groups: dict[str, list[Memory]] = {}
    for memory in memories:
        for key in _memory_group_keys(memory):
            groups.setdefault(key, []).append(memory)

    observations: list[Observation] = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) < min_group_size:
            continue
        members_sorted = sorted(members, key=lambda m: (m.created_at, m.id))
        statement = f"A recurring pattern connects {len(members_sorted)} memories around '{key}'."
        evidence = [
            ObservationEvidence(
                memory_id=member.id,
                quote=member.content,
                timestamp=_memory_timestamp(member),
                relevance=key,
            )
            for member in members_sorted
        ]
        observations.append(Observation(statement=statement, evidence=evidence))

    observations.sort(key=lambda o: o.statement)
    return observations


# =============================================================================
# SERIALIZATION -- byte-identical for byte-identical input
# =============================================================================


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _evidence_to_dict(evidence: ObservationEvidence) -> dict[str, Any]:
    return {
        "memory_id": evidence.memory_id,
        "quote": evidence.quote,
        "timestamp": _iso(evidence.timestamp),
        "relevance": evidence.relevance,
    }


def observation_to_payload(observation: Observation) -> dict[str, Any]:
    """The plain-dict payload persisted for an Observation.

    Only ``statement`` and ``evidence`` are stored -- ``proof_count``,
    ``trend``, and ``time_span`` are all derived and recomputed on read, so
    they never need to agree with a frozen, potentially stale copy on disk.
    """
    return {
        "statement": observation.statement,
        "evidence": [_evidence_to_dict(e) for e in observation.sorted_evidence()],
    }


def to_json(observation: Observation) -> str:
    """Deterministic JSON for *observation*: sorted keys, stable evidence order.

    Given the same statement and evidence set, this returns byte-identical
    output no matter the call time, call order, or original insertion order
    of the evidence list.
    """
    payload = observation_to_payload(observation)
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def observation_from_payload(payload: Mapping[str, Any]) -> Observation:
    """Reconstruct an :class:`Observation` from the dict :func:`observation_to_payload` produces."""
    evidence = [
        ObservationEvidence(
            memory_id=item["memory_id"],
            quote=item["quote"],
            timestamp=datetime.fromisoformat(item["timestamp"]),
            relevance=item.get("relevance"),
        )
        for item in payload.get("evidence", [])
    ]
    return Observation(statement=payload["statement"], evidence=evidence)


def from_json(text: str) -> Observation:
    """Inverse of :func:`to_json`."""
    return observation_from_payload(json.loads(text))


# =============================================================================
# PERSISTENCE -- flat JSON under the agent's memory home, atomic writes only
# =============================================================================


def observations_dir(agent_name: str | None = None) -> Path:
    """``<agent memory home>/observations`` -- the directory observations live in."""
    paths = get_agent_paths(agent_name)
    return paths["base"] / "memory" / "observations"


def observation_filename(statement: str) -> str:
    """A filesystem-safe, deterministic filename for a given statement.

    Hashed rather than slugified from the text: statements are free-form
    sentences and may contain characters that are not safe (or not unique
    enough) as filenames, and two different statements must never collide
    just because they slugify the same way.
    """
    digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()
    return f"{digest[:32]}.json"


def observation_path(statement: str, *, agent_name: str | None = None) -> Path:
    """Full path an Observation with this statement would be persisted at."""
    return observations_dir(agent_name) / observation_filename(statement)


def save_observation(observation: Observation, *, agent_name: str | None = None) -> Path:
    """Durably write *observation* to its JSON file under the agent's memory home.

    Uses :func:`skmemory.atomic_io.atomic_write_text` (temp file + fsync +
    atomic rename) -- never a plain ``Path.write_text`` -- per the
    2026-10-16 SQLite-only-orphans postmortem (section 3.1): a non-durable
    flat write is exactly the failure mode that incident was about, and
    observations are flat-file source-of-truth data like everything else
    under an agent's memory home.

    Args:
        observation: The observation to persist.
        agent_name: Agent to write under; defaults to the active agent.

    Returns:
        The path written to.
    """
    path = observation_path(observation.statement, agent_name=agent_name)
    assert_write_allowed(path)
    atomic_write_text(path, to_json(observation))
    return path


def load_observation(path: Path) -> Observation:
    """Load one Observation from a JSON file previously written by :func:`save_observation`."""
    return from_json(Path(path).read_text(encoding="utf-8"))


def load_all_observations(agent_name: str | None = None) -> list[Observation]:
    """Load every persisted Observation for an agent, sorted by statement.

    Returns an empty list (not an error) when the directory doesn't exist
    yet -- an agent with no observations is a normal, unremarkable state.
    """
    directory = observations_dir(agent_name)
    if not directory.exists():
        return []
    observations = [load_observation(p) for p in sorted(directory.glob("*.json"))]
    observations.sort(key=lambda o: o.statement)
    return observations
