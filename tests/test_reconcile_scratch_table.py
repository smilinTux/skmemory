"""
Regression: the backfill scratch table must be unique per reconcile run.

skmemory-sync@lumina, @jarvis and @opus are timer units that fire in the SAME
second and all talk to the same skmem-pg database. The backfill used a fixed
table name, `memories_bf`, and ended with `DROP TABLE IF EXISTS memories_bf`,
so one agent's DROP pulled the table out from under another agent's COPY:

    skmem-pg query failed (docker exec skmem-pg): ERROR:  relation
    "memories_bf" does not exist

Observed live on 2026-09-13 running a jarvis reconcile alongside the
skmemory-sync@jarvis unit. The scratch table name is therefore per agent and
per process.
"""

from __future__ import annotations

import os
import re

from skmemory import reconcile


def test_scratch_table_name_is_agent_and_process_scoped():
    lumina = reconcile._scratch_table_name("lumina")
    jarvis = reconcile._scratch_table_name("jarvis")
    assert lumina != jarvis, "two agents reconciling at once must not share a scratch table"
    assert str(os.getpid()) in lumina, "concurrent runs for one agent must not share it either"
    assert lumina == reconcile._scratch_table_name("lumina"), "must be stable within a run"


def test_scratch_table_name_is_a_safe_bare_identifier():
    for agent in ("lumina", "Dr. O'Brien; DROP TABLE memories;--", "agent-with-dash", ""):
        name = reconcile._scratch_table_name(agent)
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"unsafe identifier for {agent!r}: {name}"
        assert len(name) <= 63, "postgres truncates identifiers past 63 chars"


def test_no_fixed_scratch_table_name_remains_in_the_sql():
    """No SQL line may name the bare table; the prose explaining why may.

    `\bmemories_bf\b` does not match `memories_bf_{slug}`, since `_` is a word
    character, so only the old fixed name trips this. Requiring an uppercase SQL
    verb on the same line keeps the docstring that tells this story from failing
    the test that enforces it.
    """
    src = (reconcile.__file__ or "").replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    verbs = ("DROP", "CREATE", "COPY", "INSERT", "FROM", "TABLE")
    sql_lines = [
        line for line in body.splitlines()
        if re.search(r"\bmemories_bf\b", line) and any(v in line for v in verbs)
    ]
    assert not sql_lines, (
        "a hard-coded memories_bf is still reachable from SQL:\n" + "\n".join(sql_lines)
    )
