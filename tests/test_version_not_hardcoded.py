"""`__version__` must describe the installed skmemory, not a frozen literal.

For 63 releases it was the literal "0.11.5", set before this package moved to
setuptools-scm and then never touched again. Nothing internal read it, so
nothing broke loudly; skcapstone's own `scripts/install-bundle.sh` just printed
"skmemory: 0.11.5" at the end of every fleet bundle install while installing
something else entirely.
"""

import re
from pathlib import Path

import skmemory
from skmemory._version import resolve_version


def test_version_matches_installed_metadata():
    """__version__ is whatever the installed distribution says it is."""
    from importlib.metadata import version as pkg_version

    assert skmemory.__version__ == pkg_version("skmemory")


def test_version_is_not_the_frozen_literal():
    """Guard the specific regression: the 0.11.5 constant is gone for good."""
    assert skmemory.__version__ != "0.11.5"
    assert resolve_version() != "0.0.0+unknown"


def test_init_does_not_hardcode_a_version():
    """No module may reintroduce a literal `__version__ = "x.y.z"` assignment.

    A string literal here is the whole bug: it cannot be wrong at review time
    and is guaranteed to be wrong later.
    """
    src = Path(skmemory.__file__).parent
    literal = re.compile(r'^__version__\s*=\s*["\']')
    offenders = [
        f"{path.relative_to(src)}:{lineno}"
        for path in src.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if literal.match(line)
    ]
    assert not offenders, f"hardcoded __version__ literal(s): {offenders}"


def test_version_looks_like_a_version():
    assert re.match(r"^\d+\.\d+\.\d+", skmemory.__version__), skmemory.__version__
