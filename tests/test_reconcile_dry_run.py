"""``--dry-run`` for the node-local skmem-pg reconcile.

Card: skmem_reconcile.py was four copies, none of them supported a dry run,
so proving the consolidated entry point works on a host meant either trusting
it blind or accepting real writes. ``reconcile(..., dry_run=True)`` must
compute and report the same counts (flat/pg/missing/shared/stub_skipped/would
prune/would embed) WITHOUT issuing any mutating SQL and WITHOUT calling the
embed endpoint, so an operator can run it against production data with zero
risk.
"""

from __future__ import annotations

import json

import pytest

from skmemory import reconcile as reconcile_mod

_MUTATING_MARKERS = (
    "create table",
    "copy ",
    "insert into memories",
    "delete from memories",
    "update memories",
)


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    def __init__(self, pg_ids, all_ids=None, null_ids=None):
        self.pg_ids = list(pg_ids)
        self.all_ids = list(all_ids) if all_ids is not None else list(pg_ids)
        self.null_ids = list(null_ids or [])
        self.statements = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            return _CP(stdout="")
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        low = sql.lower()
        if low.startswith("select id from memories where agent"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if low.startswith("select id from memories;"):
            return _CP(stdout="\n".join(self.all_ids) + ("\n" if self.all_ids else ""))
        if "embedding is null" in low:
            rows = "\n".join(f"{i}\tsome content" for i in self.null_ids)
            return _CP(stdout=rows)
        if "count(*) filter" in low:
            n = len(self.pg_ids)
            return _CP(stdout=f"{n}/{n}")
        return _CP(stdout="")

    def mutating_statements(self):
        return [s for s in self.statements if any(m in s.lower() for m in _MUTATING_MARKERS)]


def _write_flat(mem_dir, layer, mem_id, content="real content"):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": content, "layer": layer})
    )
    return mem_id


@pytest.fixture
def no_network(monkeypatch):
    """A dry run must never need the embed endpoint; blow up loudly if it tries."""

    def _boom(*args, **kwargs):
        raise AssertionError("dry_run must not call the embed endpoint")

    monkeypatch.setattr(reconcile_mod.requests, "post", _boom)


def test_dry_run_issues_no_mutating_sql(monkeypatch, tmp_path, no_network):
    # 1 orphan (would prune), 2 missing real ids (would backfill)
    orphan = "orphan1"
    fake = _FakePsql(pg_ids=[orphan], all_ids=[orphan])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "real1")
    _write_flat(mem, "short-term", "real2")

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, dry_run=True
    )

    assert fake.mutating_statements() == [], (
        "dry_run must issue zero mutating statements: " + repr(fake.mutating_statements())
    )
    assert stats["dry_run"] is True
    assert stats["missing"] == 2
    assert stats["backfilled"] == 2, "dry_run reports WOULD-backfill, without writing"
    assert stats["pruned"] == 1, "dry_run reports WOULD-prune, without deleting"


def test_dry_run_reports_would_be_null_embed_count(monkeypatch, tmp_path, no_network):
    fake = _FakePsql(pg_ids=["has-null"], all_ids=["has-null"], null_ids=["has-null"])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "has-null")

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, dry_run=True
    )

    assert fake.mutating_statements() == []
    assert stats["null_embedded"] == 1, "dry_run reports WOULD-embed, without an UPDATE"


def test_dry_run_is_idempotent_and_matches_a_second_dry_run(monkeypatch, tmp_path, no_network):
    """Running dry_run twice in a row must report the same counts (nothing
    was mutated by the first run)."""
    fake = _FakePsql(pg_ids=[], all_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "real1")

    first = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, dry_run=True
    )
    second = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, dry_run=True
    )
    assert first["missing"] == second["missing"] == 1
    assert first["backfilled"] == second["backfilled"] == 1
