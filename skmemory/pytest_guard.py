"""Guard against a test run writing into a REAL agent's memory home.

Card: skmemory pytest-writes-to-real-fleet (found during the 2026-10-16
sqlite-only-orphans investigation). ~536 of Lumina's REAL memories turned
out to be pytest/CI test fixtures ("Memory item N", "e2e-test-peer",
"Sovereign Squad", other canned test phrases) -- the known
[[test-suites-write-to-the-real-fleet]] / [[pytest-mints-real-joules-into-live-wallet]]
failure class. skmemory's OWN test suite is isolated (``tests/conftest.py``
sets ``SKCAPSTONE_HOME`` to a throwaway temp dir before anything imports
skmemory), but nothing stopped a DIFFERENT repo's suite -- skchat,
skcapstone, skharness, anything that imports skmemory's store or MCP tools
-- from running with an ambient ``SKAGENT=lumina`` and no
``SKCAPSTONE_HOME`` override, so skmemory resolved and wrote straight into
the real, Syncthing-synced home.

This is deliberately a last-resort, library-level guard, not a substitute
for proper test isolation (an autouse fixture that sets ``SKCAPSTONE_HOME``
to a tmp dir before anything touches skmemory -- see this repo's
``tests/conftest.py`` for the pattern to copy into a consuming repo's own
test suite). It exists for the case where a consumer forgot to.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Explicit, audited opt-in: a test that genuinely needs to touch the real
#: agent home (e.g. a manual recovery drill) sets this.
_ALLOW_ENV = "SKMEMORY_ALLOW_PYTEST_REAL_WRITES"
_TRUE_VALUES = {"1", "true", "yes", "on"}


class PytestRealMemoryWriteRefused(RuntimeError):
    """Raised when a write would land in a real agent's memory home while
    pytest is actively running a test, with no explicit override."""


def _real_default_agents_dir() -> Path:
    """The unmodified, real agent-home root: ``~/.skcapstone/agents``.

    Computed fresh on every call (never cached) so it reflects the actual
    filesystem home, independent of whatever ``skmemory.agents`` cached at
    import time.
    """
    return Path.home() / ".skcapstone" / "agents"


def _is_isolated_elsewhere() -> bool:
    """True if the test run explicitly pointed the agent home somewhere
    other than the real default tree -- an informed, deliberate choice we
    always trust."""
    override = os.environ.get("SKMEMORY_HOME") or (
        f"{os.environ['SKCAPSTONE_HOME']}/agents" if os.environ.get("SKCAPSTONE_HOME") else ""
    )
    if not override:
        return False
    try:
        return Path(override).resolve() != _real_default_agents_dir().resolve()
    except OSError:
        # Can't resolve (e.g. path doesn't exist yet) -- treat as isolated
        # rather than guessing; the explicit override is evidence enough.
        return True


def assert_write_allowed(path: Path | str) -> None:
    """Raise if writing to ``path`` would land in the real agent home
    during an active pytest test, with nothing explicitly allowing it.

    No-ops entirely outside pytest (``PYTEST_CURRENT_TEST`` unset): this
    function can never change production behavior.

    Args:
        path: The flat-file (or db) path about to be written.

    Raises:
        PytestRealMemoryWriteRefused: If the write would land under the
            real, unmodified ``~/.skcapstone/agents/`` tree during an
            active pytest test, with no ``SKCAPSTONE_HOME``/
            ``SKMEMORY_HOME`` override pointing elsewhere and no
            ``SKMEMORY_ALLOW_PYTEST_REAL_WRITES`` opt-in.
    """
    current_test = os.environ.get("PYTEST_CURRENT_TEST")
    if not current_test:
        return  # not inside a running pytest test -- production path, no-op

    if os.environ.get(_ALLOW_ENV, "").strip().lower() in _TRUE_VALUES:
        return  # explicit, audited opt-in

    if _is_isolated_elsewhere():
        return  # the suite deliberately pointed the home elsewhere; trust it

    real_default = _real_default_agents_dir()
    candidate = Path(path)
    try:
        resolved_default = real_default.resolve()
    except OSError:
        return  # real tree doesn't even exist on this host -- nothing to protect
    try:
        candidate.resolve().relative_to(resolved_default)
    except (ValueError, OSError):
        return  # not under the real tree -- fine

    raise PytestRealMemoryWriteRefused(
        f"refusing to write {candidate} during an active pytest test "
        f"({current_test}): this path is inside the REAL, unmodified "
        f"{resolved_default} tree. Isolate the test (set SKCAPSTONE_HOME to "
        "a tmp dir BEFORE importing skmemory -- see tests/conftest.py in "
        "the skmemory repo for the pattern), or set "
        f"{_ALLOW_ENV}=1 if this is a deliberate, audited exception."
    )
