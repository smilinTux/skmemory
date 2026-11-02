"""Legacy hook scripts, kept as thin back-compat wrappers.

All hook logic now lives in ``skmemory hook <event>`` (skmemory/hook_cli.py)
and is wired into harness configs by ``skmemory hooks install``. These .sh
files only exec that command so existing settings.json entries keep working
until the installer replaces them.
"""

from pathlib import Path

HOOKS_DIR = Path(__file__).parent


def get_hook_path(name: str) -> Path:
    """Return absolute path to a hook script."""
    return HOOKS_DIR / name
