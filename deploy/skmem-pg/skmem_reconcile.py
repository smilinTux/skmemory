#!/usr/bin/env python3
"""THIN SHIM -- the real reconcile engine lives at ``skmemory/reconcile.py``.

Card: skmem_reconcile.py was four independently-evolved copies (this one,
``scripts/skmem_reconcile.py``, ``~/clawd/scripts/skmem_reconcile.py``, and
the in-package ``skmemory/reconcile.py`` engine) -- see
docs/postmortems/2026-10-16-sqlite-only-orphans.md section 3.12. This copy's
unique contribution (the cold-boot prune guardrail, card 6b8b3ced) was ported
into the canonical engine; no cron/systemd unit on the fleet currently points
at this file, but it is kept as a shim rather than deleted outright in case
anything still does, or any operator still runs it by muscle memory from this
path.

Prefer calling the canonical entry point directly: ``~/.skenv/bin/python3 -m
skmemory.reconcile [AGENT] [--force] [--dry-run] [--force-stubs]``.
"""

import os
import sys

_VENV_PYTHON = os.path.expanduser("~/.skenv/bin/python3")
_PYTHON = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable

os.execv(_PYTHON, [_PYTHON, "-m", "skmemory.reconcile", *sys.argv[1:]])
