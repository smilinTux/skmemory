"""Engine selection: ``SKMEMORY_ENGINE`` picks who serves MemoryStore.

``python`` (the default, and what an unset variable means) is the original
implementation and is untouched. ``rust`` routes the read paths (search, recall,
list, history, reader_can_see, health) and the plain write path through
``skmem_core.Engine`` (the skmemory-rs PyO3 wheel). Everything the Rust snapshot
does not do (pgvector row, graph upsert, reweave, WAL, retrieval log, write-time
triage, decomposition, custom pre-write hooks) stays in Python, performed exactly
once by the store.

Fail-fast rules (never a silent fallback):

* an unknown value raises :class:`EngineConfigError`;
* ``rust`` with ``skmem_core`` not importable raises :class:`EngineConfigError`.

Scope is never passed to Rust: Rust derives the reader ceiling from the process
(``SKMEMORY_READER``) on every call, so the shim cannot widen it. The shim only
supplies the packaged audience config path through ``SKMEMORY_AUDIENCE_CONFIG``
when the operator has not set one (Rust fails closed to ``@public`` without it).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ENV = "SKMEMORY_ENGINE"
AUDIENCE_ENV = "SKMEMORY_AUDIENCE_CONFIG"
PYTHON = "python"
RUST = "rust"
VALID = (PYTHON, RUST)


class EngineConfigError(ValueError):
    """SKMEMORY_ENGINE is invalid, or names an engine that cannot be loaded."""


def selected() -> str:
    """The engine named by the environment: ``python`` (default) or ``rust``."""
    raw = os.environ.get(ENV)
    if raw is None or not raw.strip():
        return PYTHON
    value = raw.strip().lower()
    if value not in VALID:
        raise EngineConfigError(
            f"{ENV}={raw!r} is not a known engine; use one of: {', '.join(VALID)}"
        )
    return value


def is_rust() -> bool:
    return selected() == RUST


def rust_module() -> Any:
    """Import ``skmem_core`` or fail fast. Only called when the engine is ``rust``."""
    try:
        import skmem_core
    except ImportError as exc:
        raise EngineConfigError(
            f"{ENV}=rust but skmem_core is not importable ({exc}); "
            "build the skmemory-rs wheel (maturin) or unset the variable"
        ) from exc
    if not hasattr(skmem_core, "Engine"):
        raise EngineConfigError(f"{ENV}=rust but skmem_core has no Engine")
    return skmem_core


def packaged_audience_config() -> Path:
    return Path(__file__).parent / "data" / "audience_config.json"


def ensure_audience_config() -> None:
    """Point Rust at the packaged audience config unless the operator chose one."""
    if not os.environ.get(AUDIENCE_ENV):
        os.environ[AUDIENCE_ENV] = str(packaged_audience_config())


def ceiling() -> str:
    """The process reader ceiling, as an engine cache key (never widens anything)."""
    return os.environ.get("SKMEMORY_READER", "")


class RustEngines:
    """One lazily opened ``skmem_core.Engine`` per (memory dir, ceiling).

    A process under a reader ceiling opens its engine read-only, so the cache
    key includes the ceiling: changing ``SKMEMORY_READER`` reopens rather than
    reusing an owner engine.
    """

    def __init__(self) -> None:
        self._engines: dict[tuple[str, str, bool], Any] = {}

    def get(self, memory_dir: str | Path, *, pg: bool = False) -> Any:
        mod = rust_module()
        ensure_audience_config()
        key = (str(memory_dir), ceiling(), pg)
        eng = self._engines.get(key)
        if eng is None:
            eng = mod.Engine(str(memory_dir), pg=pg)
            self._engines[key] = eng
        return eng
