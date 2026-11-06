"""Concurrent skmemory-sync@<agent> runs must not share prune state.

Regression for the `relation "flat_ids" does not exist` failures on .41
(2026-10-27). Every skmemory-sync@ timer fires in the same second against the
same node-local skmem-pg. The prune used to build a keep set in a plain public
table named `flat_ids` across three separate psql invocations (CREATE, COPY,
DELETE ... NOT EXISTS, DROP), so concurrent runs dropped each other's table
mid-flight, and when they did not error, one agent's DELETE read another
agent's keep set and wiped its own derived rows.

The fake below models the database as ONE shared table namespace that persists
across psql invocations (like the real server) and interleaves two agents' runs
so that agent B's whole prune lands between agent A's statements. Under the old
code this either raised (A's keep table gone) or deleted A's flat-backed rows.
"""

from __future__ import annotations

import json
import re
import threading
import uuid

from skmemory import reconcile as reconcile_mod


class _CP:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _SharedDb:
    """A tiny shared-state skmem-pg: memories rows plus a public table namespace."""

    def __init__(self, rows: dict[str, str]):
        self.rows = dict(rows)  # id -> agent
        self.tables: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.statements: list[str] = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            return _CP()
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        else:
            sql = ""
        with self.lock:
            self.statements.append(sql)
            return self._exec(sql, input)

    def _exec(self, sql, stdin):
        low = sql.lower().strip()
        m = re.match(r"select id from memories where agent\s*=\s*'([^']+)'", low)
        if m:
            ids = [i for i, a in self.rows.items() if a == m.group(1)]
            return _CP(stdout="".join(f"{i}\n" for i in ids))
        if low.startswith("select id from memories;"):
            return _CP(stdout="".join(f"{i}\n" for i in self.rows))
        if "embedding is null" in low or "md5(" in low or "pg_class" in low:
            return _CP()
        if "count(*) filter" in low:
            return _CP(stdout=f"{len(self.rows)}/{len(self.rows)}")
        if low.startswith("drop table if exists") and "create table" in low:
            name = re.search(r"create table (\w+)", low).group(1)
            self.tables[name] = []
            return _CP()
        if low.startswith("drop table if exists"):
            self.tables.pop(re.search(r"drop table if exists (\S+?);", low).group(1), None)
            return _CP()
        if low.startswith("copy "):
            name = low.split()[1]
            if name not in self.tables:
                return _CP(returncode=1, stderr=f'ERROR: relation "{name}" does not exist')
            self.tables[name].extend((stdin or "").split())
            return _CP()
        if "delete from memories" in low:
            agent = re.search(r"m\.agent\s*=\s*'([^']+)'", low).group(1)
            if "any(" in low:
                arr = re.search(r"any\('\{(.*)\}'::text\[\]\)", sql, re.S | re.I).group(1)
                targets = {p.strip('"') for p in arr.split(",") if p}
                victims = [i for i, a in self.rows.items() if a == agent and i in targets]
            else:
                keep_tbl = re.search(r"from (\w+) f where", low).group(1)
                if keep_tbl not in self.tables:
                    return _CP(returncode=1, stderr=f'ERROR: relation "{keep_tbl}" does not exist')
                keep = set(self.tables[keep_tbl])
                victims = [i for i, a in self.rows.items() if a == agent and i not in keep]
            for i in victims:
                del self.rows[i]
            return _CP(stdout=f"{len(victims)}\n")
        return _CP()


def _write_flat(mem_dir, mem_id):
    d = mem_dir / "mid-term"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": f"fixture {mem_id[:8]}", "layer": "mid-term"})
    )


def _agent_fixture(tmp_path, name, n_kept=20):
    kept = [str(uuid.uuid4()) for _ in range(n_kept)]
    orphan = str(uuid.uuid4())
    mem = tmp_path / name / "memory"
    for mid in kept:
        _write_flat(mem, mid)
    return mem, kept, orphan


def test_prune_uses_no_shared_scratch_table(monkeypatch, tmp_path):
    mem, kept, orphan = _agent_fixture(tmp_path, "jarvis")
    db = _SharedDb({i: "jarvis" for i in kept + [orphan]})
    monkeypatch.setattr(reconcile_mod.subprocess, "run", db.run)

    stats = reconcile_mod.reconcile("jarvis", mem_dir=str(mem), psql_cmd=["psql"], verbose=False)

    assert stats["pruned"] == 1
    assert set(db.rows) == set(kept)
    joined = "\n".join(db.statements).lower()
    assert "flat_ids" not in joined, "prune must not use the shared flat_ids table"
    assert db.tables == {}, "prune must leave no scratch table behind"


def test_interleaved_agents_neither_fail_nor_cross_delete(monkeypatch, tmp_path):
    """Agent B's full run lands in the middle of agent A's prune.

    Old code: B's `DROP TABLE flat_ids` removed A's keep table (A failed with
    `relation "flat_ids" does not exist`), or A's DELETE read B's ids and wiped
    all 20 of A's flat-backed rows.
    """
    mem_a, kept_a, orphan_a = _agent_fixture(tmp_path, "jarvis")
    mem_b, kept_b, orphan_b = _agent_fixture(tmp_path, "opus")
    rows = {i: "jarvis" for i in kept_a + [orphan_a]}
    rows.update({i: "opus" for i in kept_b + [orphan_b]})
    db = _SharedDb(rows)

    b_stats = {}
    real_run = db.run
    fired = {"done": False}

    def interleaving_run(args, **kw):
        sql = args[args.index("-c") + 1] if "-c" in args else (kw.get("input") or "")
        # Right before A's DELETE (old code: after A's CREATE + COPY into
        # flat_ids), run B's whole reconcile to completion.
        if not fired["done"] and "delete from memories" in sql.lower() and "'jarvis'" in sql:
            fired["done"] = True
            b_stats.update(
                reconcile_mod.reconcile(
                    "opus", mem_dir=str(mem_b), psql_cmd=["psql"], verbose=False
                )
            )
        return real_run(args, **kw)

    monkeypatch.setattr(reconcile_mod.subprocess, "run", interleaving_run)

    a_stats = reconcile_mod.reconcile(
        "jarvis", mem_dir=str(mem_a), psql_cmd=["psql"], verbose=False
    )

    assert fired["done"], "the interleave point was never reached"
    assert a_stats["pruned"] == 1 and b_stats["pruned"] == 1
    survivors = set(db.rows)
    assert survivors == set(kept_a) | set(kept_b), "only the two real orphans may be deleted"


def test_prune_sql_quotes_ids_and_agent():
    sql = reconcile_mod.prune_orphans_sql("o'brien", {"a'b", 'c"d'})
    assert "m.agent='o''brien'" in sql
    assert "a''b" in sql and '\\"' in sql
    assert sql.count("DELETE FROM memories") == 1
