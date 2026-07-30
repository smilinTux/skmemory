"""skmemory operator-facet probe: the explain / observe / act contract.

This is the canonical operator contract for skmemory, the module the
`skmemory operator` CLI is built over and the exact shape Atlas's skmemory
adapter (`skcapstone/src/skcapstone/operator_seat/skmemory_adapter.py`) mirrors.
One operator, many apps: skmemory conforms by exposing the same three verbs the
fleet does.

The observe probes are REAL and injectable (tests never touch a live skmemory,
a real embedding backend, or skmem-pg):

  * ``EmbedServing``   the embedding backend health (mxbai-embed-large on the
    Ollama endpoint, default ``:11434``). This is the Level-1 vector backend the
    store leans on; when it is serving, semantic search and reconcile embeds work.
  * ``ReconcileFresh`` the age of the local SQLite working index (``index.db``),
    the derived-from-flat-JSON artifact the daily reconcile rebuilds. A stale
    index (older than the reconcile-max-age while the file exists) reads as a
    reconcile that has not run recently.

Every probe fails SAFE (reports healthy) rather than raising a false alarm when
skmemory is unreachable, matching the adapter's ``_default_probe`` posture: an
inability to probe never raises a false 'embed down' or 'reconcile stale' alarm.

The act verb maps the one reversible standard action (``restart_service``) onto
``systemctl --user restart <unit>`` through an injectable runner. ``reindex`` is
declared non-standard (a major, medium-blast index rebuild) and refuses at the
act verb: it is human-approval-only and escalates as MAJOR by construction.

Core-purity note: this module imports NO subapp (no skcapstone/skchat/skcomms).
It reads real signals with the stdlib only, so the operator CLI stays free of any
subapp import (identity, if ever needed here, would come from capauth).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

#: The two operator conditions, matching Atlas's skmemory_adapter and the manifest.
CONDITIONS = ["EmbedServing", "ReconcileFresh"]

#: The kinds skmemory exposes to the operator plane.
KINDS = ["embed", "reconcile"]

#: skmemory health conditions are health-type (they fire when status is False), so
#: they are NOT problem-when-true; the operator brief treats them correctly by
#: default. Metadata order mirrors the adapter's _ACTIONS byte-for-byte.
_ACTIONS = [
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the wedged skmemory service",
        "kedb_refs": [],
    },
    {
        "name": "reindex",
        "standard": False,
        "reversible": True,
        "blast_radius": "medium",
        "runbook": "rebuild the skmemory embedding index (major)",
        "kedb_refs": [],
    },
]

#: The embedding backend the store's Level-1 vector path leans on (Ollama-style
#: /api/tags reachability check). Overridable for tests / alt endpoints.
_EMBED_HEALTH_URL_DEFAULT = "http://localhost:11434/api/tags"

#: Reconcile freshness threshold: an index older than this while it exists reads
#: as a reconcile that has not run recently (the daily sync timer rebuilds it).
_RECONCILE_MAX_AGE_S = 48 * 3600


def _b(value: bool) -> str:
    return "True" if value else "False"


def _agent() -> str:
    """The active agent, for the per-agent memory index path."""
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    )


# --- pure probe logic (unit-tested directly) ---------------------------------


def _reconcile_fresh(index_age_s: Optional[float]) -> bool:
    """The reconcile-freshness rule: a known index older than the max age reads
    as stale. Unknown age (no index yet) fails SAFE (fresh)."""
    if index_age_s is None:
        return True
    return index_age_s <= _RECONCILE_MAX_AGE_S


# --- real signal readers (each fails safe = healthy) -------------------------


def _embed_health_url() -> str:
    return os.environ.get("SKMEMORY_EMBED_HEALTH", _EMBED_HEALTH_URL_DEFAULT)


def _index_path() -> str:
    """The per-agent SQLite working index (index.db), the reconcile artifact."""
    override = os.environ.get("SKMEMORY_INDEX_DB")
    if override:
        return override
    return str(
        Path.home()
        / ".skcapstone"
        / "agents"
        / _agent()
        / "memory"
        / "index.db"
    )


def _probe_embed_serving() -> bool:
    """Read the embedding backend health endpoint. Returns True when it serves.

    Fails SAFE: an unreachable backend reports serving (True) so a probe failure
    never raises a false 'embed down' alarm (matches the adapter's fail-safe).
    """
    try:
        import urllib.request

        with urllib.request.urlopen(_embed_health_url(), timeout=8) as r:  # noqa: S310
            return 200 <= getattr(r, "status", 200) < 400
    except Exception:
        return True


def _probe_index_age() -> Optional[float]:
    """Age in seconds of the local SQLite index, or None when no index file is
    found (fails safe: unknown age reads as fresh)."""
    try:
        import time

        p = Path(_index_path())
        if not p.is_file():
            return None
        return max(0.0, time.time() - p.stat().st_mtime)
    except Exception:
        return None


def _default_probe() -> dict:
    """Best-effort skmemory health from real signals. Fails SAFE (healthy) when
    skmemory is unreachable, so an inability to probe never raises a false alarm."""
    return {
        "embed_serving": _probe_embed_serving(),
        "reconcile_fresh": _reconcile_fresh(_probe_index_age()),
    }


# --- contract verbs ----------------------------------------------------------


def explain() -> dict:
    """skmemory's self-description in the operator-contract shape."""
    return {
        "kinds": list(KINDS),
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def observe(probe: Optional[Callable[[], dict]] = None) -> dict:
    """Read-only skmemory health snapshot in the operator-contract shape.

    ``probe`` is injectable so tests are hermetic; the default reads real signals
    and fails safe. The two conditions and their objects mirror Atlas's
    skmemory_observe byte-for-byte.
    """
    st = (probe or _default_probe)()
    return {
        "conditions": [
            {
                "type": "EmbedServing",
                "status": _b(bool(st.get("embed_serving", True))),
                "object": "embed-service",
            },
            {
                "type": "ReconcileFresh",
                "status": _b(bool(st.get("reconcile_fresh", True))),
                "object": "reconciler",
            },
        ]
    }


def _action_meta(action: str) -> Optional[dict]:
    for a in _ACTIONS:
        if a["name"] == action:
            return a
    return None


def _unit_for(action: str, agent: Optional[str] = None) -> Optional[str]:
    """The systemd unit a reversible standard action restarts."""
    if action == "restart_service":
        override = os.environ.get("SKMEMORY_UNIT")
        if override:
            return override
        return f"skmemory-sync@{agent or _agent()}.service"
    return None


def _default_runner(cmd) -> dict:
    """Run a systemd command, capturing the result. Never invoked under test."""
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def act(
    action: str,
    *,
    runner: Optional[Callable[[list], dict]] = None,
    agent: Optional[str] = None,
    unit: Optional[str] = None,
) -> dict:
    """Perform a reversible standard skmemory action, or refuse.

    ``restart_service`` (standard, reversible, low blast) runs
    ``systemctl --user restart <unit>`` through the injected ``runner`` (defaults
    to a real subprocess). ``reindex`` is declared non-standard (a major,
    medium-blast index rebuild) and is NOT performed here: it is
    human-approval-only and escalates as MAJOR by construction. An unknown action
    is refused.
    """
    meta = _action_meta(action)
    if meta is None:
        raise ValueError(f"unknown skmemory operator action {action!r}")
    if not meta.get("standard"):
        # reindex and any future non-standard action: refuse at the act verb.
        return {
            "action": action,
            "performed": False,
            "escalate": "MAJOR",
            "reason": (
                "non-standard: human-approval-only, escalates as MAJOR by "
                "construction (policy.classify_change) and never actuates here"
            ),
        }
    target_unit = unit or _unit_for(action, agent)
    if target_unit is None:  # pragma: no cover - standard actions always map
        raise ValueError(f"no systemd unit mapping for skmemory action {action!r}")
    cmd = ["systemctl", "--user", "restart", target_unit]
    result = (runner or _default_runner)(cmd)
    return {
        "action": action,
        "performed": True,
        "unit": target_unit,
        "command": cmd,
        "result": result,
    }


__all__ = [
    "CONDITIONS",
    "KINDS",
    "explain",
    "observe",
    "act",
]
