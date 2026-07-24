"""Cold-boot prune guardrail for the node-local skmem-pg reconcile (card 6b8b3ced).

The reconcile prune step deletes pg rows whose flat file is gone. On a freshly
wiped / mid-Syncthing-sync machine the flat store can be empty or nearly empty
*before* it is restored; a naive prune then wipes every derived pg row for the
agent and reports success. These tests pin the guardrail that refuses such a
destructive prune.

Unlike ``test_reconcile_invariant.py`` (which needs a LIVE local skmem-pg + mxbai
endpoint and is skipped offline), these run entirely offline: the pure
``prune_guard`` decision is exercised directly, and the ``reconcile()`` wiring is
exercised against a fake ``psql`` (monkeypatched ``subprocess.run``) so no
container, network, or real DELETE is involved.
"""

from __future__ import annotations

import json
import uuid

import pytest

from skmemory import reconcile as reconcile_mod
from skmemory.reconcile import prune_guard


# --------------------------------------------------------------------------- #
# 1. Pure guardrail decision (no I/O)                                          #
# --------------------------------------------------------------------------- #

def test_empty_flat_source_refuses_prune():
    """The cold-boot case: flat store is empty but pg is full -> REFUSE."""
    allowed, reason = prune_guard(flat_count=0, pg_count=1000, would_prune=1000)
    assert allowed is False
    assert "floor" in reason and "cold-boot" in reason


def test_flat_below_floor_refuses_prune():
    allowed, reason = prune_guard(flat_count=0, pg_count=5, would_prune=5, floor=1)
    assert allowed is False


def test_normal_small_delta_is_allowed():
    """A normal prune (a few orphans well under the fraction cap) proceeds."""
    allowed, reason = prune_guard(flat_count=100, pg_count=100, would_prune=3)
    assert allowed is True
    assert reason.startswith("ok")


def test_large_fraction_refuses_without_force():
    """Non-empty flat but the prune would wipe most of pg -> REFUSE (mid-sync)."""
    allowed, reason = prune_guard(
        flat_count=50, pg_count=1000, would_prune=950, max_fraction=0.20
    )
    assert allowed is False
    assert "cap" in reason


def test_force_overrides_every_guard():
    assert prune_guard(0, 1000, 1000, force=True)[0] is True
    assert prune_guard(50, 1000, 950, force=True)[0] is True


def test_noop_is_always_allowed():
    """Nothing to prune -> allowed regardless of flat/pg counts (idempotent run)."""
    allowed, reason = prune_guard(flat_count=0, pg_count=1000, would_prune=0)
    assert allowed is True
    assert "noop" in reason


def test_fraction_cap_boundary():
    # exactly at the cap is allowed; just over is refused
    assert prune_guard(100, 100, 20, max_fraction=0.20)[0] is True
    assert prune_guard(100, 100, 21, max_fraction=0.20)[0] is False


def test_small_store_single_prune_allowed_despite_high_fraction():
    """On a tiny store the fraction cap is skipped: 1 of 3 (33%) is a normal prune.

    This is the exact scenario the live reconcile invariant exercises; the cap
    must not fire below ``min_sample`` or every small delete would be refused.
    """
    allowed, reason = prune_guard(flat_count=2, pg_count=3, would_prune=1)
    assert allowed is True
    assert reason.startswith("ok")


def test_fraction_cap_applies_only_at_or_above_min_sample():
    # 5 of 10 (50%) below the default min_sample of 20 -> allowed
    assert prune_guard(5, 10, 5)[0] is True
    # same 50% at min_sample=10 -> the cap now applies -> refused
    assert prune_guard(5, 10, 5, min_sample=10)[0] is False


# --------------------------------------------------------------------------- #
# 2. reconcile() wiring against a fake psql (offline; no DELETE, no network)   #
# --------------------------------------------------------------------------- #

class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    """Minimal stand-in for the ``docker exec skmem-pg psql`` calls reconcile makes.

    Records every SQL statement so the test can assert whether a destructive
    DELETE was ever issued, and answers the handful of SELECTs reconcile needs.
    Also swallows the best-effort ``sk-alert`` subprocess so no Telegram fires.
    """

    def __init__(self, pg_ids, delete_count=0):
        self.pg_ids = list(pg_ids)
        self.delete_count = delete_count
        self.statements = []
        self.delete_issued = False
        self.alerts = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            self.alerts.append(args)
            return _CP(stdout="")
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        low = sql.lower()
        if "delete from memories" in low:
            self.delete_issued = True
            return _CP(stdout=f"{self.delete_count}\n")
        if low.startswith("select id from memories where agent"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if "embedding is null" in low:
            return _CP(stdout="")  # no null-vector rows to re-embed
        if "count(*) filter" in low:
            n = len(self.pg_ids)
            return _CP(stdout=f"{n}/{n}")
        return _CP(stdout="")


def _write_flat(mem_dir, layer, mem_id):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": f"fixture {mem_id[:8]}", "layer": layer})
    )
    return mem_id


def test_reconcile_refuses_prune_when_flat_empty(monkeypatch, tmp_path):
    """Empty flat + full pg: reconcile must NOT issue a DELETE and must flag skip."""
    pg_ids = [str(uuid.uuid4()) for _ in range(500)]
    fake = _FakePsql(pg_ids)
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"  # exists but has zero flat json files
    (mem / "short-term").mkdir(parents=True)

    stats = reconcile_mod.reconcile(
        "__guard_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["flat"] == 0
    assert stats["pg"] == 500
    assert stats["pruned"] == 0
    assert stats["prune_skipped"] is True
    assert "cold-boot" in stats["prune_reason"]
    assert fake.delete_issued is False, "an empty flat store must never DELETE pg rows"
    # a refusal alerts loudly to sk-alert
    assert any(a[0] == "sk-alert" for a in fake.alerts)


def test_reconcile_prunes_normal_delta(monkeypatch, tmp_path):
    """Flat mostly intact with a single orphan: prune proceeds as before."""
    kept = [str(uuid.uuid4()) for _ in range(20)]
    orphan = str(uuid.uuid4())
    pg_ids = kept + [orphan]  # pg has one row with no flat file
    fake = _FakePsql(pg_ids, delete_count=1)
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    for mid in kept:  # every kept id has a flat file; orphan does not
        _write_flat(mem, "mid-term", mid)

    stats = reconcile_mod.reconcile(
        "__guard_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["flat"] == 20
    assert stats["pg"] == 21
    assert stats["prune_skipped"] is False
    assert stats["pruned"] == 1
    assert fake.delete_issued is True, "a normal small delta must still prune orphans"


def test_reconcile_force_overrides_empty_guard(monkeypatch, tmp_path):
    """force_prune=True lets an empty-flat prune through (explicit operator intent)."""
    pg_ids = [str(uuid.uuid4()) for _ in range(10)]
    fake = _FakePsql(pg_ids, delete_count=10)
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    (mem / "short-term").mkdir(parents=True)

    stats = reconcile_mod.reconcile(
        "__guard_test__",
        mem_dir=str(mem),
        psql_cmd=["psql"],
        verbose=False,
        force_prune=True,
    )

    assert stats["prune_skipped"] is False
    assert fake.delete_issued is True
    assert stats["pruned"] == 10
