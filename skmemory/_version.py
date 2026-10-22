"""Version resolution for skmemory.

Lives in its own module so ``__init__`` can import it alongside everything
else: putting the resolver inline would place executable code above the
package's relative imports and trip ruff's E402.
"""

from __future__ import annotations

__all__ = ["__version__", "resolve_version"]


def resolve_version() -> str:
    """Return the version of the skmemory that is actually on the path.

    The git tag IS the version here: ``[project].version`` is ``dynamic`` and
    setuptools-scm derives it from the tag at build time (see
    ``[tool.setuptools_scm]`` in pyproject). When skmemory is installed,
    including ``pip install -e``, that derived value is exposed through
    installed package metadata and read here, so ``__version__`` cannot drift
    from the installed package the way a hardcoded string does.

    Fallback: in a bare source checkout with no installed metadata, ask
    setuptools-scm directly, which reads the same git tag.

    Mirrors ``skcapstone._resolve_version`` deliberately; keep them in step.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("skmemory")
    except PackageNotFoundError:
        pass

    # Uninstalled dev tree: derive from the git tag, the same single source the
    # build uses. The describe-command has to be repeated here because
    # get_version() does not apply [tool.setuptools_scm].git_describe_command
    # from pyproject, and this repo carries non-semver tags (swarm-*, fixwave-*,
    # memory-index-fix-*, nextcloud-v*) that would otherwise win and derive a
    # nonsense version. Keep in lockstep with pyproject and
    # .github/workflows/publish.yml.
    try:
        from pathlib import Path

        from setuptools_scm import get_version

        return get_version(
            root=str(Path(__file__).resolve().parents[1]),
            git_describe_command=[
                "git",
                "describe",
                "--dirty",
                "--tags",
                "--long",
                "--match",
                "v[0-9]*",
            ],
        )
    except Exception:  # pragma: no cover - no scm, no tags, or scm not installed
        pass

    return "0.0.0+unknown"


__version__ = resolve_version()
