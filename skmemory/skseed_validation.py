"""Write-time SKSeed truth-check for the memory store flow.

This is the STORE / write-flow SKSeed gate (coordination card 9b72c6c2). It is
deliberately distinct from any promotion-time verification: it runs when a
memory is first written (``MemoryStore.snapshot`` -> MCP ``memory_store``) and
annotates the memory with an *advisory* ``truth_score`` derived from the SKSeed
collider, plus any contradictions found against existing memories.

Design contract (per the card):
    * Config flag ``skseed.auto_validate`` (default ``False``) gates it. Nothing
      runs unless it is turned on, so the default write path pays zero cost.
    * When enabled, memories get a ``truth_score`` field in their metadata.
    * Contradictions with existing memories are flagged (best-effort).

Hard rules:
    * **Soft import** - ``skseed`` is optional. If it is not installed we
      no-op. skmemory keeps working standalone.
    * **Advisory / fail-open** - this never blocks a write. A validation error
      (or a missing ``skseed``) annotates nothing and lets the store proceed.
      It only tags/flags; it does not reject.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

logger = logging.getLogger("skmemory.skseed_validation")

# ── Soft import: skmemory works standalone without skseed ──────────────
# The logic kernel lives in the standalone `skseed` package. If it is not
# installed we fail open - write-time validation simply becomes a no-op.
try:
    from skseed import Collider as _Collider  # type: ignore

    _SKSEED_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    _Collider = None  # type: ignore[assignment,misc]
    _SKSEED_AVAILABLE = False

if TYPE_CHECKING:
    from .models import Memory
    from .store import MemoryStore

# Truthy env values for SKMEMORY_SKSEED_AUTO_VALIDATE.
_TRUTHY = {"1", "true", "yes", "on"}


def skseed_available() -> bool:
    """Return True when the optional ``skseed`` package is importable."""
    return _SKSEED_AVAILABLE


def _env_flag(name: str) -> bool | None:
    """Read a tri-state boolean env var (unset -> None)."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUTHY


def resolve_auto_validate() -> bool:
    """Resolve the ``skseed.auto_validate`` flag.

    Precedence: env ``SKMEMORY_SKSEED_AUTO_VALIDATE`` > config
    ``skseed.auto_validate`` > ``False``. Fail-open: any error resolving the
    config yields ``False`` (validation off).
    """
    env = _env_flag("SKMEMORY_SKSEED_AUTO_VALIDATE")
    if env is not None:
        return env
    try:
        from .config import load_config

        cfg = load_config()
        if cfg is not None:
            return bool(cfg.skseed.auto_validate)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("skseed auto_validate config resolve failed: %s", exc)
    return False


def _build_collider() -> Any | None:
    """Construct a SKSeed collider, or None when skseed is unavailable.

    No LLM callback is wired by default, so the collider runs offline and
    returns an *ungraded* advisory score (no network, no cost). A caller (or a
    future config hook) can inject a configured collider via
    :func:`annotate_truth_score`'s ``collider`` argument.
    """
    if not _SKSEED_AVAILABLE or _Collider is None:
        return None
    try:
        return _Collider()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("skseed collider init failed (fail-open): %s", exc)
        return None


def _grade_str(grade: Any) -> str:
    """Normalise a TruthGrade enum (or str) to a plain string."""
    return str(getattr(grade, "value", grade))


def _find_contradictions(
    memory: Memory,
    store: MemoryStore | None,
    result: Any,
    collider: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Best-effort contradiction flagging against existing memories.

    Only attempts real cross-referencing when the collider produced invariants
    (i.e. an LLM callback is wired). Without that, contradiction detection is
    not meaningful, so we return an empty list rather than burn cycles.
    """
    invariants = list(getattr(result, "invariants", []) or [])
    if not invariants or store is None:
        return []

    # Pull a few semantically-similar existing memories to cross-reference.
    try:
        neighbours = store.search(memory.content[:512], limit=limit)
    except Exception as exc:  # pragma: no cover - search is optional
        logger.debug("skseed contradiction search skipped: %s", exc)
        return []

    contradictions: list[dict[str, Any]] = []
    for other in neighbours:
        if getattr(other, "id", None) == getattr(memory, "id", None):
            continue
        try:
            other_result = collider.truth_score_memory(other.content)
            cross = collider.cross_reference([result, other_result])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("skseed cross_reference failed: %s", exc)
            continue
        conflicts = cross.get("conflicts") if isinstance(cross, dict) else None
        if conflicts:
            contradictions.append(
                {
                    "memory_id": getattr(other, "id", None),
                    "title": getattr(other, "title", ""),
                    "conflicts": conflicts,
                }
            )
    return contradictions


def annotate_truth_score(
    memory: Memory,
    store: MemoryStore | None = None,
    *,
    collider: Any | None = None,
    contradiction_limit: int = 5,
) -> dict[str, Any] | None:
    """Run the SKSeed truth-check on *memory* and annotate its metadata.

    This is advisory and fail-open. On success it writes into
    ``memory.metadata``:

        * ``truth_score`` - the collider coherence score (0.0-1.0).
        * ``skseed`` - a detail block: ``truth_score``, ``truth_grade``,
          ``validated_by``, and (when found) ``contradictions``.

    Args:
        memory: The memory about to be written. Mutated in place.
        store: Optional store, used to find neighbours for contradiction
            flagging.
        collider: Optional pre-built collider (test / LLM-wired injection).
            Defaults to an offline collider.
        contradiction_limit: Max existing memories to cross-reference.

    Returns:
        The detail block dict on success, else ``None`` (skseed absent or the
        check errored - in both cases the write proceeds unannotated).
    """
    active = collider if collider is not None else _build_collider()
    if active is None:
        # skseed not installed (or collider init failed) -> fail open, no-op.
        return None

    try:
        result = active.truth_score_memory(memory.content)
        truth_score = round(float(getattr(result, "coherence_score", 0.0) or 0.0), 4)
        detail: dict[str, Any] = {
            "truth_score": truth_score,
            "truth_grade": _grade_str(getattr(result, "truth_grade", "ungraded")),
            "validated_by": "skseed",
        }

        contradictions = _find_contradictions(
            memory, store, result, active, contradiction_limit
        )
        if contradictions:
            detail["contradictions"] = contradictions

        # Surface truth_score at the top level per the card, keep the full
        # block under a namespaced key.
        memory.metadata["truth_score"] = truth_score
        memory.metadata["skseed"] = detail
        return detail
    except Exception as exc:
        # Advisory only - a failed truth-check must never block the write.
        logger.warning("skseed truth-check failed (fail-open): %s", exc)
        return None
