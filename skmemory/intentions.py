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
being armed, as int-002 does; a pending intention can also be marked done, a
user action; the fixture file gained pending+fire and pending+done rows).
Terminal states (done, cancelled, expired, invalidated) ignore every event.
Cooldown, max_fires and scope gating belong to the prefilter, not here.

Prefilter (SKMEM-P2-02)
-----------------------
:meth:`IntentionStore.prefilter` is the no-LLM event matcher run on every user
prompt. Candidates come from lean columns of the ``intentions`` table (no JSON
parse or pydantic per row), but the fire itself is decided on the *flat file*
under the per-intention flock and goes through :func:`transition`, so one fire
updates flat and index together and concurrent prompts cannot double-fire.
Event intentions in state ``armed`` or ``fired`` are eligible (a fired
intention stays live until max_fires, expiry or a terminal event); ``pending``
is not armed and is skipped. :func:`prefilter_safe` is the hook entry point:
it never raises and gives up quickly when the store is locked.
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
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # POSIX only; without it locking degrades to a warning
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from . import wal_guard
from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"done", "cancelled", "expired", "invalidated"})
_LAYERS = ("short-term", "mid-term", "long-term")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: (state, event) -> next state. Anything absent for a non-terminal state raises.
_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "arm"): "armed",
    ("pending", "fire"): "fired",
    ("pending", "done"): "done",
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
    #: Repeat every this many seconds: a fire re-arms with ``due_at`` advanced.
    interval_s: int | None = None
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


def _repeats(i: Intention, fires_after: int) -> bool:
    return (
        i.kind == "time"
        and bool(i.interval_s)
        and i.interval_s > 0
        and i.due_at is not None
        and (i.max_fires is None or fires_after < i.max_fires)
    )


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
        fired_at = _aware(now) or datetime.now(timezone.utc)
        upd["last_fired_at"] = fired_at
        if _repeats(intention, upd["fires"]):
            # Advance by whole intervals to the first slot strictly after now, so a
            # long outage yields one fire, not a storm of catch-up fires.
            step = intention.interval_s
            due = intention.due_at
            k = max(1, (fired_at - due) // timedelta(seconds=step) + 1)
            upd["state"] = "armed"
            upd["due_at"] = due + timedelta(seconds=step * k)
            upd["snooze_until"] = None
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


_SEP = "\x1f"
_WORD = re.compile(r"\w+")
_FIRST_TOKEN = _WORD
_TRAIL_WORD = re.compile(r"\w+$")
_LEAD_WORD = re.compile(r"^\w+")


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _env_num(name: str, default: Any, cast: Any) -> Any:
    try:
        return cast(os.environ.get(f"SKMEMORY_PREFILTER_{name}", default))
    except (TypeError, ValueError):
        return default


_BASE_COLS = (
    "id", "kind", "state", "due_at", "snooze_until", "expires_at", "origin_memory_id",
    "channel", "data",
)  # fmt: skip
_PREFILTER_COLS = (
    ("terms", "TEXT"),
    ("scope", "TEXT"),
    ("cooldown_s", "INTEGER"),
    ("max_fires", "INTEGER"),
    ("fires", "INTEGER"),
    ("last_fired_at", "TEXT"),
)
_ALL_COLS = _BASE_COLS + tuple(c for c, _ in _PREFILTER_COLS)
_UPSERT_SQL = (
    f"INSERT OR REPLACE INTO intentions ({','.join(_ALL_COLS)}) "
    f"VALUES ({','.join('?' * len(_ALL_COLS))})"
)


class _LockBusy(Exception):
    """A non-blocking intention lock could not be taken in time."""


@lru_cache(maxsize=2048)
def _term_re(term: str) -> re.Pattern[str]:
    """Phrase regex with word boundaries only where the term edge is a word char.

    ``fusion`` must not match inside ``confusion``; ``HOSTS=`` ends in a
    non-word char so a following ``"`` or letter is fine and no guard is added there.
    """
    pre = r"(?<!\w)" if re.match(r"\w", term[0]) else ""
    post = r"(?!\w)" if re.match(r"\w", term[-1]) else ""
    return re.compile(pre + re.escape(term) + post)


def _phrase_hit(terms: list[str], low: str) -> bool:
    # substring first: cheap reject before the regex
    return any(t and t in low and _term_re(t).search(low) for t in terms)


def _scope_ok(want: dict[str, str], have: dict[str, str]) -> bool:
    return all(k in have and have[k] == v for k, v in want.items())


def _gate_ok(
    state: str,
    fires: int,
    max_fires: int | None,
    cooldown_s: int,
    last: datetime | None,
    expires: datetime | None,
    now: datetime,
) -> bool:
    if state not in ("armed", "fired"):
        return False
    if expires is not None and expires <= now:
        return False
    if max_fires is not None and fires >= max_fires:
        return False
    return not (last is not None and cooldown_s and (now - last).total_seconds() < cooldown_s)


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

    def __init__(self, base_path: str | Path, *, busy_timeout_ms: int = 30000) -> None:
        self.base_path = Path(base_path)
        self._busy_ms = int(busy_timeout_ms)
        self.dir = self.base_path / "intentions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.base_path / "index.db"
        self._conn: sqlite3.Connection | None = None
        self._gen = 0  # bumped on every index write by this instance
        self._pf_cache: tuple | None = None
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
            self._conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False, timeout=self._busy_ms / 1000
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={self._busy_ms}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            wal_guard.apply(self._conn)
        return self._conn

    def _ensure_table(self) -> None:
        db = self._db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS intentions ("
            "id TEXT PRIMARY KEY, kind TEXT, state TEXT, due_at TEXT, snooze_until TEXT,"
            " expires_at TEXT, origin_memory_id TEXT, channel TEXT, data TEXT NOT NULL)"
        )
        have = {r[1] for r in db.execute("PRAGMA table_info(intentions)")}
        added = False
        for col, typ in _PREFILTER_COLS:
            if col not in have:
                db.execute(f"ALTER TABLE intentions ADD COLUMN {col} {typ}")
                added = True
        db.execute("CREATE INDEX IF NOT EXISTS idx_intentions_state ON intentions(state)")
        db.commit()
        if added:  # rows written before the prefilter columns existed: derive them from data
            self._backfill_prefilter_cols()

    def _backfill_prefilter_cols(self) -> None:
        db = self._db()
        with db:
            for r in db.execute("SELECT id, data FROM intentions").fetchall():
                try:
                    i = Intention(**json.loads(r["data"]))
                except Exception:  # noqa: BLE001 - unreadable rows are reported by list()
                    continue
                db.execute(
                    "UPDATE intentions SET terms=?, scope=?, cooldown_s=?, max_fires=?, fires=?,"
                    " last_fired_at=? WHERE id=?",
                    (*self._prefilter_vals(i), i.id),
                )

    @staticmethod
    def _prefilter_vals(i: Intention) -> tuple:
        terms = [
            t.replace(_SEP, " ").strip().lower()
            for t in i.trigger_terms
            if isinstance(t, str) and t.strip()
        ]
        return (
            _SEP.join(terms),  # cheaper than JSON on the per-prompt scan
            json.dumps(i.scope, sort_keys=True),
            i.cooldown_s,
            i.max_fires,
            i.fires,
            i.last_fired_at.isoformat() if i.last_fired_at else None,
        )

    @staticmethod
    def _row(i: Intention, body: str) -> tuple:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return (
            i.id, i.kind, i.state, iso(i.due_at), iso(i.snooze_until),
            iso(i.expires_at), i.origin_memory_id, i.channel, body,
            *IntentionStore._prefilter_vals(i),
        )  # fmt: skip

    def _index(self, i: Intention, body: str) -> None:
        db = self._db()
        self._gen += 1
        with db:
            db.execute(_UPSERT_SQL, self._row(i, body))

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
    def _lock(self, intention_id: str, timeout: float | None = None) -> Iterator[None]:
        """Exclusive flock on a per-intention, host-local lock file.

        Held across read, transition and atomic write so two local processes
        cannot interleave. Cross-host races are not covered by a lock; they are
        resolved by :func:`merge_intentions` at reindex. If locking is
        unavailable (no fcntl, unwritable lock dir, flock error) this logs and
        proceeds unlocked rather than blocking the write. With ``timeout`` the
        lock is polled non-blocking and :class:`_LockBusy` is raised on expiry.
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
                if timeout is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                os.close(fd)
                                fd = None
                                raise _LockBusy(intention_id) from None
                            time.sleep(0.001)
            except _LockBusy:
                raise
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
        self._gen += 1
        with db:  # one transaction: readers never see an empty table
            db.execute("DELETE FROM intentions")
            db.executemany(_UPSERT_SQL, rows)
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

    # -- prefilter (SKMEM-P2-02) ---------------------------------------
    def _stamp(self) -> tuple:
        """Cheap change key for the index: db and WAL stat plus this instance's write count."""
        out: list[Any] = [self._gen]
        for suffix in ("", "-wal"):
            try:
                st = os.stat(str(self._db_path) + suffix)
                out += [st.st_mtime_ns, st.st_size]
            except OSError:
                out += [0, 0]
        return tuple(out)

    def _matcher(self) -> tuple[dict[str, list[str]], list[str], dict[str, list[str]]]:
        """(first-token -> ids, ids with no word-char term, id -> terms), cached on the index stamp.

        Every term that contains a word char is a whole-token phrase start: its
        first ``\\w+`` run can only match a complete token of the prompt (a
        neighbouring literal or a boundary guard stops it). So one tokenisation
        of the prompt plus set lookups finds every candidate; the regex then
        only verifies those. Terms with no word char are always verified.
        """
        key = self._stamp()
        if self._pf_cache is not None and self._pf_cache[0] == key:
            return self._pf_cache[1]
        by_tok: dict[str, list[str]] = {}
        nonword: list[str] = []
        terms_of: dict[str, list[str]] = {}
        for iid, terms in self._db().execute(
            "SELECT id, terms FROM intentions WHERE kind='event' AND state IN ('armed','fired')"
        ):
            if not terms:
                continue
            ts = [t for t in terms.split(_SEP) if t]
            terms_of[iid] = ts
            for t in ts:
                m = _FIRST_TOKEN.search(t)
                if m is None:
                    nonword.append(iid)
                else:
                    by_tok.setdefault(m.group(0), []).append(iid)
        val = (by_tok, nonword, terms_of)
        self._pf_cache = (key, val)
        return val

    def prefilter(
        self,
        text: str,
        scope: dict[str, str] | None = None,
        now: datetime | None = None,
        *,
        deadline_s: float | None = None,
        max_fires: int | None = None,
        _t0: float | None = None,
    ) -> list[Intention]:
        """Fire and return the event intentions this prompt triggers.

        Case-insensitive, word-bounded phrase match of any trigger term (no
        stemming); every key of the intention's scope must equal the prompt
        scope; skipped when not armed/fired, expired, at max_fires, or inside
        the cooldown. Candidates are chosen from the index, then each is
        re-checked and fired on the flat file under its lock via
        :func:`transition`, so a stale index cannot double-fire and flat and
        index move together.

        Bounded work: the whole call has one ``deadline_s`` (default 50 ms,
        env ``SKMEMORY_PREFILTER_DEADLINE_S``); at most ``max_fires`` (default
        3, ``SKMEMORY_PREFILTER_MAX_FIRES``) intentions fire; the fire path only
        try-locks, so a locked intention is skipped this prompt; only the head
        and tail of ``SKMEMORY_PREFILTER_MAX_CHARS`` (default 16384) of a long
        prompt is scanned (a token split by a cut is dropped, so ``fusion`` never
        fires on the tail of ``confusion``). Multi-word terms match only with
        exactly one space between words (no whitespace normalisation). Run :meth:`sweep_supersession` first when
        invalidation must be honoured.
        """
        t0 = _t0 if _t0 is not None else time.monotonic()
        end = t0 + (deadline_s if deadline_s is not None else _env_num("DEADLINE_S", 0.05, float))
        cap = max_fires if max_fires is not None else _env_num("MAX_FIRES", 3, int)
        limit = _env_num("MAX_CHARS", 16384, int)
        now = _aware(now) or datetime.now(timezone.utc)
        text = text or ""
        if len(text) > limit:  # head and tail; NUL cannot join two words into a phrase
            half = max(limit // 2, 1)
            head, tail = text[:half], text[-half:]
            if _is_word(text[half - 1]) and _is_word(text[half]):
                head = _TRAIL_WORD.sub("", head)  # the cut split a token: drop its stub
            if _is_word(text[-half - 1]) and _is_word(text[-half]):
                tail = _LEAD_WORD.sub("", tail)
            text = head + "\x00" + tail
        low = text.lower()
        if not low.strip() or cap <= 0:
            return []
        have = scope or {}
        by_tok, nonword, terms_of = self._matcher()
        ids = set(nonword)
        for tok in set(_WORD.findall(low)):
            hit = by_tok.get(tok)
            if hit:
                ids.update(hit)
        db = self._db()
        fired: list[Intention] = []
        for iid in sorted(ids):
            if time.monotonic() >= end or len(fired) >= cap:
                break
            if not _phrase_hit(terms_of[iid], low):
                continue
            r = db.execute(
                "SELECT scope, cooldown_s, max_fires, fires, last_fired_at, expires_at, state"
                " FROM intentions WHERE id=?",
                (iid,),
            ).fetchone()
            try:
                if r is None or not _scope_ok(json.loads(r["scope"] or "{}"), have):
                    continue
                if not _gate_ok(
                    r["state"], r["fires"] or 0, r["max_fires"], r["cooldown_s"] or 0,
                    _parse_dt(r["last_fired_at"]), _parse_dt(r["expires_at"]), now,
                ):  # fmt: skip
                    continue
            except (ValueError, TypeError):
                continue
            got = self._fire_if_eligible(iid, low, have, now)
            if got is not None:
                fired.append(got)
        return fired

    def _fire_if_eligible(
        self, iid: str, low: str, have: dict[str, str], now: datetime
    ) -> Intention | None:
        """Re-check on the flat file and fire under a try-lock. None if not fired or locked."""
        try:
            with self._lock(iid, timeout=0):
                cur = self._read(iid)
                if cur is None or cur.kind != "event":
                    return None
                terms = [t.strip().lower() for t in cur.trigger_terms if isinstance(t, str)]
                if not (
                    _phrase_hit(terms, low)
                    and _scope_ok(cur.scope, have)
                    and _gate_ok(
                        cur.state,
                        cur.fires,
                        cur.max_fires,
                        cur.cooldown_s,
                        cur.last_fired_at,
                        cur.expires_at,
                        now,
                    )  # fmt: skip
                ):
                    return None
                new = transition(cur, "fire", now=now)  # transition() owns the state change
                body = new.model_dump_json(indent=2)
                atomic_write_text(self._path(iid), body)  # flat is truth: write it first
                try:
                    self._index(new, body)
                except sqlite3.Error as e:  # flat is saved; reindex heals the derived row
                    logger.warning("intention %s fired but index update failed: %s", iid, e)
                return new
        except _LockBusy:
            return None

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


_IN_RE = re.compile(
    r"^\s*in\s+(\d{1,6})\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\s*$", re.IGNORECASE
)
_IN_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_due_at(value: Any, now: datetime | None = None) -> datetime:
    """Parse ``due_at``: an ISO 8601 timestamp or ``in N minutes|hours|days|weeks``.

    Relative forms are resolved against ``now`` (default: the current time). A
    naive ISO time is taken as UTC. Raises ``ValueError`` with a message that
    names the accepted formats; it never returns a guess.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("due_at is required: an ISO 8601 time or 'in N days/hours'")
    m = _IN_RE.match(value)
    if m:
        from datetime import timedelta

        unit = _IN_UNIT[m.group(2)[0].lower()]
        return now + timedelta(**{unit: int(m.group(1))})
    try:
        return _aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(
            f"unrecognised due_at {value!r}: use an ISO 8601 time "
            "(2026-12-01T09:00:00Z) or 'in N days/hours/minutes/weeks'"
        ) from None


def prefilter_safe(
    base_path: str | Path,
    text: str,
    scope: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[Intention]:
    """Hook entry point: :meth:`IntentionStore.prefilter` that can never raise.

    Any failure (missing dir, locked or corrupt database, bad input) returns
    ``[]`` and is logged at debug. SQLite waits at most 50 ms, intention locks are
    try-locks and the whole call shares one deadline (see :meth:`prefilter`).
    """
    store = None
    t0 = time.monotonic()
    try:
        store = IntentionStore(base_path, busy_timeout_ms=50)
        return store.prefilter(text, scope, now, _t0=t0)
    except Exception as e:  # noqa: BLE001 - runs on every prompt, must not raise into the hook
        logger.debug("intention prefilter skipped: %s", e)
        return []
    finally:
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()
