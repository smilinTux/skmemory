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
import threading
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
    """The audience config Python itself resolves (the packaged one unless patched)."""
    from . import audience

    return Path(audience._DEFAULT_CONFIG_PATH)


_set_by_shim: str | None = None


def ensure_audience_config() -> None:
    """Point Rust at the audience config Python uses, unless the operator chose one.

    A value the operator exported is never touched. A value this shim set earlier is
    refreshed, so Rust follows Python if the default path changes.
    """
    global _set_by_shim
    cur = os.environ.get(AUDIENCE_ENV)
    if not cur or cur == _set_by_shim:
        _set_by_shim = str(packaged_audience_config())
        os.environ[AUDIENCE_ENV] = _set_by_shim


def rust_reader(reader: Any) -> str | None:
    """The per-call reader for Rust. A non-string is ``@public`` (as ``resolve_reader``).

    Never widens: ``None`` stays ``None`` (the process ceiling decides), a string
    passes through unchanged and Rust fails closed on what it cannot parse.
    """
    if reader is None or isinstance(reader, str):
        return reader
    return "@public"


def ceiling() -> str:
    """The process reader ceiling, as an engine cache key (never widens anything)."""
    return os.environ.get("SKMEMORY_READER", "")


class RustEngines:
    """One lazily opened ``skmem_core.Engine`` per (memory dir, ceiling, pg, audience config).

    Process-wide on purpose: Python's sqlite3 and the Rust engine each bundle their
    own SQLite, and POSIX advisory locks are per process, so closing the Rust
    connection (a dropped Engine) releases locks the Python connection to the same
    index.db holds, which surfaces as ``disk I/O error``. An engine therefore lives
    as long as the process.

    A process under a reader ceiling opens its engine read-only, so the cache
    key includes the ceiling: changing ``SKMEMORY_READER`` reopens rather than
    reusing an owner engine.
    """

    _engines: dict[tuple[str, str, bool, str], Any] = {}
    _lock = threading.Lock()

    def get(self, memory_dir: str | Path, *, pg: bool = False) -> Any:
        mod = rust_module()
        ensure_audience_config()
        cfg = str(packaged_audience_config())
        key = (str(memory_dir), ceiling(), pg, cfg)
        with self._lock:
            eng = self._engines.get(key)
            if eng is None:
                # The per-call reader resolves under the engine's config, the ceiling under the
                # env one: give both the same file so neither can disagree with Python.
                eng = mod.Engine(str(memory_dir), cfg, pg=pg)
                self._engines[key] = eng
        return eng
