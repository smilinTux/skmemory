"""Standing answers: a registered question with a watermark-staled cached answer.

A standing answer pairs a stable, registered question with a stored answer
that is refreshed in the background. The point is for an agent to boot with
settled knowledge ("what's my current standing with Chef", "what projects are
open") instead of re-deriving the answer from scratch every session by
re-running retrieval. This is modelled on what Hindsight (vectorize-io) calls
"mental models": a durable answer the agent can re-check cheaply, not a cache
that silently rots.

Design point: staleness is keyed on the memory watermark, not the wall clock
---------------------------------------------------------------------------
Every ``StandingAnswer`` carries two very different timestamps:

* ``last_refreshed_at`` -- wall clock. When did a refresh pass last run.
* ``last_memory_seen_at`` -- the **memory watermark**. The newest
  ``created_at`` among the memories the current answer was actually computed
  from.

``is_stale()`` compares the watermark to the newest timestamp *in scope*, not
the wall clock to "now". This is deliberate, not incidental:

A wall-clock design ("fresh if refreshed within the last N hours") cannot
tell the difference between "nothing has happened since the last refresh"
and "a new memory landed one second after the last refresh and I haven't
looked at it yet". Both look identical to a clock. That is the exact shape
of bug this repo has already paid for once: ``docs/postmortems/
2026-10-16-sqlite-only-orphans.md`` section 3.2 documents ``health()``
reporting ``ok: True`` off a successful query instead of off the actual
drift counts, and section 3.3 an audit script exiting 0 on zero targets --
in both cases a wrong answer was indistinguishable from a right one until
someone measured. Keying staleness on wall clock reproduces exactly that
pattern here: a refresh that happened to find nothing still *looks* fresh by
the clock, right up until the moment a genuinely new memory silently stops
being reflected. See ``skmemory/eval/standing_bench.py`` for a harness that
makes this divergence concrete with numbers instead of leaving it as an
assertion.

Because staleness is keyed on the watermark, the watermark itself must never
be advanced past what a refresh actually, honestly saw:

* A **failed** refresh (the injected compute raised) must leave the
  watermark exactly where it was -- ``last_refresh_failed_at`` is recorded,
  nothing else moves.
* A **successful** refresh that legitimately saw nothing new in scope
  (``ComputedAnswer.newest_memory_seen_at is None``) must also leave the
  watermark where it was, rather than jumping it to "now" or to an assumed
  full-scope value it did not actually verify. Advancing it anyway is
  precisely "a refresh that found nothing still looks fresh" wearing a
  different hat: it would hide a memory that lands after this refresh but
  before the next one bothers to check, for exactly as long as nobody
  notices.

Reading is a plain read
------------------------
``StandingAnswerStore.get()`` does one thing: read a JSON file and parse it.
No retrieval, no LLM call, no network. That is the entire value of a
standing answer over re-deriving it live: an agent can boot by reading a
handful of these files and have settled knowledge in hand before it has made
a single retrieval call. ``refresh_if_stale()`` is the only path that calls
out, and only when ``is_stale()`` says so, through the injected
``FreshContextRunner`` seam (``skmemory.fresh_context``) so no retrieval or
LLM client is hardcoded here.

Identity: how the id is derived
--------------------------------
A ``StandingAnswer``'s ``id`` is derived deterministically from its
``question`` text alone (``standing_answer_id()``): a lowercase slug of the
question (for a readable filename under ``standing/``) with an 8-hex-char
suffix taken from ``sha256(question)``. The slug keeps ``ls standing/``
legible; the hash suffix guarantees two questions that differ only in
punctuation or case (and so would slugify to the same string) never collide,
and guarantees the id is stable across process runs with no registry lookup
required -- the same question text always produces the same id, deriving it
needs nothing but the string itself.

Persistence
------------
Each standing answer is one JSON file at
``<agent memory home>/standing/<id>.json``, written through
``skmemory.atomic_io.atomic_write_text`` (temp file + fsync + atomic
rename). Flat-file writes here must go through that helper. A plain
``Path.write_text`` only guarantees the bytes reached the page cache, not
the disk, and that exact gap caused a SEV-1 (see
``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` section 3.1); this
module does not reintroduce it. Serialization is deterministic: sorted keys,
stable indentation, so two writes of an unchanged answer produce byte-
identical files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .agents import get_agent_paths
from .atomic_io import atomic_write_text
from .fresh_context import FreshContextRunner, resolve_runner

logger = logging.getLogger("skmemory.standing")

__all__ = [
    "AnswerComputer",
    "ComputedAnswer",
    "NewestTimestampSource",
    "RefreshOutcome",
    "StandingAnswer",
    "StandingAnswerStore",
    "standing_answer_id",
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 60


def standing_answer_id(question: str) -> str:
    """Deterministic id for a standing answer, derived only from ``question``.

    A lowercase slug (non-alphanumeric runs collapsed to a single hyphen,
    trimmed, capped at 60 characters) with an 8-hex-char suffix from
    ``sha256(question)``. The slug makes ``standing/`` directory listings
    legible; the hash suffix guarantees stability (same question text,
    same bytes in, same id out, forever, with no registry needed) and
    guarantees two questions that collapse to the same slug (differing only
    in case or punctuation) still get distinct ids.

    Args:
        question: The registered question text, exactly as asked.

    Returns:
        str: A filesystem-safe, deterministic id.
    """
    normalized = question.strip()
    slug = _SLUG_RE.sub("-", normalized.lower()).strip("-")
    slug = slug[:_MAX_SLUG_LEN].strip("-") if slug else "q"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _ensure_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC rather than silently misbehaving."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _max_optional(a: datetime | None, b: datetime | None) -> datetime | None:
    """``max()`` that treats ``None`` as "no information", not "negative infinity".

    ``None`` never wins and never erases a known value: only replace the
    existing watermark with something that was actually, honestly observed.
    """
    if a is None:
        return b
    if b is None:
        return a
    return max(_ensure_aware(a), _ensure_aware(b))


@runtime_checkable
class NewestTimestampSource(Protocol):
    """Anything that can cheaply report the newest memory timestamp in scope.

    Deliberately NOT ``MemoryStore`` itself: this module must not depend on
    the store's concrete API. A caller can hand ``is_stale()`` either a
    plain ``datetime`` it already has lying around (the common case: a
    single ``MAX(created_at)``-style lookup, computed once and reused for
    several standing answers) or an object implementing this protocol.
    """

    def newest_memory_timestamp(self) -> datetime | None:  # pragma: no cover - protocol
        ...


def _resolve_newest(
    source: NewestTimestampSource | datetime | None,
) -> datetime | None:
    if source is None or isinstance(source, datetime):
        return source
    if isinstance(source, NewestTimestampSource):
        return source.newest_memory_timestamp()
    raise TypeError(
        "store_or_newest_timestamp must be a datetime, None, or an object "
        "exposing newest_memory_timestamp() -> datetime | None, "
        f"got {type(source)!r}"
    )


class StandingAnswer(BaseModel):
    """A registered question plus its stored answer and refresh bookkeeping.

    Attributes:
        id: Deterministic id derived from ``question`` (see
            :func:`standing_answer_id`). This is the identity; two
            ``StandingAnswer`` instances with the same ``id`` are the same
            standing answer at different points in its history.
        question: The registered question text, stable once registered.
        answer: The stored answer text. Reading this is a plain read.
        last_refreshed_at: Wall clock of the last successful refresh (or of
            registration, if never refreshed). Informational only -- never
            consulted by :meth:`is_stale`.
        last_memory_seen_at: The memory watermark: the newest
            ``created_at`` among memories this answer was genuinely computed
            from. ``None`` means no successful refresh has ever reported
            having seen anything. This, not ``last_refreshed_at``, is what
            staleness is keyed on.
        last_refresh_failed_at: Wall clock of the last *failed* refresh
            attempt, or ``None`` if the most recent attempt (if any)
            succeeded. A failed refresh never touches the watermark.
    """

    id: str
    question: str
    answer: str = ""
    last_refreshed_at: datetime
    last_memory_seen_at: datetime | None = None
    last_refresh_failed_at: datetime | None = None

    def is_stale(self, store_or_newest_timestamp: NewestTimestampSource | datetime | None) -> bool:
        """Cheap staleness check: watermark vs. the newest timestamp in scope.

        No retrieval is run here. ``store_or_newest_timestamp`` is either a
        ``datetime`` the caller already has (ideally from a single
        newest-in-scope lookup, not a re-run of retrieval) or an object
        exposing ``newest_memory_timestamp()``.

        Args:
            store_or_newest_timestamp: The newest memory timestamp in scope,
                or something that can report it, or ``None`` if scope is
                currently empty.

        Returns:
            bool: ``True`` if a memory newer than the watermark exists in
            scope (or no refresh has ever reported seeing anything and
            scope is non-empty). ``False`` if scope is empty, or everything
            in scope is at or before the watermark.
        """
        newest = _resolve_newest(store_or_newest_timestamp)
        if newest is None:
            return False
        if self.last_memory_seen_at is None:
            return True
        return _ensure_aware(newest) > _ensure_aware(self.last_memory_seen_at)


class ComputedAnswer(BaseModel):
    """What an injected :data:`AnswerComputer` hands back from one refresh pass.

    Attributes:
        answer: The newly computed answer text.
        newest_memory_seen_at: The newest ``created_at`` among the memories
            this pass actually, honestly scanned, or ``None`` if the pass
            cannot vouch for having seen anything (e.g. its retrieval
            legitimately returned zero results and it has no other basis
            for a scan boundary). ``None`` here is a conservative signal:
            the stored watermark will not be advanced, so staleness stays
            detectable rather than getting hidden by a refresh that ran but
            did not actually establish a new scan boundary.
    """

    answer: str
    newest_memory_seen_at: datetime | None = None


class RefreshOutcome(BaseModel):
    """Result of a :meth:`StandingAnswerStore.refresh_if_stale` call.

    Attributes:
        refreshed: Whether a refresh actually ran and updated the answer.
        reason: ``"not_stale"`` (no-op), ``"refreshed"`` (success), or
            ``"failed: <message>"`` (the compute/runner raised).
        answer: The current ``StandingAnswer`` -- unchanged on ``not_stale``
            or ``failed`` (aside from ``last_refresh_failed_at`` in the
            failed case), updated on ``refreshed``.
    """

    refreshed: bool
    reason: str
    answer: StandingAnswer


#: A pass that performs one refresh for the standing answer it is given and
#: returns the freshly computed answer. Receives the *current* StandingAnswer
#: (so an incremental compute can see the prior answer/watermark) and must not
#: mutate it. Deliberately not bound to any retrieval or LLM client here --
#: production injects a real one, tests inject a trivial fake.
AnswerComputer = Callable[[StandingAnswer], ComputedAnswer]


def _standing_dir(agent_name: str | None = None) -> Path:
    paths = get_agent_paths(agent_name)
    return paths["base"] / "memory" / "standing"


class StandingAnswerStore:
    """Persists and refreshes standing answers for one agent.

    ``get()`` is a plain read: no retrieval, no LLM, no network.
    ``refresh_if_stale()`` is the only path that calls out, and only when
    ``StandingAnswer.is_stale()`` says so, via the injected
    ``FreshContextRunner`` seam.

    Args:
        agent_name: Agent whose memory home holds the ``standing/``
            directory. ``None`` uses the active agent (see
            ``skmemory.agents.get_agent_paths``).
        runner: Injected fresh-context runner used to execute the compute
            pass during a refresh. Defaults to the dependency-free
            in-process runner (see ``skmemory.fresh_context``), so this
            works offline with zero dependencies unless a real runner is
            wired in.
    """

    def __init__(
        self,
        agent_name: str | None = None,
        runner: FreshContextRunner | None = None,
    ) -> None:
        self._dir = _standing_dir(agent_name)
        self._runner = resolve_runner(runner)

    @property
    def directory(self) -> Path:
        """Directory holding this agent's standing-answer JSON files."""
        return self._dir

    def _path(self, answer_id: str) -> Path:
        return self._dir / f"{answer_id}.json"

    def _load(self, answer_id: str) -> StandingAnswer | None:
        path = self._path(answer_id)
        if not path.exists():
            return None
        try:
            return StandingAnswer.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("standing.py: failed to load %s: %s", path, exc)
            return None

    def _save(self, answer: StandingAnswer) -> None:
        payload = json.dumps(
            answer.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        atomic_write_text(self._path(answer.id), payload)

    def register(self, question: str, *, now: datetime | None = None) -> StandingAnswer:
        """Register ``question`` once. Idempotent.

        If ``question`` is already registered, returns the existing
        ``StandingAnswer`` unchanged -- registration never overwrites an
        existing answer, since the question is the stable identity.

        Args:
            question: The question text to register.
            now: Override for "now" (tests). Defaults to UTC now.

        Returns:
            StandingAnswer: The existing or newly created standing answer.
        """
        answer_id = standing_answer_id(question)
        existing = self._load(answer_id)
        if existing is not None:
            return existing
        created = StandingAnswer(
            id=answer_id,
            question=question,
            answer="",
            last_refreshed_at=now or datetime.now(timezone.utc),
            last_memory_seen_at=None,
            last_refresh_failed_at=None,
        )
        self._save(created)
        return created

    def get(self, question: str) -> StandingAnswer | None:
        """Plain read of a registered question's standing answer.

        No retrieval, no LLM call, no network -- this is one file read and
        one JSON parse. Returns ``None`` if ``question`` was never
        registered (or its file is unreadable).

        Args:
            question: The question text, exactly as registered.

        Returns:
            StandingAnswer | None: The stored answer, or ``None``.
        """
        return self._load(standing_answer_id(question))

    def list_answers(self) -> list[StandingAnswer]:
        """Plain read of every standing answer registered for this agent."""
        if not self._dir.exists():
            return []
        answers: list[StandingAnswer] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                answers.append(
                    StandingAnswer.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as exc:
                logger.warning("standing.py: failed to load %s: %s", path, exc)
        return answers

    def refresh_if_stale(
        self,
        question: str,
        store_or_newest_timestamp: NewestTimestampSource | datetime | None,
        compute: AnswerComputer,
        *,
        now: datetime | None = None,
    ) -> RefreshOutcome:
        """Refresh ``question``'s standing answer if, and only if, it is stale.

        Registers ``question`` first if this is its first refresh (see
        :meth:`register`). When the existing answer is not stale, this is a
        no-op: ``compute`` is never called, nothing is written, and the
        outcome says so (``reason="not_stale"``).

        When stale, ``compute`` runs exactly once, through the injected
        ``FreshContextRunner`` seam, receiving the current ``StandingAnswer``.

        On success, the watermark advances to
        ``max(existing watermark, computed.newest_memory_seen_at)`` -- see
        the module docstring for why a ``None`` from ``compute`` must not
        advance it. The answer text and ``last_refreshed_at`` update, and
        any prior ``last_refresh_failed_at`` is cleared.

        On failure (``compute`` or the runner raises), the exception is
        caught here: it is logged, ``last_refresh_failed_at`` is recorded
        and persisted, and the answer text and watermark are left exactly
        as they were. This method itself never raises for a failed
        refresh -- callers get the failure back in the outcome, not an
        exception, so a boot-time refresh sweep over many standing answers
        can't be taken down by one bad compute.

        Args:
            question: The question text to refresh.
            store_or_newest_timestamp: The newest memory timestamp in
                scope, or something that can report it. Passed straight to
                :meth:`StandingAnswer.is_stale`.
            compute: The injected pass that produces a new answer.
            now: Override for "now" (tests). Defaults to UTC now.

        Returns:
            RefreshOutcome: What happened, and the resulting answer.
        """
        resolved_now = now or datetime.now(timezone.utc)
        existing = self.register(question, now=resolved_now)

        if not existing.is_stale(store_or_newest_timestamp):
            return RefreshOutcome(refreshed=False, reason="not_stale", answer=existing)

        def pass_fn() -> ComputedAnswer:
            return compute(existing)

        try:
            computed = self._runner(pass_fn)
        except Exception as exc:  # deliberately broad: a bad compute must not crash the caller
            logger.warning("standing.py: refresh failed for question id=%s: %s", existing.id, exc)
            failed = existing.model_copy(update={"last_refresh_failed_at": resolved_now})
            self._save(failed)
            return RefreshOutcome(refreshed=False, reason=f"failed: {exc}", answer=failed)

        new_watermark = _max_optional(existing.last_memory_seen_at, computed.newest_memory_seen_at)
        refreshed = existing.model_copy(
            update={
                "answer": computed.answer,
                "last_refreshed_at": resolved_now,
                "last_memory_seen_at": new_watermark,
                "last_refresh_failed_at": None,
            }
        )
        self._save(refreshed)
        return RefreshOutcome(refreshed=True, reason="refreshed", answer=refreshed)
