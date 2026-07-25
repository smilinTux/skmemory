"""Cascade executor: run one operation across the derived memory backends.

SKMemory keeps a required *primary* backend plus optional *derived* backends
(a vector store for semantic search, a graph store for relationships). After a
primary write or delete, the same change must fan out to whichever derived
backends are attached. Historically every store operation (``store``,
``promote``, ``ingest_seed``, ``forget``) hand-rolled that fan-out with its own
``if self.vector: try/except`` / ``if self.graph: try/except`` block, so
partial-failure handling drifted between call sites and a backend that silently
lacked a method could leave rows behind unnoticed (card 7d3e9fcc / Gap A).

:class:`CascadeExecutor` centralises that fan-out. Given an ordered list of
:class:`CascadeStep` (each naming a derived backend, a method, and its args) it:

* runs the steps in order (primary is the caller's job; this is derived-only),
* treats a ``None`` backend as "not attached" and skips it,
* is best-effort: a failing step is recorded and the cascade continues, so one
  dead backend never blocks the others,
* never swallows silently: every failure and every required-but-missing method
  is captured in a :class:`CascadeResult` and logged, so callers can inspect
  exactly which backends succeeded and which did not.

The executor deliberately does not know about the WAL, tombstones, or the
primary backend: those stay in the calling store method. It only owns the
derived-store fan-out and its partial-failure accounting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Step outcome codes.
OK = "ok"
FAILED = "failed"
MISSING = "missing"  # a required method was absent on the backend


@dataclass
class CascadeStep:
    """One derived-backend operation in a cascade.

    Args:
        role: Human label for the backend's role (e.g. ``"vector"``, ``"graph"``).
        backend: The backend instance, or ``None`` if not attached (then skipped).
        method: Name of the method to invoke on the backend.
        args: Positional args passed to the method.
        check_presence: When True, verify the method exists (and is callable)
            before calling. A missing method is recorded as ``MISSING`` rather
            than attempted. When False, the method is called directly and an
            ``AttributeError`` is caught like any other failure. Use True when a
            missing method is a real, reportable gap (e.g. a vector backend with
            no ``remove()``); False when the method is part of the backend
            contract and its absence should read as a plain failure.
        warn_missing: Optional pre-rendered warning logged when a
            ``check_presence`` step finds its method absent.
        warn_fail: Optional callable ``(exc) -> str`` producing the warning
            message logged when the step raises. Receives the exception so the
            caller controls exact wording (backend name, id, error).
    """

    role: str
    backend: Any
    method: str
    args: tuple = ()
    check_presence: bool = True
    warn_missing: str | None = None
    warn_fail: Callable[[BaseException], str] | None = None


@dataclass
class StepResult:
    """Outcome of a single cascade step."""

    role: str
    backend: str  # backend class name, or "" when the backend was absent
    method: str
    status: str  # OK | FAILED | MISSING
    error: str | None = None


@dataclass
class CascadeResult:
    """Aggregate outcome of a cascade across derived backends."""

    op: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no step failed and no required method was missing."""
        return not self.failed and not self.missing

    @property
    def failed(self) -> list[StepResult]:
        """Steps whose backend method raised."""
        return [s for s in self.steps if s.status == FAILED]

    @property
    def missing(self) -> list[StepResult]:
        """Steps whose required method was absent on the backend."""
        return [s for s in self.steps if s.status == MISSING]


class CascadeExecutor:
    """Execute an operation across derived backends, best-effort, with reporting.

    A single executor can be reused across calls; it holds no per-op state.
    """

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logger

    def run(self, op: str, steps: list[CascadeStep]) -> CascadeResult:
        """Run *steps* in order and collect a per-backend result.

        Absent backends (``backend is None``) are skipped and not recorded.
        Every attached backend is attempted regardless of an earlier step's
        failure. Nothing is raised for a backend failure; inspect the returned
        :class:`CascadeResult` (or its logged warnings) to react.

        Args:
            op: Label for the operation being cascaded (e.g. ``"forget"``).
            steps: Ordered derived-backend steps.

        Returns:
            CascadeResult: One :class:`StepResult` per attached backend.
        """
        result = CascadeResult(op=op)
        for step in steps:
            if step.backend is None:
                continue
            bname = type(step.backend).__name__

            if step.check_presence:
                fn = getattr(step.backend, step.method, None)
                if not callable(fn):
                    result.steps.append(
                        StepResult(step.role, bname, step.method, MISSING)
                    )
                    if step.warn_missing:
                        self._log.warning(step.warn_missing)
                    continue

            try:
                fn = getattr(step.backend, step.method)
                fn(*step.args)
                result.steps.append(StepResult(step.role, bname, step.method, OK))
            except Exception as exc:  # best-effort: record, keep going
                result.steps.append(
                    StepResult(step.role, bname, step.method, FAILED, str(exc))
                )
                if step.warn_fail:
                    self._log.warning(step.warn_fail(exc))

        return result
