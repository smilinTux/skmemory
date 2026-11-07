"""Time tick for intentions: due time intentions fire into sk-alert (SKMEM-P2-03).

Design decisions (all load-bearing):

* **Delivery order: deliver first, then record the fire.** The alert is sent
  through ``sk-alert`` and only when that exits 0 is the state change applied
  (via :meth:`IntentionStore.apply`, the only state-change path). A crash
  between the two leaves the intention due, so the next tick delivers it
  again: an alert is never lost, a crash causes at most a duplicate. The
  opposite order (fire, then deliver) could mark an intention fired and lose
  the alert, which is the failure we refuse.
* **Delivery failure** (non-zero exit, missing command, timeout) changes
  nothing: a pending or armed intention stays eligible and is retried on the
  next tick. It is logged and counted in the result.
* **Multi-host rule: one configured tick host.** The flat dir replicates, so
  every host sees the same due intentions. Only the host whose short hostname
  equals ``SKMEMORY_TICK_HOST`` (or ``--tick-host``) runs the tick; any other
  host is a no-op. With no tick host configured the tick refuses unless the
  caller passes ``any_host=True`` (single-host nodes), so an unconfigured
  fleet cannot double-deliver by accident.
* **Alert text is only the intention title and the origin memory id** plus a
  resolve hint. No memory content, no origin memory title, no scope or terms:
  alert channels may be shared. (The card asked for the origin memory title;
  that is deliberately not read, since a memory title can be private.)
* A host-local, non-blocking flock serialises two ticks on one host.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import socket
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .intentions import Intention, IntentionStore

try:  # POSIX only
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ALERT_GROUP = "intentions"
ALERT_CMD_ENV = "SKMEMORY_ALERT_CMD"
TICK_HOST_ENV = "SKMEMORY_TICK_HOST"
_MAX_TITLE = 200
DEFAULT_MAX_PER_TICK = 5

Deliver = Callable[[str], bool]


@dataclass
class TickResult:
    """What one tick did. ``skipped`` is set when the tick did not run at all."""

    skipped: str | None = None
    fired: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    remaining: int = 0  # due but not delivered this tick (cap hit or stopped on failure)


def _clean(text: str, limit: int = _MAX_TITLE) -> str:
    """One line, no control characters, bounded length."""
    flat = "".join(c if c.isprintable() else " " for c in text)
    flat = " ".join(flat.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def alert_text(i: Intention) -> str:
    """Title + origin memory id + one-line resolve hint. Nothing else leaves the host."""
    origin = i.origin_memory_id or "none"
    return f"Intention due: {_clean(i.title)} (origin: {_clean(origin, 128)}). Resolve: skmemory intentions done {i.id}"


def sk_alert_deliver(text: str, *, timeout: float = 30.0) -> bool:
    """Run ``sk-alert -g intentions ... TEXT``; True only on exit 0."""
    argv = shlex.split(os.environ.get(ALERT_CMD_ENV) or "sk-alert")
    argv += ["-g", ALERT_GROUP, "--title", "skmemory intention due", text]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("sk-alert delivery failed to run: %s", e)
        return False
    if proc.returncode != 0:
        logger.warning(
            "sk-alert exited %s: %s", proc.returncode, (proc.stderr or "").strip()[:200]
        )
        return False
    return True


def is_due(i: Intention, now: datetime) -> bool:
    return (
        i.kind == "time"
        and i.state in ("pending", "armed")
        and i.due_at is not None
        and i.due_at <= now
        and (i.snooze_until is None or i.snooze_until <= now)
    )


def due_intentions(store: IntentionStore, now: datetime) -> list[Intention]:
    """Due time intentions, judged from the flat files (truth), oldest due first."""
    out: list[Intention] = []
    for cand in store.list():
        cur = store.get(cand.id)
        if cur is not None and is_due(cur, now):
            out.append(cur)
    return sorted(out, key=lambda i: (i.due_at, i.id))


def _short_host() -> str:
    return socket.gethostname().split(".")[0].casefold()


@contextlib.contextmanager
def _tick_lock(store: IntentionStore) -> Iterator[bool]:
    """Non-blocking host-local flock. Yields False when another tick holds it."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    fd = None
    try:
        store._lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(store._lock_dir / "_tick.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
    except OSError as e:
        logger.warning("tick lock unavailable, proceeding unlocked: %s", e)
    try:
        yield True
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def run_tick(
    store: IntentionStore,
    *,
    now: datetime | None = None,
    deliver: Deliver | None = None,
    tick_host: str | None = None,
    any_host: bool = False,
    hostname: str | None = None,
    max_per_tick: int = DEFAULT_MAX_PER_TICK,
) -> TickResult:
    """Fire every due time intention once. See the module docstring for the rules."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    deliver = deliver or sk_alert_deliver
    host = (hostname or _short_host()).strip().split(".")[0].casefold()
    want = (tick_host or os.environ.get(TICK_HOST_ENV) or "").strip().split(".")[0].casefold()
    if want:
        if want != host:
            return TickResult(
                skipped=f"not the tick host (this host {host!r}, tick host {want!r})"
            )
    elif not any_host:
        return TickResult(
            skipped=f"no tick host configured: set {TICK_HOST_ENV} or pass --tick-host/--any-host"
        )

    res = TickResult()
    with _tick_lock(store) as got:
        if not got:
            res.skipped = "another tick is running on this host"
            return res
        store.reindex()  # merge synced replicas before judging what is due
        due = due_intentions(store, now)  # oldest due first
        batch, rest = due[: max(1, max_per_tick)], due[max(1, max_per_tick) :]
        for i in batch:
            if not deliver(alert_text(i)):
                # Stop on the first failure: sk-alert is down, retrying every item
                # would take N x timeout and overlap the next run.
                logger.warning("intention %s: alert not delivered, stopping tick", i.id)
                res.failed.append(i.id)
                res.remaining = len(due) - len(res.fired)
                return res
            after = store.apply(i.id, "fire", now=now)  # only after delivery succeeded
            if after is None:
                logger.warning("intention %s delivered but could not be recorded", i.id)
                res.failed.append(i.id)
            else:
                res.fired.append(i.id)
        res.remaining = len(rest)
        if rest and not deliver(f"{len(rest)} more due"):
            logger.warning("summary alert for %d more due not delivered", len(rest))
    return res
