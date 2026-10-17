"""Two behaviors ported into the canonical ``skmemory.reconcile`` engine from
copies that had them independently (card: skmem_reconcile.py was four copies,
see docs/postmortems/2026-10-16-sqlite-only-orphans.md section 3.12).

1. Stub guard: a flat file carrying the ``recovered_from_sqlite_preview``
   marker (a lossy SQLite-preview rescue, see ``skmemory/stub_markers.py``)
   must never be backfilled into skmem-pg as real content. A reconcile run
   did exactly that to 373 ids on 2026-10-16, before any write path checked
   for the marker. The pgvector backend (``PGVectorBackend.save()``) and the
   standalone cron script both grew this guard independently; the vendored
   engine that ``skmemory sync --vector`` actually calls did not have it at
   all until this change.

2. Shared-id convergence fix: ``memories_pkey`` is a bare ``id`` primary key,
   not ``(id, agent)``. When two agents hold a flat file for the same id
   (shared "Seed: emotion-*" records arrive in several agents' dirs via
   Syncthing), whichever agent reconciles first claims the row and every
   other agent's reconcile counted it as "missing" forever: opus reported
   missing=7 and re-embedded the same 7 records every single night, with the
   count never reaching zero. This was fixed in the standalone
   ``scripts/skmem_reconcile.py`` copy but never ported into this engine.

Both are exercised fully offline against a fake ``psql`` transport
(monkeypatched ``subprocess.run``) and a fake embed endpoint (monkeypatched
``requests.post``), following the pattern in test_reconcile_prune_guard.py.
"""

from __future__ import annotations

import json

import pytest

from skmemory import reconcile as reconcile_mod


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    """Fake ``docker exec skmem-pg psql`` transport.

    ``pg_ids`` is this agent's rows (what ``WHERE agent=`` returns);
    ``all_ids`` is every row in the table regardless of agent (used by the
    shared-id fix to distinguish "absent from the table" from "owned by
    another agent"). Records every COPY payload so tests can assert which
    ids actually got sent toward an INSERT.
    """

    def __init__(self, pg_ids, all_ids=None, delete_count=0):
        self.pg_ids = list(pg_ids)
        self.all_ids = list(all_ids) if all_ids is not None else list(pg_ids)
        self.statements = []
        self.copy_inputs = []
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
        if "copy" in low and "from stdin" in low:
            if "format csv" in low:
                self.copy_inputs.append(input or "")
            return _CP(stdout="")
        if "delete from memories" in low:
            return _CP(stdout="0\n")
        if low.startswith("select id from memories where agent"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if low.startswith("select id from memories;"):
            return _CP(stdout="\n".join(self.all_ids) + ("\n" if self.all_ids else ""))
        if "embedding is null" in low:
            return _CP(stdout="")
        if "count(*) filter" in low:
            n = len(self.pg_ids)
            return _CP(stdout=f"{n}/{n}")
        return _CP(stdout="")


def _write_flat(mem_dir, layer, mem_id, *, content="real content", stub=False, extra=None):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    payload = {"id": mem_id, "content": content, "layer": layer}
    if stub:
        payload["metadata"] = {"recovered_from_sqlite_preview": True}
    if extra:
        payload.update(extra)
    (d / f"{mem_id}.json").write_text(json.dumps(payload))
    return mem_id


@pytest.fixture
def fake_embed(monkeypatch):
    calls = []

    def _post(url, json=None, timeout=None):  # noqa: A002 - matches requests.post signature
        calls.append(json)

        class _Resp:
            def json(self):
                n = len(json["input"]) if json and "input" in json else 1
                return {"embeddings": [[0.1, 0.2] for _ in range(n)]}

        return _Resp()

    monkeypatch.setattr(reconcile_mod.requests, "post", _post)
    return calls


# --------------------------------------------------------------------------- #
# 1. Stub guard
# --------------------------------------------------------------------------- #


def test_stub_flagged_flat_file_is_skipped_by_default(monkeypatch, tmp_path, fake_embed):
    fake = _FakePsql(pg_ids=[], all_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "stub1", content="[recovered from SQLite preview", stub=True)

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["backfilled"] == 0
    assert stats["stub_skipped"] == 1
    assert fake_embed == [], "a stub-only backfill must never call the embed endpoint"
    assert not any("stub1" in payload for payload in fake.copy_inputs)


def test_force_stubs_allows_insertion(monkeypatch, tmp_path, fake_embed):
    fake = _FakePsql(pg_ids=[], all_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "stub1", content="[recovered from SQLite preview", stub=True)

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, force_stubs=True
    )

    assert stats["backfilled"] == 1
    assert stats["stub_skipped"] == 0
    assert any("stub1" in payload for payload in fake.copy_inputs)


def test_mixed_batch_backfills_real_and_skips_stub(monkeypatch, tmp_path, fake_embed):
    fake = _FakePsql(pg_ids=[], all_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "real1", content="a perfectly ordinary real memory")
    _write_flat(mem, "short-term", "stub1", content="[recovered from SQLite preview", stub=True)

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["backfilled"] == 1
    assert stats["stub_skipped"] == 1
    assert any("real1" in payload for payload in fake.copy_inputs)
    assert not any("stub1" in payload for payload in fake.copy_inputs)


# --------------------------------------------------------------------------- #
# 2. Shared-id convergence fix
# --------------------------------------------------------------------------- #


def test_id_owned_by_another_agent_is_reported_shared_not_missing(
    monkeypatch, tmp_path, fake_embed
):
    """A flat file whose id already exists in pg under a DIFFERENT agent must
    not be retried forever as 'missing' (memories_pkey is id-only, so this
    agent can never claim it)."""
    fake = _FakePsql(pg_ids=[], all_ids=["shared1"])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "shared1", content="owned by another agent")

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["missing"] == 0, "a shared id must not inflate the missing count"
    assert stats["shared"] == 1
    assert stats["backfilled"] == 0
    assert fake_embed == [], "a shared-only run must never call the embed endpoint"
    assert not any("shared1" in payload for payload in fake.copy_inputs)


def test_real_missing_id_without_a_shared_owner_still_backfills(monkeypatch, tmp_path, fake_embed):
    fake = _FakePsql(pg_ids=[], all_ids=[])
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)

    mem = tmp_path / "memory"
    _write_flat(mem, "short-term", "real1", content="a perfectly ordinary real memory")

    stats = reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False
    )

    assert stats["missing"] == 1
    assert stats["shared"] == 0
    assert stats["backfilled"] == 1
    assert len(fake_embed) == 1
    assert any("real1" in payload for payload in fake.copy_inputs)
