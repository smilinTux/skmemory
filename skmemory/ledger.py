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
``follow_rate`` is raw followed / served. A skill version is an anti-pattern
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 90.0
HARM_WEIGHT = 4.0
MIN_FOLLOWS_ANTI = 3
MIN_SERVED_NEVER_FOLLOWED = 3
EVENTS = frozenset({"served", "followed"})
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
    ) -> str:
        """Append one event to this host's file for the event's month. Returns the event id."""
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
        rec["id"] = event_id(*(rec[k] for k in _FIELDS))
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file(when).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._index([rec])
        return rec["id"]

    def events(self) -> list[dict]:
        """Every event from every host file, deduped by id, ordered by time."""
        seen: dict[str, dict] = {}
        for p in sorted(self.dir.glob("outcomes-*.jsonl")) if self.dir.is_dir() else []:
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    clean = {k: r[k] if k != "effect" else r.get("effect") for k in _FIELDS}
                    if clean["event"] not in EVENTS:
                        raise ValueError("bad event")
                    _utc(clean["at"])
                except (ValueError, KeyError, TypeError) as err:
                    logger.warning("ledger: skipping %s:%d (%s)", p.name, n, err)
                    continue
                clean["id"] = event_id(*(clean[k] for k in _FIELDS))
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
            # [served, followed, decayed success, decayed harm]
            a = agg.setdefault((r["skill"], r["version"]), [0, 0, 0.0, 0.0])
            if r["event"] == "served":
                a[0] += 1
                continue
            a[1] += 1
            age = max((now - _utc(r["at"])).total_seconds() / 86400.0, 0.0)
            w = 0.5 ** (age / HALF_LIFE_DAYS)
            if r["effect"] == "success":
                a[2] += w
            elif r["effect"] == "harm":
                a[3] += w
        out = []
        for (skill, version), (served, followed, succ, harm) in agg.items():
            net = succ - HARM_WEIGHT * harm
            out.append(
                Score(
                    skill=skill,
                    version=version,
                    served=int(served),
                    followed=int(followed),
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
