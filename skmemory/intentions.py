"""Intentions: typed prospective memory with a code-owned lifecycle (SKMEM-P2-01).

An intention is "remember to do X when Y": either a *time* intention (due_at)
or an *event* intention (trigger_terms matched against later prompts).

Storage
-------
* Source of truth: one flat JSON file per intention under
  ``<memory base>/intentions/<id>.json``, written with ``atomic_write_text``.
  One file per intention (never a shared JSON file) so Syncthing replicas that
  change different intentions concurrently do not conflict.
* ``index.db`` (same file the memory index uses) gets an ``intentions`` table.
  It is derived and disposable: :meth:`IntentionStore.reindex` rebuilds it from
  the flat files. Call it after a Syncthing pull, since ``list`` reads the index.

Lifecycle
---------
:func:`transition` is the only place state changes are decided. The table
follows ``intention-transitions-v1.jsonl``. Decision recorded here: ``fire``
from ``pending`` goes to ``fired`` (a due time intention fires without ever
being armed, as int-002 does; the fixture file gained a pending+fire row).
Terminal states (done, cancelled, expired, invalidated) ignore every event.
Cooldown, max_fires and scope gating belong to the prefilter (P2-02), not here.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"done", "cancelled", "expired", "invalidated"})
_LAYERS = ("short-term", "mid-term", "long-term")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: (state, event) -> next state. Anything absent for a non-terminal state raises.
_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "arm"): "armed",
    ("pending", "fire"): "fired",
    ("pending", "snooze"): "pending",
    ("pending", "cancel"): "cancelled",
    ("pending", "expire"): "expired",
    ("pending", "invalidate"): "invalidated",
    ("armed", "fire"): "fired",
    ("armed", "snooze"): "armed",
    ("armed", "done"): "done",
    ("armed", "cancel"): "cancelled",
    ("armed", "expire"): "expired",
    ("armed", "invalidate"): "invalidated",
    ("fired", "fire"): "fired",
    ("fired", "snooze"): "armed",
    ("fired", "done"): "done",
    ("fired", "cancel"): "cancelled",
    ("fired", "expire"): "expired",
    ("fired", "invalidate"): "invalidated",
}
EVENTS = frozenset(e for _, e in _TRANSITIONS)

State = Literal["pending", "armed", "fired", "done", "cancelled", "expired", "invalidated"]


def _aware(v: Any) -> Any:
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class Intention(BaseModel):
    """A prospective memory. Unknown fields are kept so a newer host's data survives a rewrite."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["time", "event"]
    title: str
    state: State = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    origin_session: str | None = None
    origin_memory_id: str | None = None
    channel: str = "inline"
    # time intentions
    due_at: datetime | None = None
    snooze_until: datetime | None = None
    # event intentions
    trigger_terms: list[str] = Field(default_factory=list)
    scope: dict[str, str] = Field(default_factory=dict)
    cooldown_s: int = 0
    max_fires: int | None = None
    fires: int = 0
    last_fired_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("cooldown_s", "fires", mode="before")
    @classmethod
    def _none_is_zero(cls, v: Any) -> Any:
        return 0 if v is None else v

    @field_validator("created_at", "due_at", "snooze_until", "last_fired_at", "expires_at")
    @classmethod
    def _tz(cls, v: Any) -> Any:
        return _aware(v)


def transition(
    intention: Intention,
    event: str,
    *,
    now: datetime | None = None,
    snooze_until: datetime | None = None,
) -> Intention:
    """Return the intention after ``event``; the input is never mutated.

    Terminal states ignore every event. A non-terminal state given an event it
    has no row for raises ``ValueError`` (as does an unknown event name).
    """
    if event not in EVENTS:
        raise ValueError(f"unknown intention event: {event!r}")
    if intention.state in TERMINAL_STATES:
        return intention.model_copy()
    nxt = _TRANSITIONS.get((intention.state, event))
    if nxt is None:
        raise ValueError(f"no transition from {intention.state!r} on {event!r}")
    upd: dict[str, Any] = {"state": nxt}
    if event == "fire":
        upd["fires"] = intention.fires + 1
        upd["last_fired_at"] = _aware(now) or datetime.now(timezone.utc)
    elif event == "snooze" and snooze_until is not None:
        upd["snooze_until"] = _aware(snooze_until)
    return intention.model_copy(update=upd)


def _check_id(intention_id: str) -> str:
    if not isinstance(intention_id, str) or not _ID_RE.match(intention_id):
        raise ValueError(f"invalid intention id: {intention_id!r}")
    return intention_id


def _parse_dt(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return _aware(datetime.fromisoformat(v.replace("Z", "+00:00")))
    except ValueError:
        return None


class IntentionStore:
    """Flat-file store (source of truth) with a derived ``intentions`` table in index.db."""

    def __init__(self, base_path: str | Path) -> None:
        self.base_path = Path(base_path)
        self.dir = self.base_path / "intentions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.base_path / "index.db"
        self._conn: sqlite3.Connection | None = None
        self._ensure_table()

    # -- index ---------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_table(self) -> None:
        db = self._db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS intentions ("
            "id TEXT PRIMARY KEY, kind TEXT, state TEXT, due_at TEXT, snooze_until TEXT,"
            " expires_at TEXT, origin_memory_id TEXT, channel TEXT, data TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_intentions_state ON intentions(state)")
        db.commit()

    def _index(self, i: Intention, body: str) -> None:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        self._db().execute(
            "INSERT OR REPLACE INTO intentions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                i.id, i.kind, i.state, iso(i.due_at), iso(i.snooze_until),
                iso(i.expires_at), i.origin_memory_id, i.channel, body,
            ),
        )  # fmt: skip
        self._db().commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- CRUD ----------------------------------------------------------
    def _path(self, intention_id: str) -> Path:
        return self.dir / f"{_check_id(intention_id)}.json"

    def save(self, intention: Intention) -> None:
        """Write the flat file atomically, then index it (flat first, as for memories)."""
        body = intention.model_dump_json(indent=2)
        atomic_write_text(self._path(intention.id), body)
        self._index(intention, body)

    def get(self, intention_id: str) -> Intention | None:
        """Read from the flat file (source of truth), not the index."""
        p = self._path(intention_id)
        if not p.exists():
            return None
        return Intention(**json.loads(p.read_text(encoding="utf-8")))

    def list(self, state: str | None = None) -> list[Intention]:
        """List from the index. Run :meth:`reindex` after files arrive from a sync."""
        q, args = "SELECT data FROM intentions", ()
        if state:
            q, args = q + " WHERE state=?", (state,)
        rows = self._db().execute(q + " ORDER BY id", args).fetchall()
        return [Intention(**json.loads(r["data"])) for r in rows]

    def reindex(self) -> int:
        """Rebuild the table from the flat files; unreadable files are skipped."""
        db = self._db()
        db.execute("DELETE FROM intentions")
        db.commit()
        n = 0
        for p in sorted(self.dir.glob("*.json")):
            try:
                i = Intention(**json.loads(p.read_text(encoding="utf-8")))
                _check_id(i.id)
            except Exception as e:  # noqa: BLE001 - one bad file must not block the rest
                logger.warning("skipping unreadable intention file %s: %s", p.name, e)
                continue
            self._index(i, i.model_dump_json(indent=2))
            n += 1
        return n

    # -- supersession sweep -------------------------------------------
    def _origin_valid_to(self, memory_id: str) -> datetime | None:
        """valid_to of the origin memory, read defensively from the raw flat JSON.

        The v2 schema (valid_to) may not be in the Memory model on this node, so
        read the file directly: top-level ``valid_to`` first, then ``metadata``.
        """
        try:
            _check_id(memory_id)
        except ValueError:
            return None
        for layer in _LAYERS:
            p = self.base_path / layer / f"{memory_id}.json"
            if not p.exists():
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            meta = raw.get("metadata")
            v = raw.get("valid_to") or (meta.get("valid_to") if isinstance(meta, dict) else None)
            return _parse_dt(v)
        return None

    def sweep_supersession(self, now: datetime | None = None) -> list[str]:
        """Invalidate live intentions whose origin memory's ``valid_to`` has passed.

        Returns the ids changed. A future valid_to, a missing origin file or an
        unparseable value leaves the intention alone.
        """
        now = _aware(now) or datetime.now(timezone.utc)
        changed: list[str] = []
        for i in self.list():
            if i.state in TERMINAL_STATES or not i.origin_memory_id:
                continue
            vt = self._origin_valid_to(i.origin_memory_id)
            if vt is not None and vt <= now:
                self.save(transition(i, "invalidate", now=now))
                changed.append(i.id)
        return changed
