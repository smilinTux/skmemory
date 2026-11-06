"""Harness integration sources (OpenCode plugin, Pi extension) shipped as package data.

`skmemory hooks install` renders these with the absolute path of the installed
`skmemory` console script and writes them into each harness's config dir.
"""

from pathlib import Path

HARNESS_DIR = Path(__file__).parent
PLACEHOLDER = "__SKMEMORY_BIN__"


def render(name: str, skmemory_bin: str) -> str:
    """Return the named template with the skmemory binary path substituted."""
    import json

    text = (HARNESS_DIR / name).read_text(encoding="utf-8")
    return text.replace(json.dumps(PLACEHOLDER), json.dumps(skmemory_bin))
