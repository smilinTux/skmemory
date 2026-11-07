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

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # POSIX only; without it locking degrades to a warning
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

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


#: Merge precedence when two replicas disagree. Terminal beats live; among
#: terminal states done > cancelled > invalidated > expired; among live states
#: fired > armed > pending. A higher fires count wins next, then the later
#: last_fired_at, then the canonical JSON text as a total-order tie-break, so
#: every host picks the same winner regardless of merge order.
_STATE_RANK = {
    "pending": 0, "armed": 1, "fired": 2,
    "expired": 10, "invalidated": 11, "cancelled": 12, "done": 13,
}  # fmt: skip
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _merge_key(i: Intention) -> tuple:
    return (
        _STATE_RANK[i.state],
        i.fires,
        i.last_fired_at or _EPOCH,
        i.model_dump_json(),
    )


def merge_intentions(a: Intention, b: Intention) -> Intention:
    """Deterministic, commutative merge of two replicas of one intention.

    The winner (by :func:`_merge_key`) supplies the record; ``fires`` and
    ``last_fired_at`` are the maxima of both so no fire is forgotten.
    """
    win = max((a, b), key=_merge_key)
    lasts = [d for d in (a.last_fired_at, b.last_fired_at) if d]
    return win.model_copy(
        update={"fires": max(a.fires, b.fires), "last_fired_at": max(lasts) if lasts else None}
    )


def _lock_root() -> Path:
    """Host-local, never-synced directory for lock files.

    ``$XDG_RUNTIME_DIR`` (a per-user tmpfs, not under any Syncthing folder)
    when set, else the system temp dir. Lock files must not live in the
    synced memory dir: they would replicate and mean nothing on other hosts.
    """
    root = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(root) / f"skmemory-intent-locks-{os.getuid() if hasattr(os, 'getuid') else 0}"


class IntentionStore:
    """Flat-file store (source of truth) with a derived ``intentions`` table in index.db."""

    def __init__(self, base_path: str | Path) -> None:
        self.base_path = Path(base_path)
        self.dir = self.base_path / "intentions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.base_path / "index.db"
        self._conn: sqlite3.Connection | None = None
        #: Count of unreadable files/rows skipped by this store instance.
        self.unreadable = 0
        self._lock_dir = (
            _lock_root() / hashlib.sha1(str(self.base_path.resolve()).encode()).hexdigest()[:16]
        )
        self.last_reindex_report: dict[str, Any] = {}
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

    @staticmethod
    def _row(i: Intention, body: str) -> tuple:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return (
            i.id, i.kind, i.state, iso(i.due_at), iso(i.snooze_until),
            iso(i.expires_at), i.origin_memory_id, i.channel, body,
        )  # fmt: skip

    def _index(self, i: Intention, body: str) -> None:
        db = self._db()
        with db:
            db.execute(
                "INSERT OR REPLACE INTO intentions VALUES (?,?,?,?,?,?,?,?,?)", self._row(i, body)
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- CRUD ----------------------------------------------------------
    def _path(self, intention_id: str) -> Path:
        return self.dir / f"{_check_id(intention_id)}.json"

    def _read(self, intention_id: str) -> Intention | None:
        """Read the flat file. Missing -> None; corrupt -> None, counted and logged."""
        p = self._path(intention_id)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            return self._unreadable(p, e)
        try:
            return Intention(**json.loads(raw))
        except Exception as e:  # noqa: BLE001 - corrupt/partial file must not abort callers
            return self._unreadable(p, e)

    def _unreadable(self, p: Path, err: Exception) -> None:
        self.unreadable += 1
        logger.warning("unreadable intention file %s: %s", p.name, err)
        return None

    @contextlib.contextmanager
    def _lock(self, intention_id: str) -> Iterator[None]:
        """Exclusive flock on a per-intention, host-local lock file.

        Held across read, transition and atomic write so two local processes
        cannot interleave. Cross-host races are not covered by a lock; they are
        resolved by :func:`merge_intentions` at reindex. If locking is
        unavailable (no fcntl, unwritable lock dir, flock error) this logs and
        proceeds unlocked rather than blocking the write.
        """
        fd = None
        if fcntl is not None:
            try:
                self._lock_dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self._lock_dir / f"{_check_id(intention_id)}.lock",
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as e:
                logger.warning(
                    "intention lock unavailable for %s, proceeding unlocked: %s", intention_id, e
                )
                if fd is not None:
                    os.close(fd)
                    fd = None
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _write(self, intention: Intention) -> None:
        body = intention.model_dump_json(indent=2)
        atomic_write_text(self._path(intention.id), body)
        self._index(intention, body)

    def save(self, intention: Intention) -> None:
        """Create an intention, or rewrite one whose state is unchanged on disk.

        The flat file is truth. A save that would change the state of an
        existing intention is refused (use :meth:`apply`), so a dead intention
        cannot be revived by writing a stale copy. Flat file first, then index.
        """
        with self._lock(intention.id):
            on_disk = self._read(intention.id)
            if on_disk is not None and on_disk.state != intention.state:
                raise ValueError(
                    f"refusing state change {on_disk.state!r} -> {intention.state!r} for "
                    f"{intention.id!r} via save(); use apply() so transition() decides"
                )
            self._write(intention)

    def apply(
        self,
        intention_id: str,
        event: str,
        *,
        now: datetime | None = None,
        snooze_until: datetime | None = None,
    ) -> Intention | None:
        """Re-read the flat file, apply :func:`transition`, write. The only state-change path.

        Returns the resulting intention, or None if the file is missing or
        unreadable. Terminal states stay terminal. Serialised between local
        processes by a flock; hosts racing through Syncthing are reconciled by the
        merge in :meth:`reindex`.
        """
        with self._lock(intention_id):
            cur = self._read(intention_id)
            if cur is None:
                return None
            new = transition(cur, event, now=now, snooze_until=snooze_until)
            if new != cur:
                self._write(new)
            return new

    def get(self, intention_id: str) -> Intention | None:
        """Read from the flat file (source of truth); a corrupt file reads as None."""
        return self._read(intention_id)

    def list(self, state: str | None = None) -> list[Intention]:
        """List from the index, skipping unparseable rows. Run :meth:`reindex` after a sync."""
        q, args = "SELECT id, data FROM intentions", ()
        if state:
            q, args = q + " WHERE state=?", (state,)
        out: list[Intention] = []
        for r in self._db().execute(q + " ORDER BY id", args).fetchall():
            try:
                out.append(Intention(**json.loads(r["data"])))
            except Exception as e:  # noqa: BLE001
                self.unreadable += 1
                logger.warning("unreadable intention index row %s: %s", r["id"], e)
        return out

    def reindex(self) -> int:
        """Rebuild the table from the flat files in one transaction; returns rows indexed.

        Syncthing conflict copies (``a1.sync-conflict-*.json``, any file whose
        stem is not the id inside) are merged, not dropped: all replicas of an
        id are combined with :func:`merge_intentions` (terminal beats live,
        fired > armed > pending, higher fires wins), the result is written
        atomically to the canonical ``<id>.json``, and only then are the
        conflict copies removed. A conflict-only id is promoted to canonical.
        The merge is deterministic, so two hosts converge. Details of what was
        merged and any unreadable files are in ``last_reindex_report``.
        """
        groups: dict[str, list[Path]] = {}
        bad: list[str] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                i = Intention(**json.loads(p.read_text(encoding="utf-8")))
                _check_id(i.id)
            except Exception as e:  # noqa: BLE001 - one bad file must not block the rest
                logger.warning("skipping unreadable intention file %s: %s", p.name, e)
                bad.append(p.name)
                continue
            groups.setdefault(i.id, []).append(p)
        rows, merged = [], []
        for iid, paths in groups.items():
            if len(paths) == 1 and paths[0].stem == iid:
                rows.append(self._row(*self._row_args(paths[0])))
                continue
            with self._lock(iid):
                res = self._merge_group(iid)
            if res is not None:
                merged.append({"id": iid, "files": [p.name for p in paths if p.stem != iid]})
                rows.append(self._row(res, res.model_dump_json(indent=2)))
        db = self._db()
        with db:  # one transaction: readers never see an empty table
            db.execute("DELETE FROM intentions")
            db.executemany("INSERT INTO intentions VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.unreadable += len(bad)
        self.last_reindex_report = {
            "indexed": len(rows),
            "merged": merged,
            "conflicts": [n for m in merged for n in m["files"]],
            "unreadable": bad,
        }
        return len(rows)

    @staticmethod
    def _row_args(p: Path) -> tuple[Intention, str]:
        i = Intention(**json.loads(p.read_text(encoding="utf-8")))
        return i, i.model_dump_json(indent=2)

    def _merge_group(self, iid: str) -> Intention | None:
        """Merge canonical + conflict copies of ``iid`` (caller holds the lock).

        Re-reads every file under the lock. The canonical file is rewritten
        only if the merge differs from it; conflict copies are unlinked only
        after that write landed.
        """
        canon = self._path(iid)
        replicas: list[Intention] = []
        copies: list[Path] = []
        canon_val: Intention | None = None
        for p in sorted(self.dir.glob("*.json")):
            try:
                i = Intention(**json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - reported by the caller's scan
                continue
            if i.id != iid:
                continue
            replicas.append(i)
            if p == canon:
                canon_val = i
            else:
                copies.append(p)
        if not replicas:
            return None
        result = replicas[0]
        for r in replicas[1:]:
            result = merge_intentions(result, r)
        if canon_val is None or result != canon_val:
            self._write(result)  # atomic; raises before any copy is removed
        for p in copies:
            logger.warning("merged intention conflict copy %s into %s.json", p.name, iid)
            with contextlib.suppress(OSError):
                p.unlink()
        return result

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
        for iid in [i.id for i in self.list()]:  # index only supplies candidate ids
            cur = self._read(iid)  # flat file is truth; the index row may be stale
            if cur is None or cur.state in TERMINAL_STATES or not cur.origin_memory_id:
                continue
            vt = self._origin_valid_to(cur.origin_memory_id)
            if vt is not None and vt <= now:
                self.apply(iid, "invalidate", now=now)
                changed.append(iid)
        return changed
