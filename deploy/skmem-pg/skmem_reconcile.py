#!/usr/bin/env python3
"""THIN SHIM -- the real reconcile engine lives at ``skmemory/reconcile.py``.

Card: skmem_reconcile.py was four independently-evolved copies (this one,
``scripts/skmem_reconcile.py``, ``~/clawd/scripts/skmem_reconcile.py``, and
the in-package ``skmemory/reconcile.py`` engine) -- see
docs/postmortems/2026-10-16-sqlite-only-orphans.md section 3.12. This copy's
unique contribution (the cold-boot prune guardrail, card 6b8b3ced) was ported
into the canonical engine.

The claim that used to stand here -- "no cron/systemd unit on the fleet
currently points at this file" -- was FALSE and is corrected: on the chi estate
``skmemory-pg-reconcile@<agent>.service`` executes
``~/.local/share/skmemory-runtime/deploy/skmem-pg/skmem_reconcile.py`` daily on
every host. Worse, the copy deployed there was the pre-shim 155-line standalone
version, so those nodes ran their own independently-evolved prune and never
picked up fixes to the engine (confirmed 2026-10-20: it does not contain the
string "archive" at all, so it treated every archived memory as an orphan). Keep
this shim, keep the path working, and when deploying make sure the node gets THIS
file and not an old standalone copy.

Prefer calling the canonical entry point directly: ``~/.skenv/bin/python3 -m
skmemory.reconcile [AGENT] [--force] [--dry-run] [--force-stubs]``.
"""

import os
import sys

_VENV_PYTHON = os.path.expanduser("~/.skenv/bin/python3")
_PYTHON = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable

os.execv(_PYTHON, [_PYTHON, "-m", "skmemory.reconcile", *sys.argv[1:]])
