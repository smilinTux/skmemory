"""One place that decides which derived backends a process may use.

Two silent-degradation defects lived in the absence of this module (measured on
192.168.0.41, 2026-11-06, no agent selector declared and
``SKMEMORY_VECTOR_BACKEND=pgvector`` in the environment):

* The CLI never read ``SKMEMORY_VECTOR_BACKEND`` at all. It tried the retired
  embedded vector store, then Qdrant, and printed a "trying Qdrant" warning to
  stderr. A different vector store is a different set of answers.
* The Postgres DSN lookup (``SKMEMORY_PG_DSN`` env, then the yaml) was copied
  into four call sites in ``cli.py`` and a fifth in ``mcp_server.py``.

The contract implemented here:

1. ``SKMEMORY_VECTOR_BACKEND`` names a backend explicitly (``pgvector``,
   ``skvector``, or ``none``). That backend is used or the operation fails
   with :class:`BackendInitError` carrying the real reason. There is no
   fallback to another store.
2. With no explicit request, the config's ``backends_enabled`` list decides.
   Declaring nothing means no vector backend.
3. ChromaDB is retired (SKMEM-P5-01). Naming it in ``SKMEMORY_VECTOR_BACKEND``
   fails fast with :data:`CHROMA_RETIRED_MESSAGE` (non-zero exit on the CLI,
   red health). A stale ``chroma`` entry in a yaml is NOT fatal: it is dropped
   with a one-time warning in ``config.py`` so old node configs cannot take
   down session hooks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

VECTOR_BACKEND_ENV = "SKMEMORY_VECTOR_BACKEND"
PG_DSN_ENV = "SKMEMORY_PG_DSN"

# The release that removed ChromaDB. Quoted in the fail-fast message.
CHROMA_RETIRED_IN = "0.11.99"
CHROMA_RETIRED_MESSAGE = (
    f"ChromaDB was retired in skmemory {CHROMA_RETIRED_IN}; use pgvector (shared) "
    "or the SQLite FTS5 local arm. Unset SKMEMORY_VECTOR_BACKEND or set it to "
    "pgvector or none."
)

# alias -> canonical name. "none" means "explicitly no vector backend".
_ALIASES = {
    "pgvector": "pgvector",
    "pg": "pgvector",
    "postgres": "pgvector",
    "skvector": "skvector",
    "qdrant": "skvector",
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
    if raw in ("chroma", "chromadb"):
        raise BackendInitError(f"{VECTOR_BACKEND_ENV}={raw}: {CHROMA_RETIRED_MESSAGE}")
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


def enabled_vector_backends(
    cfg: Any = None,
    *,
    has_skvector_url: bool = False,
    environment: Mapping[str, str] | None = None,
) -> tuple[bool, bool, str | None]:
    """Return ``(pgvector, skvector, requested)`` flags.

    When ``requested`` is not None exactly one flag is set (or none for
    ``"none"``) and the caller must NOT fall back if it fails to initialise.
    """
    requested = requested_vector_backend(environment)
    if requested is not None:
        return (requested == "pgvector", requested == "skvector", requested)
    enabled = list(getattr(cfg, "backends_enabled", None) or []) if cfg is not None else []
    return ("pgvector" in enabled, "skvector" in enabled, None)
