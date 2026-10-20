"""Repair flat memory files that skmemory's own model cannot load.

Why this exists
---------------
Flat JSON under ``~/.skcapstone/agents/<agent>/memory/{short,mid,long}-term/``
is the source of truth, but nothing stops a producer writing a file that
``Memory`` cannot validate, because a producer can dump raw JSON without going
through the model at all. When that happens the memory is not corrupt and not
missing: it is simply unreadable through every normal path, which is a quieter
and more confusing kind of loss than an absent file.

Measured on lumina 2026-10-17: 17 of 45,340 live flat files (0.04%) have no
``title``, which ``Memory`` requires. Twelve came from ``telegram-bridge`` and
the rest from cron watchers. The same 17 rows were mirrored faithfully into
skmem-pg, where they made every search that touched one of them raise until
``PGVectorBackend._rows_to_memories`` started skipping them.

Scope, stated plainly: this module repairs the DATA. It cannot fix the
producers, which live in other repos, and it does not pretend to. Repairing
without naming the producer would just let the same files reappear, so
:func:`scan_unloadable` reports the ``source`` of every file it finds and the
caller is expected to chase those upstream.

What a repair is allowed to do
------------------------------
Derive the missing ``title`` from the content the file already has, and change
nothing else. It never invents content, never drops a field, never merges, and
never touches a file it can already load. Writes go through
:func:`skmemory.atomic_io.atomic_write_text` (temp file, fsync, atomic rename),
because a non-durable flat write is what caused the 2026-10-16 SEV-1 in the
first place (see ``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` §3.1).

Dry run is the default. A repair that runs before anyone has looked at what it
would do is not a repair.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

#: Tier directories that hold live memories. ``archive/`` is deliberately out
#: of scope: it is cold storage, not something a read path loads.
LIVE_TIERS = ("short-term", "mid-term", "long-term")

#: Fields ``Memory`` requires. A file missing either cannot be loaded at all.
REQUIRED_FIELDS = ("title", "content")

#: Longest derived title. Long enough to be recognisable in a listing, short
#: enough not to duplicate the content field.
MAX_DERIVED_TITLE = 80

#: Legacy key some producers used for the memory id. ``Memory`` reads ``id``,
#: so a file carrying only this one gets a FRESHLY MINTED id every time it is
#: loaded. See :func:`normalise_legacy_id` for why that matters more than the
#: missing title does.
LEGACY_ID_KEY = "memory_id"


@dataclass(frozen=True)
class UnloadableFile:
    """One flat file that ``Memory`` cannot validate."""

    path: Path
    missing: tuple[str, ...]
    source: str = ""
    memory_id: str = ""
    repairable: bool = False
    reason: str = ""
    #: True when the file carries only the legacy ``memory_id`` key, so the
    #: model would mint a new id for it on every load.
    legacy_id_only: bool = False


@dataclass
class RepairReport:
    """What a repair pass found and what it did.

    ``dry_run`` is carried on the report so a caller can never mistake a
    preview for an applied change, which is the whole point of defaulting to
    preview.
    """

    dry_run: bool = True
    scanned: int = 0
    unloadable: list[UnloadableFile] = field(default_factory=list)
    repaired: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def repairable_count(self) -> int:
        return sum(1 for item in self.unloadable if item.repairable)

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "APPLIED"
        return (
            f"[{mode}] scanned {self.scanned}, unloadable {len(self.unloadable)}, "
            f"repairable {self.repairable_count}, repaired {len(self.repaired)}, "
            f"skipped {len(self.skipped)}, failed {len(self.failed)}"
        )


def derive_title(content: str, *, max_length: int = MAX_DERIVED_TITLE) -> str:
    """A title taken from the content's own first line.

    Deterministic and lossless with respect to content: it only ever copies a
    prefix of text already in the file, so a repaired title can never assert
    something the memory does not say. Returns an empty string when there is
    no usable content, which marks the file as NOT repairable rather than
    inventing a placeholder.
    """
    if not content or not content.strip():
        return ""
    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    if not first_line:
        return ""
    if len(first_line) <= max_length:
        return first_line
    # Cut on a word boundary when there is one reasonably near the limit.
    cut = first_line[:max_length]
    space = cut.rfind(" ")
    if space >= max_length // 2:
        cut = cut[:space]
    return cut.rstrip(" .,;:-")


def normalise_legacy_id(obj: dict) -> bool:
    """Move a legacy ``memory_id`` to the canonical ``id``. True if changed.

    This matters more than the missing title, and it is the subtler bug of the
    two. ``Memory`` reads ``id``; a file carrying only ``memory_id`` therefore
    gets a BRAND NEW uuid minted every time it is loaded, so the memory has no
    stable identity through the model at all. Saving such a file to the derived
    Postgres cache INSERTS A DUPLICATE rather than updating the row it came
    from, because the row it came from is keyed on the original id.

    Found the hard way: refreshing 17 repaired files into skmem-pg took lumina
    from 20,299 rows to 20,316 and left all 17 original rows untouched. The
    duplicates were identified by content and source and removed, and the count
    is back to 20,299.

    The id is already in the file, under the wrong key, so moving it across is
    content-preserving: nothing is invented.

    The redundant legacy key is now REMOVED, reversing the earlier decision to
    leave it in place. Leaving it was not neutral, it was load-bearing for a
    perpetual data-loss loop:

      * skcapstone's legacy ``MemoryEntry`` model has no ``title`` field and
        keys identity as ``memory_id``. Round-tripping a record through it
        silently drops both ``id`` and ``title`` (pydantic ``extra="ignore"``).
      * ``memory_engine._load_entry`` skips a unified record only when ``id``
        is present AND ``memory_id`` is absent. A file carrying BOTH keys, the
        exact shape this function used to produce, slips past that guard.
      * The hourly promotion sweep's compress pass then re-saves it through
        that model, stripping the title again.

    Observed 2026-10-20: a file repaired three days earlier was back to
    title-less, rewritten on the hour, every hour. Dropping the redundant key
    is what makes a repaired file legible as a unified record and therefore
    skipped by the legacy writer.

    A legacy value that DISAGREES with an existing ``id`` is left alone. Two
    different identifiers is a conflict, not redundancy, and silently dropping
    one of them would be the same class of mistake as the loop above.
    """
    legacy = obj.get(LEGACY_ID_KEY)
    if not isinstance(legacy, str) or not legacy.strip():
        return False
    legacy = legacy.strip()

    existing = obj.get("id")
    if not existing:
        obj["id"] = legacy
        del obj[LEGACY_ID_KEY]
        return True
    if str(existing).strip() == legacy:
        del obj[LEGACY_ID_KEY]
        return True
    # Conflicting identifiers: preserve both and let a human decide.
    return False


def _tier_dirs(memory_dir: Path) -> list[Path]:
    return [memory_dir / tier for tier in LIVE_TIERS if (memory_dir / tier).is_dir()]


def scan_unloadable(memory_dir: Path | str) -> RepairReport:
    """Find every live flat file that ``Memory`` cannot validate.

    Reports the ``source`` of each one, because the data repair is only half
    the job: a producer that wrote one title-less file will write another.
    """
    memory_dir = Path(memory_dir).expanduser()
    report = RepairReport(dry_run=True)

    for tier_dir in _tier_dirs(memory_dir):
        for path in sorted(tier_dir.glob("*.json")):
            report.scanned += 1
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                report.unloadable.append(
                    UnloadableFile(
                        path=path,
                        missing=("<unparseable>",),
                        repairable=False,
                        reason=f"not valid JSON: {exc}",
                    )
                )
                continue
            if not isinstance(obj, dict):
                report.unloadable.append(
                    UnloadableFile(
                        path=path,
                        missing=("<not-an-object>",),
                        repairable=False,
                        reason="top level is not a JSON object",
                    )
                )
                continue

            missing = tuple(f for f in REQUIRED_FIELDS if f not in obj)
            if not missing:
                continue

            # Only a missing title is derivable, and only from real content.
            derivable = missing == ("title",) and bool(derive_title(obj.get("content") or ""))
            report.unloadable.append(
                UnloadableFile(
                    path=path,
                    missing=missing,
                    source=str(obj.get("source") or ""),
                    memory_id=str(obj.get("id") or obj.get(LEGACY_ID_KEY) or ""),
                    legacy_id_only=not obj.get("id") and bool(obj.get(LEGACY_ID_KEY)),
                    repairable=derivable,
                    reason=""
                    if derivable
                    else f"cannot derive {', '.join(missing)} from this file",
                )
            )

    return report


def repair_missing_titles(memory_dir: Path | str, *, dry_run: bool = True) -> RepairReport:
    """Backfill a derived ``title`` on files that lack only that field.

    Defaults to a dry run: nothing is written unless ``dry_run=False``. Only
    the ``title`` key is added; every other field is written back byte-for-byte
    as it was parsed. Idempotent, because a file that now has a title is no
    longer unloadable and is not visited again.
    """
    report = scan_unloadable(memory_dir)
    report.dry_run = dry_run

    for item in report.unloadable:
        if not item.repairable:
            report.skipped.append((item.path, item.reason))
            continue
        try:
            obj = json.loads(item.path.read_text(encoding="utf-8"))
            title = derive_title(obj.get("content") or "")
            if not title:
                report.skipped.append((item.path, "content yielded no usable title"))
                continue
            obj["title"] = title
            # Same pass, because a file repaired WITHOUT this still cannot
            # round-trip to its own row in the derived cache.
            normalise_legacy_id(obj)
            if dry_run:
                report.repaired.append(item.path)
                continue
            atomic_write_text(
                item.path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
            )
            report.repaired.append(item.path)
            logger.info("repaired missing title on %s -> %r", item.path.name, title)
        except Exception as exc:
            # A failed durable write means the file was NOT repaired. Record it
            # rather than letting it look like a success.
            report.failed.append((item.path, str(exc)))
            logger.warning("could not repair %s: %s", item.path, exc)

    return report

def strip_legacy_ids(memory_dir: Path | str, *, dry_run: bool = True) -> RepairReport:
    """Remove a redundant legacy ``memory_id`` from every live flat file.

    ``repair_missing_titles`` only ever visits files that ``Memory`` cannot
    validate. A file repaired by an EARLIER run already has its title, so it is
    no longer unloadable and that pass never looks at it again, which leaves the
    redundant ``memory_id`` key sitting in it. That key is what lets
    skcapstone's legacy ``MemoryEntry`` writer pick the record up and strip the
    title straight back off (see :func:`normalise_legacy_id`).

    So this is a separate sweep over ALL live files rather than an extension of
    the repair pass: the files that most need it are precisely the ones that
    look healthy.

    Files whose ``memory_id`` disagrees with their ``id`` are reported as
    skipped, never rewritten.
    """
    memory_dir = Path(memory_dir).expanduser()
    report = RepairReport(dry_run=dry_run)

    for tier_dir in _tier_dirs(memory_dir):
        for path in sorted(tier_dir.glob("*.json")):
            report.scanned += 1
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                # Not this function's job: scan_unloadable reports these.
                continue
            if not isinstance(obj, dict) or LEGACY_ID_KEY not in obj:
                continue

            legacy = obj.get(LEGACY_ID_KEY)
            existing = obj.get("id")
            if (
                isinstance(legacy, str)
                and isinstance(existing, str)
                and legacy.strip()
                and existing.strip()
                and legacy.strip() != existing.strip()
            ):
                report.skipped.append(
                    (path, f"conflicting ids: id={existing!r} {LEGACY_ID_KEY}={legacy!r}")
                )
                continue

            if not normalise_legacy_id(obj):
                continue
            if dry_run:
                report.repaired.append(path)
                continue
            try:
                atomic_write_text(
                    path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
                )
                report.repaired.append(path)
                logger.info("stripped redundant %s from %s", LEGACY_ID_KEY, path.name)
            except Exception as exc:
                report.failed.append((path, str(exc)))
                logger.warning("could not strip %s from %s: %s", LEGACY_ID_KEY, path, exc)

    return report
