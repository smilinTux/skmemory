"""The legacy standalone skmem_reconcile.py cron entry points are now thin
shims that exec the canonical ``skmemory.reconcile`` module, not independent
reimplementations.

Card: skmem_reconcile.py was four independently-evolved copies (this engine
plus three standalone scripts: ``~/skmem-build/skmem_reconcile.py`` /
``scripts/skmem_reconcile.py`` / ``deploy/skmem-pg/skmem_reconcile.py``), and
the one actually invoked by cron on both fleet hosts was the one PR #37's
stub guard never reached (see docs/postmortems/2026-10-16-sqlite-only-orphans.md
section 3.12). Consolidating means these paths must keep working (cron still
calls them by this exact path) while containing zero reconcile logic of their
own.

This replaces ``test_skmem_reconcile_stub_guard.py``, which pinned
``scripts/skmem_reconcile.py``'s own now-retired ``is_stub_flat()``/``main()``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_PATHS = [
    REPO_ROOT / "scripts" / "skmem_reconcile.py",
    REPO_ROOT / "deploy" / "skmem-pg" / "skmem_reconcile.py",
]


@pytest.mark.parametrize("path", SHIM_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shim_execs_the_canonical_module(path):
    body = path.read_text(encoding="utf-8")
    assert "skmemory.reconcile" in body
    assert "os.execv" in body, "a shim must exec, not import/shell out piecemeal"


@pytest.mark.parametrize("path", SHIM_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shim_does_not_reimplement_the_pipeline(path):
    body = path.read_text(encoding="utf-8")
    for banned in (
        "def is_stub_flat",
        "def embed(",
        "DELETE FROM memories",
        "docker exec",
        "import requests",
        "import subprocess",
    ):
        assert banned not in body, f"{path} still reimplements the pipeline ({banned!r})"


@pytest.mark.parametrize("path", SHIM_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shim_is_executable(path):
    assert os.access(path, os.X_OK), f"{path} must stay executable (cron calls it directly)"


@pytest.mark.parametrize("path", SHIM_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shim_forwards_argv_to_skmemory_reconcile(monkeypatch, path):
    """Importing and running the shim's module body (without actually exec'ing)
    must compute an argv for `python -m skmemory.reconcile` that carries every
    original argument through untouched."""
    import importlib.util
    import sys as sysmod

    captured = {}

    def fake_execv(python, argv):
        captured["python"] = python
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(sysmod, "argv", [str(path), "architect", "--dry-run", "--force-stubs"])

    spec = importlib.util.spec_from_file_location("legacy_shim", path)
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit):
        spec.loader.exec_module(mod)

    assert captured["argv"][1:] == [
        "-m",
        "skmemory.reconcile",
        "architect",
        "--dry-run",
        "--force-stubs",
    ]
