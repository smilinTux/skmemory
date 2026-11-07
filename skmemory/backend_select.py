"""One place that decides which derived backends a process may use.

Two silent-degradation defects lived in the absence of this module (measured on
a fleet node, 2026-11-06, no agent selector declared and
``SKMEMORY_VECTOR_BACKEND=pgvector`` in the environment):

* The CLI never read ``SKMEMORY_VECTOR_BACKEND`` at all. It tried the retired
  embedded vector store, then Qdrant, and printed a "trying Qdrant" warning to
  stderr. A different vector store is a different set of answers.
* The Postgres DSN lookup (``SKMEMORY_PG_DSN`` env, then the yaml) was copied
  into four call sites in ``cli.py`` and a fifth in ``mcp_server.py``.

The contract implemented here:

1. ``SKMEMORY_VECTOR_BACKEND`` names a backend explicitly (``pgvector`` or
   ``none``). That backend is used or the operation fails with
   :class:`BackendInitError` carrying the real reason. There is no fallback to
   another store.
2. With no explicit request, the config's ``backends_enabled`` list decides.
   Declaring nothing means no vector backend.
3. ChromaDB (SKMEM-P5-01), Qdrant/skvector and FalkorDB/skgraph (SKMEM-P5-02)
   are retired: the graph is AGE only and the vector tier is pgvector only.
   Naming one in ``SKMEMORY_VECTOR_BACKEND`` fails fast with its retirement
   message (non-zero exit on the CLI, red health). A stale entry in a yaml is
   NOT fatal: it is dropped with a one-time warning in ``config.py`` so old
   node configs cannot take down session hooks.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

VECTOR_BACKEND_ENV = "SKMEMORY_VECTOR_BACKEND"
PG_DSN_ENV = "SKMEMORY_PG_DSN"

CHROMA_RETIRED_MESSAGE = (
    "ChromaDB has been retired; use pgvector (shared) "
    "or the SQLite FTS5 local arm. Unset SKMEMORY_VECTOR_BACKEND or set it to "
    "pgvector or none."
)

SKVECTOR_RETIRED_MESSAGE = (
    "Qdrant (skvector) has been retired; the vector tier is pgvector only. "
    "Unset SKMEMORY_VECTOR_BACKEND or set it to pgvector or none."
)

SKGRAPH_RETIRED_MESSAGE = (
    "FalkorDB (skgraph) has been retired; the graph is Apache AGE only. "
    "Unset SKMEMORY_VECTOR_BACKEND or set it to pgvector or none."
)

# retired alias -> message. Checked before the live aliases so a retired name
# can never be mistaken for an unknown typo (which would read as "guessing").
RETIRED_BACKENDS = {
    "chroma": CHROMA_RETIRED_MESSAGE,
    "chromadb": CHROMA_RETIRED_MESSAGE,
    "skvector": SKVECTOR_RETIRED_MESSAGE,
    "qdrant": SKVECTOR_RETIRED_MESSAGE,
    "skgraph": SKGRAPH_RETIRED_MESSAGE,
    "falkordb": SKGRAPH_RETIRED_MESSAGE,
    "falkor": SKGRAPH_RETIRED_MESSAGE,
}

# alias -> canonical name. "none" means "explicitly no vector backend".
_ALIASES = {
    "pgvector": "pgvector",
    "pg": "pgvector",
    "postgres": "pgvector",
    "none": "none",
    "off": "none",
    "disabled": "none",
    "flat": "none",
}


class BackendInitError(RuntimeError):
    """An explicitly requested backend could not be initialised."""


def requested_vector_backend(environment: Mapping[str, str] | None = None) -> str | None:
    """Return the canonical backend named by ``SKMEMORY_VECTOR_BACKEND``.

    Returns None when the variable is unset or blank (nothing was requested).
    An unrecognised value raises :class:`BackendInitError`: guessing what the
    operator meant is how a request for one store ends up served by another.
    """
    source = os.environ if environment is None else environment
    raw = (source.get(VECTOR_BACKEND_ENV) or "").strip().lower()
    if not raw:
        return None
    if raw in RETIRED_BACKENDS:
        raise BackendInitError(f"{VECTOR_BACKEND_ENV}={raw}: {RETIRED_BACKENDS[raw]}")
    try:
        return _ALIASES[raw]
    except KeyError:
        raise BackendInitError(
            f"{VECTOR_BACKEND_ENV}={raw!r} is not a known vector backend. "
            f"Use one of: {', '.join(sorted(set(_ALIASES.values())))}."
        ) from None


def resolve_pg_dsn(cfg: Any = None, environment: Mapping[str, str] | None = None) -> str | None:
    """Resolve the Postgres DSN: node-local env first, then the shared yaml.

    The agent home is one Syncthing folder, so a yaml DSN is a SHARED value
    that cannot carry a node IP; the per-node env is the only correct per-node
    lever and therefore wins.
    """
    source = os.environ if environment is None else environment
    env_dsn = source.get(PG_DSN_ENV)
    if env_dsn:
        return env_dsn
    yaml_dsn = getattr(cfg, "pgvector_dsn", None) if cfg is not None else None
    return yaml_dsn or None


_RETIRED_ENV_VARS = (
    "SKMEMORY_SKVECTOR_URL",
    "SKMEMORY_SKVECTOR_KEY",
    "SKMEMORY_SKVECTOR_EMBEDDING_MODEL",
    "SKMEMORY_SKVECTOR_VECTOR_DIM",
    "SKMEMORY_SKGRAPH_URL",
)
_retired_env_warned = False


def _warn_retired_env_once(source: Mapping[str, str]) -> None:
    """A stale URL variable is ignored with one warning, never fatal (hooks)."""
    global _retired_env_warned
    if _retired_env_warned:
        return
    stale = [name for name in _RETIRED_ENV_VARS if source.get(name)]
    if not stale:
        return
    _retired_env_warned = True
    logging.getLogger("skmemory.backend_select").warning(
        "%s is set but its backend is retired; ignoring it. %s %s",
        ", ".join(stale),
        SKVECTOR_RETIRED_MESSAGE,
        SKGRAPH_RETIRED_MESSAGE,
    )


def enabled_vector_backends(
    cfg: Any = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Return ``(pgvector, requested)``.

    When ``requested`` is not None the caller must NOT fall back if the named
    backend fails to initialise. ``requested == "none"`` means no vector backend.
    """
    _warn_retired_env_once(os.environ if environment is None else environment)
    requested = requested_vector_backend(environment)
    if requested is not None:
        return (requested == "pgvector", requested)
    enabled = list(getattr(cfg, "backends_enabled", None) or []) if cfg is not None else []
    return ("pgvector" in enabled, None)
