#!/usr/bin/env python3
"""Re-embed one agent's rows whose embed-text formula changed (dry run by default).

Thin entry point for :mod:`skmemory.reembed`; see its docstring. Example:

    SKMEMORY_PG_DSN=... python scripts/reembed_agent.py --agent lumina            # dry run
    SKMEMORY_PG_DSN=... python scripts/reembed_agent.py --agent lumina --apply    # writes
"""

from skmemory.reembed import main

if __name__ == "__main__":
    raise SystemExit(main())
