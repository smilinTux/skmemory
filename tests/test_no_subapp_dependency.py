"""Gate: skmemory CORE must import free of every higher-layer subapp.

skmemory is an L0 core package (capauth / skcomms / skmemory are the shared
core).  It may be composed WITH the higher-layer subapps (skcapstone, skchat,
skos, skharness), but importing the core memory surface must never PULL one of
them into ``sys.modules``.  Each core -> subapp coupling is either inverted,
lazily guarded, or degrades gracefully; this test locks that in.

The proof runs in a CLEAN interpreter subprocess (not the pytest process, which
has already imported plenty), imports the core surface, and asserts NONE of the
subapps entered ``sys.modules``.  The subapps ARE installed in a full dev/prod
env, so a green result there means the core genuinely does not touch them, not
merely that they are absent.  (On a bare CI runner the subapps are not installed
at all, so the gate is trivially green -- it is a regression guard for the
full-stack environment, where the coupling actually manifests.)

Historical note: skmemory did not import a subapp directly, but it leaked
``skcapstone`` *transitively* -- ``skmemory.store`` -> ``skmemory.skseed_validation``
and ``skmemory.steelman`` import the standalone ``skseed`` kernel, whose
``skseed.integration`` bridge used to import ``skcapstone`` at module load.  The
fix made that bridge (and skmemory's own ``integration`` bridge) resolve
skcapstone lazily on first use.  This test would have caught that leak.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# The higher-layer subapps that skmemory L0 must never import as a side effect
# of a core import.  (capauth is an L0 peer, out of scope here; skcomms is a
# peer too but is included because skmemory's only skcomms coupling
# -- context_loader's consent-grant check -- is in-function, so a core import
# must never pull it, and this locks that in.)
SUBAPPS = ("skcapstone", "skchat", "skcomms", "skos", "skharness")

# The skmemory CORE surface: a bare ``import skmemory`` plus every module its
# package ``__init__`` re-exports, each imported explicitly.  The bare import is
# the strictest single case (it runs the whole __init__ chain), but naming the
# core modules too means a future refactor cannot quietly reintroduce a leak
# off-__init__.
CORE_MODULES = (
    "skmemory",
    "skmemory.config",
    "skmemory.models",
    "skmemory.store",
    "skmemory.fortress",
    "skmemory.backends.file_backend",
    "skmemory.backends.sqlite_backend",
    "skmemory.anchor",
    "skmemory.journal",
    "skmemory.lovenote",
    "skmemory.moc",
    "skmemory.openclaw",
    "skmemory.quadrants",
    "skmemory.ritual",
    "skmemory.sealing",
    "skmemory.soul",
    "skmemory.steelman",
    "skmemory.synthesis",
    "skmemory.importers.telegram",
)


def _clean_import_leaks(modules: tuple[str, ...]) -> list[str]:
    """Import ``modules`` in a fresh interpreter; return any leaked subapps.

    Returns the sorted list of subapp names that entered ``sys.modules`` as a
    side effect of importing the given modules.  An empty list is the pass
    condition.
    """
    script = textwrap.dedent(
        f"""
        import sys
        for _m in {modules!r}:
            __import__(_m)
        _subapps = {SUBAPPS!r}
        _leaked = sorted(
            s for s in _subapps
            if any(k == s or k.startswith(s + ".") for k in sys.modules)
        )
        print(",".join(_leaked))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    return out.split(",") if out else []


def test_core_import_pulls_no_subapp():
    """Importing the whole skmemory core surface leaks zero subapps."""
    leaked = _clean_import_leaks(CORE_MODULES)
    assert leaked == [], (
        f"skmemory core import pulled in subapps: {leaked}. "
        "The L0 core must not depend on any higher-layer subapp."
    )


def test_bare_skmemory_import_pulls_no_subapp():
    """Even a bare ``import skmemory`` (package __init__) leaks zero subapps."""
    leaked = _clean_import_leaks(("skmemory",))
    assert leaked == [], f"`import skmemory` pulled in subapps: {leaked}"


def test_integration_module_import_is_lazy():
    """Importing the skcapstone bridge module must not eagerly load skcapstone.

    ``skmemory.integration`` legitimately bridges to skcapstone, but the import
    is resolved lazily on first use (``_get_sdk``), so merely importing the
    module never pulls skcapstone into ``sys.modules``.
    """
    leaked = _clean_import_leaks(("skmemory.integration",))
    assert leaked == [], (
        f"`import skmemory.integration` eagerly pulled in subapps: {leaked}. "
        "The skcapstone import must be lazy (deferred to first use)."
    )
