"""Fresh-context execution seam for long-running memory maintenance.

Consolidation and promotion passes can be long and chatty: they scan many
memories, generate summaries, and (optionally) call an LLM. When such a pass
is driven from inside a live agent turn, all of that work pollutes the main
agent's working context window.

This module provides the *seam* for running a pass in a **fresh context** — an
isolated execution with a clean context window (in production, a spawned
subagent/subprocess). The seam is a pluggable, injectable callable — a
``FreshContextRunner`` — that takes a zero-argument "pass" callable, executes
it *somewhere*, and returns its result.

Design goals:
  * **No hardcoded LLM/subagent client.** The runner is injected. Production
    injects a runner that spawns a subagent/subprocess; tests inject a mock.
  * **Safe default.** :func:`in_process_runner` runs the pass in-process with
    no isolation — the identity element of the seam, with zero dependencies.
  * **Extensible.** :class:`SubprocessRunner` is the scaffold for real
    subagent/subprocess spawning; the actual spawn mechanism is itself injected
    (a ``spawn`` callable), so nothing here is coupled to a specific runtime.

Example::

    from skmemory.promotion import PromotionEngine
    from skmemory.fresh_context import SubprocessRunner

    def spawn(pass_fn):
        # production: serialize a job, launch `python -m skmemory promote`
        # (or a Claude subagent) with a clean context, collect the result.
        ...

    engine = PromotionEngine(store, runner=SubprocessRunner(spawn))
    result = engine.run_pass()  # runs the sweep in a fresh context
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger("skmemory.fresh_context")

T = TypeVar("T")

#: A zero-argument callable that performs one maintenance pass and returns a
#: result (e.g. ``PromotionEngine.sweep`` returning a ``PromotionResult``).
Pass = Callable[[], T]


@runtime_checkable
class FreshContextRunner(Protocol):
    """Protocol for a fresh-context runner.

    A runner receives a zero-argument *pass* callable, executes it in some
    context (in-process, a thread, a subprocess, a spawned subagent, ...) and
    returns whatever the pass returned. Implementations MUST propagate the
    pass's return value and SHOULD propagate exceptions so callers can handle
    failures uniformly.
    """

    def __call__(self, pass_fn: Pass[T]) -> T:  # pragma: no cover - protocol
        ...


def in_process_runner(pass_fn: Pass[T]) -> T:
    """Default runner: execute the pass in the current process and context.

    Provides **no** context isolation — it simply calls ``pass_fn()``. It is
    the safe, dependency-free default and the identity element of the seam, so
    that wiring the seam never changes behaviour unless a real fresh-context
    runner is injected.

    Args:
        pass_fn: The zero-argument pass to execute.

    Returns:
        Whatever ``pass_fn`` returns.
    """
    return pass_fn()


class SubprocessRunner:
    """Fresh-context runner that delegates the pass to an injected spawner.

    This is the seam for **real** subagent/subprocess spawning. It deliberately
    does NOT hardcode any LLM/subagent client, subprocess command, or
    serialization format. Instead you inject a ``spawn`` callable that knows how
    to run a pass in a clean context and return its result.

    A production ``spawn`` might serialize a job descriptor, launch
    ``python -m skmemory promote`` (or a Claude subagent) with a fresh context
    window, wait for completion, and deserialize the result. In tests, ``spawn``
    is a mock that records the call and returns a canned result.

    Args:
        spawn: Callable that executes the pass in a fresh context and returns
            its result.
    """

    def __init__(self, spawn: Callable[[Pass[T]], T]) -> None:
        self._spawn = spawn

    def __call__(self, pass_fn: Pass[T]) -> T:
        logger.debug("Dispatching maintenance pass via injected fresh-context spawner.")
        return self._spawn(pass_fn)


def resolve_runner(runner: FreshContextRunner | None) -> FreshContextRunner:
    """Return ``runner`` if provided, else the safe in-process default.

    Central helper so every call site defaults identically.

    Args:
        runner: An injected runner, or ``None``.

    Returns:
        FreshContextRunner: The runner to use.
    """
    return runner or in_process_runner
