#!/usr/bin/env python3
"""THIN SHIM -- the real reconcile engine lives at ``skmemory/reconcile.py``.

Card: skmem_reconcile.py was four independently-evolved copies (this one,
``deploy/skmem-pg/skmem_reconcile.py``, ``~/clawd/scripts/skmem_reconcile.py``,
and the in-package ``skmemory/reconcile.py`` engine), and the live cron on
both fleet hosts ran whichever one a shared checkout happened to have
checked out -- see
docs/postmortems/2026-10-16-sqlite-only-orphans.md section 3.12. All unique
behavior this file used to have (the stub/shrink guard, the shared-id
convergence fix for the bare ``id`` primary key) has been ported into the
canonical engine. This file now does nothing but exec it, so cron/systemd
entries that still call this exact path keep working.

Prefer calling the canonical entry point directly in any new or updated cron
line: ``~/.skenv/bin/python3 -m skmemory.reconcile [AGENT] [--force]
[--dry-run] [--force-stubs]``. This shim is kept for one release as a safety
net for anything still pointing at this path.
"""

import os
import sys

_VENV_PYTHON = os.path.expanduser("~/.skenv/bin/python3")
_PYTHON = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable

os.execv(_PYTHON, [_PYTHON, "-m", "skmemory.reconcile", *sys.argv[1:]])
