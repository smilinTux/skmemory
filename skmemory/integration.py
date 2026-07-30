"""skmemory ⇄ skcapstone — optional integration adapter.

skmemory runs fully standalone.  When the ``skcapstone`` package is installed
(and the operator has not forced standalone mode with ``SK_STANDALONE=1``),
this adapter routes alerts through skcapstone's shared **sk-alert** bus and
registers skmemory's promotion sweep with the fleet **skscheduler**, so the
whole sk* mesh sees one alert stream and one scheduler.  When skcapstone is
absent, every call degrades to skmemory's native behaviour (structured logging
+ the in-process ``PromotionScheduler`` / systemd ``skmemory-sync@`` timer).

This is the *default-on-by-presence* pattern from
``skcapstone/docs/ADR-optional-integration-backbone.md`` — nothing here is a
hard dependency; ``skcapstone`` lives in the optional ``[skcapstone]`` extra.

Public API:
    is_present()                  -> bool
    alert(topic, payload, level)  -> bool   (True iff sent via sk-alert)
    ensure_schedule(interval_hours) -> bool (True iff registered with skscheduler)
    unregister_schedule()         -> bool
    register_self(pid_file)       -> bool

Topic convention: ``skmemory.<severity>`` (severity ∈ info|warn|error|critical).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("skmemory.integration")

#: This service's name — used as the alert topic prefix and registry key.
SERVICE = "skmemory"

#: Fleet-scheduler job name for the promotion sweep.
SWEEP_JOB = "skmemory_sweep"

# Optional skcapstone SDK — resolved lazily on first use.
#
# Importing skcapstone eagerly at module load would pull it into
# ``sys.modules`` the instant this module is imported, which breaks the L0
# core-purity invariant: skmemory is a core package and importing it must never
# drag a higher-layer subapp (skcapstone) in as an import side effect.
# Deferring the import to first use keeps ``import skmemory`` free of skcapstone
# while preserving full integrated behaviour when skcapstone IS installed.
_UNRESOLVED: Any = object()
_sdk: Any = _UNRESOLVED


def _get_sdk() -> Any:
    """Return the skcapstone ``sdk`` module, or ``None`` when unavailable.

    The import is deferred to first call and then cached (``None`` is a valid,
    cached "unavailable" result), so merely importing this module never pulls
    skcapstone into ``sys.modules``.  Tests may still assign the module-level
    ``_sdk`` directly to force a value.
    """
    global _sdk
    if _sdk is _UNRESOLVED:
        try:
            from skcapstone import sdk as resolved
        except Exception:  # ImportError, or a broken partial install
            resolved = None
        _sdk = resolved
    return _sdk

#: severity → logging method name (native fallback)
_LOG_METHOD = {
    "info": "info",
    "warn": "warning",
    "error": "error",
    "critical": "critical",
}
_NOTIFY_LEVELS = frozenset({"warn", "error", "critical"})


def is_present() -> bool:
    """Return whether skcapstone integration should be used from this process.

    ``True`` only when the package imported, the operator has not set
    ``SK_STANDALONE``, and the SDK reports itself available.  Any failure is
    treated as "not present" so callers transparently use their native path.
    """
    if os.environ.get("SK_STANDALONE"):
        return False
    sdk = _get_sdk()
    if sdk is None:
        return False
    try:
        return bool(sdk.is_available())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("skcapstone present-check failed: %s", exc)
        return False


def alert(event: str, payload: dict[str, Any], level: str = "info") -> bool:
    """Emit an alert: via skcapstone sk-alert when present, else local log.

    The published topic follows the ecosystem convention ``skmemory.<severity>``
    (so ``skcapstone alerts`` — which subscribes to ``*.error`` / ``*.critical``
    / ``*.warn`` — surfaces it). The semantic *event* name is carried in the
    payload's ``event`` field rather than the topic, so routing stays
    severity-based while detail is preserved.

    Args:
        event: Semantic event name (e.g. ``"sweep_failed"``). Stored in the
            payload as ``event``.
        payload: JSON-serialisable event body.
        level: ``info | warn | error | critical``.

    Returns:
        ``True`` if published to the shared bus, ``False`` if it fell back to
        local logging (which always also happens at the matching level).
    """
    body = {"event": event, **dict(payload)}
    if is_present():
        try:
            return bool(
                _get_sdk().alert(
                    f"{SERVICE}.{level}",
                    body,
                    level=level,
                    notify=level in _NOTIFY_LEVELS,
                )
            )
        except Exception as exc:
            logger.warning("sk-alert publish failed, logging locally: %s", exc)

    # native fallback — structured log at the matching level
    method = getattr(logger, _LOG_METHOD.get(level, "info"))
    method("[%s.%s] %s", SERVICE, level, body)
    return False


def ensure_schedule(interval_hours: float = 6.0) -> bool:
    """Register the promotion sweep with the fleet scheduler, if present.

    Writes a ``jobs.d/skmemory_sweep.yaml`` drop-in that runs ``skmemory
    sweep`` every *interval_hours*, so the skcapstone daemon owns the cadence
    (with central retry/notify).  Idempotent — safe to call on every startup.

    Args:
        interval_hours: Sweep cadence in hours (matches
            ``PromotionScheduler`` default of 6h).

    Returns:
        ``True`` if registered with skscheduler; ``False`` when skcapstone is
        absent and the caller should rely on its native scheduler.
    """
    if not is_present():
        return False
    try:
        _get_sdk().register_job(
            {
                "name": SWEEP_JOB,
                "type": "shell",
                "command": "skmemory sweep",
                "every": f"{int(interval_hours * 3600)}s",
                "timeout": 1800,
                "notify": "on_failure",
                "notify_level": "error",
            }
        )
        logger.info(
            "Registered '%s' with skcapstone scheduler (every %.1fh).", SWEEP_JOB, interval_hours
        )
        return True
    except Exception as exc:
        logger.warning("ensure_schedule failed (using native): %s", exc)
        return False


def unregister_schedule() -> bool:
    """Remove the promotion-sweep drop-in from the fleet scheduler."""
    sdk = _get_sdk()
    if sdk is None:
        return False
    try:
        return bool(sdk.unregister_job(SWEEP_JOB))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("unregister_schedule failed: %s", exc)
        return False


def register_self(pid_file: str | None = None) -> bool:
    """Advertise skmemory to skcapstone's discovery registry, if present.

    Args:
        pid_file: Optional pid-file path used as a liveness signal.

    Returns:
        ``True`` if registered, ``False`` otherwise.
    """
    if not is_present():
        return False
    try:
        _get_sdk().register_service(
            SERVICE,
            pid_file=pid_file or str(Path("~/.skmemory/daemon.pid").expanduser()),
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("register_self failed: %s", exc)
        return False
