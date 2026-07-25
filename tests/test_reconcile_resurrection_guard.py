"""Resurrection guard for the node-local skmem-pg reconcile (card 7d3e9fcc).

The reconcile backfill step re-inserts any flat memory missing from pg. That is
a resurrection hazard for a deliberately *forgotten* memory: forget() removes it
from the flat store + pgvector + AGE, but a stale flat copy that reappears later
(Syncthing re-deliver from a node that has not seen the delete, a second source
path, or an ingest re-import) looks like a brand-new "missing" memory and would
be re-inserted. These tests pin the guard that refuses to resurrect a tombstoned
memory, while proving a normal (non-tombstoned) memory still reconciles in.

Runs entirely offline: the tombstone helpers are exercised directly, and
reconcile() is driven against a fake ``psql`` (monkeypatched ``subprocess.run``)
plus a stub embed, so no container, network, or real INSERT/DELETE is involved.
"""

from __future__ import annotations

import json
import uuid

from skmemory import reconcile as reconcile_mod
from skmemory.tombstones import (
    is_tombstoned,
    load_tombstones,
    tombstone_dir,
    write_tombstone,
)


# --------------------------------------------------------------------------- #
# 1. tombstones module (pure file I/O under tmp_path)                          #
# --------------------------------------------------------------------------- #

def test_write_then_load_tombstone(tmp_path):
    mem = tmp_path / "memory"
    mid = str(uuid.uuid4())
    assert load_tombstones(mem) == set()
    assert is_tombstoned(mem, mid) is False
    path = write_tombstone(mem, mid, agent="lumina")
    assert path is not None and path.exists()
    assert load_tombstones(mem) == {mid}
    assert is_tombstoned(mem, mid) is True


def test_write_tombstone_is_idempotent(tmp_path):
    mem = tmp_path / "memory"
    mid = str(uuid.uuid4())
    write_tombstone(mem, mid)
    write_tombstone(mem, mid)  # re-tombstone: no duplicate, no raise
    assert load_tombstones(mem) == {mid}
    assert len(list(tombstone_dir(mem).glob("*.json"))) == 1


def test_empty_memory_id_is_noop(tmp_path):
    mem = tmp_path / "memory"
    assert write_tombstone(mem, "") is None
    assert load_tombstones(mem) == set()


def test_malformed_tombstone_still_contributes_its_id(tmp_path):
    """A corrupt tombstone file is still honoured (id from the stem)."""
    mem = tmp_path / "memory"
    d = tombstone_dir(mem)
    d.mkdir(parents=True)
    mid = str(uuid.uuid4())
    (d / f"{mid}.json").write_text("{ this is not valid json")
    assert load_tombstones(mem) == {mid}


# --------------------------------------------------------------------------- #
# 2. reconcile() resurrection guard against a fake psql (offline)             #
# --------------------------------------------------------------------------- #

class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    """Records SQL so a test can assert whether a backfill INSERT or a prune
    DELETE was issued, and answers the SELECTs reconcile needs."""

    def __init__(self, pg_ids, delete_count=0):
        self.pg_ids = list(pg_ids)
        self.delete_count = delete_count
        self.statements = []
        self.insert_issued = False
        self.delete_issued = False
        self.alerts = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            self.alerts.append(args)
            return _CP()
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        low = sql.lower()
        if "insert into memories " in low and "select" in low:
            self.insert_issued = True
            return _CP()
        if "delete from memories" in low:
            self.delete_issued = True
            return _CP(stdout=f"{self.delete_count}\n")
        if low.startswith("select id from memories where agent"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if "embedding is null" in low:
            return _CP(stdout="")
        if "count(*) filter" in low:
            n = len(self.pg_ids)
            return _CP(stdout=f"{n}/{n}")
        return _CP(stdout="")


def _stub_embed(monkeypatch):
    """Stub the embedding endpoint so backfill needs no network."""
    class _R:
        def json(self):
            return {"embeddings": [[0.0, 0.0, 0.0]]}

    monkeypatch.setattr(reconcile_mod.requests, "post", lambda *a, **k: _R())


def _write_flat(mem_dir, layer, mem_id):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": f"fixture {mem_id[:8]}", "layer": layer})
    )
    return mem_id


def test_reconcile_does_not_resurrect_tombstoned_memory(monkeypatch, tmp_path):
    """The core guard: a forgotten memory whose stale flat copy reappears while
    pg no longer has it must NOT be backfilled."""
    forgotten = str(uuid.uuid4())
    fake = _FakePsql(pg_ids=[])  # forgotten already cascade-deleted from pg
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    _stub_embed(monkeypatch)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", forgotten)  # stale copy present
    write_tombstone(mem, forgotten)  # but it was deliberately forgotten

    stats = reconcile_mod.reconcile(
        "__rez_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["missing"] == 0, "a tombstoned memory must not count as missing"
    assert stats["backfilled"] == 0
    assert stats["resurrection_blocked"] == 1
    assert stats["tombstoned"] == 1
    assert fake.insert_issued is False, "a tombstoned memory must never be re-inserted"


def test_reconcile_still_backfills_a_normal_new_memory(monkeypatch, tmp_path):
    """The guard must not block a legitimate new (non-tombstoned) memory."""
    new_mem = str(uuid.uuid4())
    fake = _FakePsql(pg_ids=[])  # brand-new memory, not yet in pg
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    _stub_embed(monkeypatch)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", new_mem)  # no tombstone

    stats = reconcile_mod.reconcile(
        "__rez_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["missing"] == 1
    assert stats["backfilled"] == 1
    assert stats["resurrection_blocked"] == 0
    assert fake.insert_issued is True, "a genuinely new memory must still reconcile in"


def test_guard_is_selective_new_reconciles_tombstoned_blocked(monkeypatch, tmp_path):
    """With both a fresh memory and a tombstoned stale copy present, only the
    fresh one is backfilled; the tombstoned one is blocked."""
    fresh = str(uuid.uuid4())
    forgotten = str(uuid.uuid4())
    fake = _FakePsql(pg_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    _stub_embed(monkeypatch)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", fresh)
    _write_flat(mem, "mid-term", forgotten)
    write_tombstone(mem, forgotten)

    stats = reconcile_mod.reconcile(
        "__rez_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["missing"] == 1  # only the fresh one
    assert stats["backfilled"] == 1
    assert stats["resurrection_blocked"] == 1
    assert fake.insert_issued is True


def test_tombstoned_row_still_in_pg_is_pruned(monkeypatch, tmp_path):
    """A tombstoned id that somehow lingers in pg (with a stale flat copy) is
    treated as an orphan and pruned out, so 'forgotten' stays gone from pg."""
    forgotten = str(uuid.uuid4())
    kept = [str(uuid.uuid4()) for _ in range(20)]
    pg_ids = kept + [forgotten]  # forgotten still lingering in pg
    fake = _FakePsql(pg_ids, delete_count=1)
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    _stub_embed(monkeypatch)

    mem = tmp_path / "memory"
    for mid in kept:
        _write_flat(mem, "long-term", mid)
    _write_flat(mem, "short-term", forgotten)  # stale flat copy present
    write_tombstone(mem, forgotten)

    stats = reconcile_mod.reconcile(
        "__rez_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    # tombstoned id is excluded from the flat truth -> it is an orphan in pg
    assert stats["resurrection_blocked"] == 1
    assert stats["prune_skipped"] is False
    assert fake.delete_issued is True
    assert fake.insert_issued is False, "a tombstoned pg row is pruned, never re-inserted"


# --------------------------------------------------------------------------- #
# 3. end-to-end: store.forget() writes the tombstone reconcile then honours    #
# --------------------------------------------------------------------------- #

def test_forget_writes_tombstone_reconcile_honours(monkeypatch, tmp_path):
    """store.forget() records a tombstone; a later reconcile refuses to
    resurrect the memory even after a stale flat copy reappears."""
    from skmemory.backends.file_backend import FileBackend
    from skmemory.store import MemoryStore

    mem = tmp_path / "memories"  # FileBackend.base_path == the reconcile mem dir
    backend = FileBackend(base_path=str(mem))
    store = MemoryStore(primary=backend)

    m = store.snapshot(title="secret", content="delete me for good")
    assert store.forget(m.id) is True
    # forget must have laid down a durable tombstone
    assert is_tombstoned(mem, m.id) is True

    # a stale flat copy of the forgotten memory reappears (Syncthing re-deliver)
    (mem / "short-term").mkdir(parents=True, exist_ok=True)
    (mem / "short-term" / f"{m.id}.json").write_text(
        json.dumps({"id": m.id, "content": "delete me for good", "layer": "short-term"})
    )

    fake = _FakePsql(pg_ids=[])  # forgotten already gone from pg
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    _stub_embed(monkeypatch)

    stats = reconcile_mod.reconcile(
        "__rez_test__", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )
    assert stats["resurrection_blocked"] == 1
    assert stats["backfilled"] == 0
    assert fake.insert_issued is False
