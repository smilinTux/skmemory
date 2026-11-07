"""Outcome ledger: served, followed, effect per skill version (SKMEM-P4-01).

Storage
-------
* Source of truth: append-only JSONL under ``<memory base>/ledger/``, one file
  per host per month (``outcomes-<host>-<YYYY-MM>.jsonl``). A host only ever
  appends to its own file, so Syncthing replicas never edit the same file and
  never produce conflict copies. Readers merge every file and dedupe by a
  stable event id (a hash of the event fields), so a replayed or duplicated
  line counts once.
* ``index.db`` gets a ``ledger_events`` table. It is derived and disposable:
  :meth:`Ledger.reindex` rebuilds it from the files.
* The API has no update or delete: history is only ever appended to.
* Events carry no transcript text: skill id, version, event, timestamp,
  effect and session id only.

Scoring
-------
Each ``followed`` event with an effect is weighted by exponential decay
(half-life 90 days). Harm counts ``HARM_WEIGHT`` times a success.
``follow_rate`` is raw followed / served, first-party events only: events whose source is in
``IMPORTED_SOURCES`` (Hermes counters) are reported in ``imported_*`` and excluded from rates,
the anti-pattern floor and the served-never-followed flag. ``followed_error`` is a follow
whose matched command failed; it carries no effect (a failure is not evidence of harm). A skill version is an anti-pattern
when decayed harm exceeds decayed success after at least 3 follows, and is
flagged ``served-never-followed`` when served >= 3 and followed == 0.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 90.0
HARM_WEIGHT = 4.0
MIN_FOLLOWS_ANTI = 3
MIN_SERVED_NEVER_FOLLOWED = 3
EVENTS = frozenset({"served", "followed", "followed_error"})
# Counter imports from another runtime: reported apart, never counted toward floors or rates.
IMPORTED_SOURCES = frozenset({"hermes-usage-counter"})
EFFECTS = frozenset({"success", "harm", "neutral"})
_FIELDS = ("skill", "version", "event", "at", "effect", "session")
_HOST_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _utc(v: datetime | str) -> datetime:
    d = datetime.fromisoformat(v) if isinstance(v, str) else v
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def event_id(
    skill: str, version: str, event: str, at: str, effect: str | None, session: str
) -> str:
    """Stable id: the same logical event hashes the same on every host."""
    raw = json.dumps([skill, version, event, at, effect, session], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Score:
    skill: str
    version: str
    served: int
    followed: int
    follow_rate: float
    net: float
    net_effect_sign: str
    anti_pattern: bool
    flag: str | None = None
    rank: int = 0
    imported_served: int = 0
    imported_followed: int = 0

    @property
    def key(self) -> str:
        return f"{self.skill}@{self.version}"


class Ledger:
    """Append-only outcome ledger with a derived SQLite index."""

    def __init__(self, base_path: str | Path, host: str | None = None) -> None:
        self.base_path = Path(base_path).expanduser()
        self.dir = self.base_path / "ledger"
        self._db_path = self.base_path / "index.db"
        self.host = _HOST_RE.sub("-", host or socket.gethostname().split(".")[0]) or "host"

    def _file(self, at: datetime) -> Path:
        return self.dir / f"outcomes-{self.host}-{at:%Y-%m}.jsonl"

    def record(
        self,
        skill: str,
        version: str,
        event: str,
        *,
        effect: str | None = None,
        session: str = "",
        at: datetime | str | None = None,
        event_id: str | None = None,
        source: str | None = None,
        missing: str | None = None,
    ) -> str:
        """Append one event to this host's file for the event's month. Returns the event id.

        ``event_id`` lets a producer that re-reads the same input (a transcript, a usage
        file) supply a stable id: a second ``record`` with a known id appends nothing.
        ``source`` says what produced the event and ``missing`` what is unknown about it
        (e.g. effect is null because only a heuristic saw the follow). Both are optional
        metadata, never scored. Never pass transcript text in either.
        """
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}")
        if effect is not None and effect not in EFFECTS:
            raise ValueError(f"unknown effect {effect!r}")
        if event == "served" and effect is not None:
            raise ValueError("a served event has no effect")
        if not skill or not version:
            raise ValueError("skill and version are required")
        when = _utc(at) if at is not None else datetime.now(timezone.utc)
        rec = {
            "skill": skill,
            "version": str(version),
            "event": event,
            "at": when.isoformat(),
            "effect": effect,
            "session": session,
        }
        # Random id minted once here: two genuine events with identical fields stay
        # distinct, while re-reading or re-importing the same line dedupes on this id.
        rec["id"] = event_id or uuid.uuid4().hex
        if source:
            rec["source"] = str(source)[:80]
        if missing:
            rec["missing"] = str(missing)[:200]
        if event_id and self._known(event_id):
            return event_id
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._file(when)
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with path.open("ab") as f:
            if f.tell() > 0:
                with path.open("rb") as r:
                    r.seek(-1, 2)
                    if r.read(1) != b"\n":  # torn tail: do not fuse onto it
                        f.write(b"\n")
            f.write(line.encode("utf-8"))
        self._index([rec])
        return rec["id"]

    def _known(self, eid: str) -> bool:
        """True when the derived index already holds this id (best effort: a miss just
        appends a line that readers dedupe by id)."""
        if not self._db_path.is_file():
            return False
        try:
            c = self._conn()
            with contextlib.closing(c):
                return (
                    c.execute("SELECT 1 FROM ledger_events WHERE id=?", (eid,)).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    def events(self) -> list[dict]:
        """Every event from every host file, deduped by id, ordered by time."""
        seen: dict[str, dict] = {}
        for p in sorted(self.dir.glob("outcomes-*.jsonl")) if self.dir.is_dir() else []:
            for n, raw in enumerate(p.read_bytes().split(b"\n"), 1):
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8"))
                    clean = {k: r[k] if k != "effect" else r.get("effect") for k in _FIELDS}
                    clean["version"] = str(clean["version"])
                    if clean["event"] not in EVENTS:
                        raise ValueError("bad event")
                    _utc(clean["at"])
                except (ValueError, KeyError, TypeError) as err:
                    logger.warning("ledger: skipping %s:%d (%s)", p.name, n, err)
                    continue
                stored = r.get("id")
                clean["id"] = (
                    stored
                    if isinstance(stored, str) and stored
                    else event_id(*(clean[k] for k in _FIELDS))
                )
                for extra in ("source", "missing"):
                    if isinstance(r.get(extra), str) and r[extra]:
                        clean[extra] = r[extra]
                seen.setdefault(clean["id"], clean)
        return sorted(seen.values(), key=lambda r: (_utc(r["at"]), r["id"]))

    # derived index -------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        self.base_path.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self._db_path), timeout=30)
        c.execute(
            "CREATE TABLE IF NOT EXISTS ledger_events (id TEXT PRIMARY KEY, skill TEXT, "
            "version TEXT, event TEXT, at TEXT, effect TEXT, session TEXT)"
        )
        return c

    def _index(self, recs: list[dict]) -> None:
        # The index is derived: a failure here must never lose the appended event.
        try:
            c = self._conn()
            with contextlib.closing(c), c:
                c.executemany(
                    "INSERT OR IGNORE INTO ledger_events VALUES (?,?,?,?,?,?,?)",
                    [(r["id"], *(r[k] for k in _FIELDS)) for r in recs],
                )
        except sqlite3.Error as err:
            logger.warning("ledger: index update failed (%s); run reindex", err)

    def reindex(self) -> int:
        """Rebuild ``ledger_events`` from the JSONL files. Call after a Syncthing pull."""
        recs = self.events()
        c = self._conn()
        with contextlib.closing(c), c:
            c.execute("DELETE FROM ledger_events")
            c.executemany(
                "INSERT INTO ledger_events VALUES (?,?,?,?,?,?,?)",
                [(r["id"], *(r[k] for k in _FIELDS)) for r in recs],
            )
        return len(recs)

    # scoring -------------------------------------------------------------
    def scores(self, as_of: datetime | str | None = None) -> list[Score]:
        """Score every skill version, ranked by decayed net effect (best first)."""
        now = _utc(as_of) if as_of is not None else datetime.now(timezone.utc)
        agg: dict[tuple[str, str], list[float]] = {}
        for r in self.events():
            # [served, followed, decayed success, decayed harm, imported served, imported followed]
            a = agg.setdefault((r["skill"], r["version"]), [0, 0, 0.0, 0.0, 0, 0])
            if r.get("source") in IMPORTED_SOURCES:
                a[4 if r["event"] == "served" else 5] += 1
                continue
            if r["event"] == "served":
                a[0] += 1
                continue
            a[1] += 1  # followed and followed_error both count as a (heuristic) follow
            # Future-dated events (clock skew between hosts) are capped at weight 1.0.
            age = max((now - _utc(r["at"])).total_seconds() / 86400.0, 0.0)
            w = 0.5 ** (age / HALF_LIFE_DAYS)
            if r["effect"] == "success":
                a[2] += w
            elif r["effect"] == "harm":
                a[3] += w
        out = []
        for (skill, version), (served, followed, succ, harm, imp_s, imp_f) in agg.items():
            net = succ - HARM_WEIGHT * harm
            out.append(
                Score(
                    skill=skill,
                    version=version,
                    served=int(served),
                    followed=int(followed),
                    imported_served=int(imp_s),
                    imported_followed=int(imp_f),
                    follow_rate=(followed / served) if served else 0.0,
                    net=net,
                    net_effect_sign="+" if net > 1e-9 else "-" if net < -1e-9 else "0",
                    anti_pattern=followed >= MIN_FOLLOWS_ANTI and harm > succ,
                    flag=(
                        "served-never-followed"
                        if served >= MIN_SERVED_NEVER_FOLLOWED and followed == 0
                        else None
                    ),
                )
            )
        out.sort(key=lambda s: (-s.net, s.key))
        return [Score(**{**s.__dict__, "rank": i}) for i, s in enumerate(out, 1)]

    def score(self, skill: str, version: str, as_of: datetime | str | None = None) -> Score | None:
        for s in self.scores(as_of):
            if s.skill == skill and s.version == str(version):
                return s
        return None

    def top(self, as_of: datetime | str | None = None, limit: int = 20) -> list[Score]:
        return self.scores(as_of)[:limit]
