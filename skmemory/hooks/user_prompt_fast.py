"""Stdlib-only UserPromptSubmit fast path for standing intentions (SKMEM-P2-04).

Run as ``python -S user_prompt_fast.py --harness <h>``: no site import, no
skmemory import. It exits at once (empty output, status 0) when the kill switch
is on, a reader is declared, or the memory index has no armed/fired event
intention. Only when something might fire does it hand the saved stdin to
``python -m skmemory.hook_cli user-prompt`` for the real prefilter. If it cannot
tell (no resolvable memory dir, unexpected error) it also hands over: a slow
correct answer beats a silent miss. It never raises and never prints a traceback.
"""

import contextlib
import os
import subprocess
import sys


def _base():
    home = os.environ.get("SKMEMORY_HOME", "").strip()
    if home:
        return os.path.join(home, "memory")
    names = {
        v: os.environ.get(v, "").strip()
        for v in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT")
        if os.environ.get(v, "").strip()
    }
    if len(set(names.values())) != 1:
        return None  # none declared or conflicting: let the real resolver decide
    cap = os.environ.get("SKCAPSTONE_HOME", "").strip()
    root = (
        os.path.join(cap, "agents")
        if cap
        else os.path.join(os.path.expanduser("~"), ".skcapstone", "agents")
    )
    return os.path.join(root, next(iter(names.values())), "memory")


def _maybe_armed(base):
    """True/False from a read-only index query; None when unknown."""
    db = os.path.join(base, "index.db")
    if not os.path.isdir(os.path.join(base, "intentions")):
        return False
    if not os.path.exists(db):
        return None  # flat files without an index: the real path will reindex or skip
    import sqlite3

    con = None
    try:
        con = sqlite3.connect(f"file:{db.replace('?', '%3f')}?mode=ro", uri=True, timeout=0.05)
        row = con.execute(
            "SELECT 1 FROM intentions WHERE kind='event' AND state IN ('armed','fired') LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError as e:
        return False if "no such table" in str(e) else None
    except Exception:
        return None
    finally:
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()


def main(argv):
    if os.environ.get("SKMEMORY_HOOKS") == "off":
        return 0
    if (os.environ.get("SKMEMORY_READER") or "").strip():
        return 0
    harness = "generic"
    if "--harness" in argv and argv.index("--harness") + 1 < len(argv):
        harness = argv[argv.index("--harness") + 1]
    base = _base()
    if base is not None and _maybe_armed(base) is False:
        return 0
    try:
        raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    except Exception:
        raw = b""
    exe = sys.executable
    proc = subprocess.run(
        [exe, "-m", "skmemory.hook_cli", "user-prompt", "--harness", harness],
        input=raw,
        capture_output=True,
        timeout=4,
        check=False,
    )
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except BaseException:
        rc = 0
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    os._exit(rc)
