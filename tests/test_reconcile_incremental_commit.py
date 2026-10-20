"""The backfill was all-or-nothing, and that cost 40 minutes of embedding.

Every row was COPYed into a scratch table and then committed by ONE
`INSERT ... SELECT` at the very end. On 2026-10-20 a lumina backfill embedded
24,429 rows over 40 minutes and died on that final statement because ONE tag
out of 24,429 carried an unescaped backslash. Nothing landed. The rows were
recoverable only because the scratch table happened to outlive the failure and
could be re-inserted by hand.

Per batch, the same failure costs one batch. Everything already committed
stays committed, and the next run sees a smaller `missing` list, so the work
RESUMES instead of restarting. That matters most for the part that is slowest
to redo: the embedding is the expensive step and is already spent by the time
the insert runs.
"""

from __future__ import annotations

import json
import re

import pytest

from skmemory import reconcile as reconcile_mod

BATCH = 24  # must match the B in reconcile's backfill loop


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    """Counts COPYs, INSERTs and TRUNCATEs so batching is observable.

    ``fail_insert_on`` makes the Nth INSERT fail, which is how the real
    failure looked: the COPY succeeded and the commit did not.
    """

    def __init__(self, fail_insert_on: int | None = None):
        self.copies = 0
        self.inserts = 0
        self.truncates = 0
        self.drops = 0
        self.fail_insert_on = fail_insert_on
        self.statements: list[str] = []

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            return _CP()
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        low = sql.lower()

        if "copy" in low and "from stdin" in low:
            # Count ONLY the backfill staging COPY. The prune phase also does
            # `COPY flat_ids FROM STDIN` to build its comparison set, and
            # counting that made this fixture lie about the batch count.
            if "memories_bf_" in low:
                self.copies += 1
            return _CP()
        if low.lstrip().startswith("insert into memories"):
            self.inserts += 1
            if self.fail_insert_on and self.inserts == self.fail_insert_on:
                return _CP(returncode=1)
            return _CP()
        if low.lstrip().startswith("truncate"):
            self.truncates += 1
            return _CP()
        if "drop table" in low:
            self.drops += 1
            return _CP()
        if "md5(" in low:
            return _CP()
        if low.startswith("select id from memories where agent"):
            return _CP()
        if low.startswith("select id from memories;"):
            return _CP()
        if "embedding is null" in low:
            return _CP()
        if "count(*) filter" in low:
            return _CP(stdout="0/0")
        if "pg_class" in low:
            return _CP()
        if "delete from memories" in low:
            return _CP(stdout="0\n")
        return _CP()


def _write_flats(mem_dir, n, layer="short-term"):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"m{i:05d}.json").write_text(
            json.dumps(
                {"id": f"m{i:05d}", "title": f"T{i}", "content": f"content {i}", "layer": layer}
            )
        )


@pytest.fixture
def fake_embed(monkeypatch):
    def _post(url, json=None, timeout=None):  # noqa: A002
        class _Resp:
            def json(self):
                n = len(json["input"]) if json and "input" in json else 1
                return {"embeddings": [[0.1, 0.2] for _ in range(n)]}

        return _Resp()

    monkeypatch.setattr(reconcile_mod.requests, "post", _post)


def _run(monkeypatch, fake, mem, **kw):
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    return reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, **kw
    )


class TestEachBatchCommits:
    def test_three_batches_produce_three_inserts(self, monkeypatch, tmp_path, fake_embed):
        """Not one INSERT at the end. That was the whole problem."""
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH * 3)
        fake = _FakePsql()

        stats = _run(monkeypatch, fake, mem)

        assert fake.copies == 3
        assert fake.inserts == 3, "the commit must happen per batch"
        assert stats["committed"] == BATCH * 3

    def test_the_staging_table_is_cleared_between_batches(self, monkeypatch, tmp_path, fake_embed):
        """Without the TRUNCATE each batch would re-insert all previous rows."""
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH * 2)
        fake = _FakePsql()

        _run(monkeypatch, fake, mem)

        assert fake.truncates == 2

    def test_one_batch_still_works(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flats(mem, 5)
        fake = _FakePsql()

        stats = _run(monkeypatch, fake, mem)

        assert fake.copies == 1
        assert fake.inserts == 1
        assert stats["committed"] == 5

    def test_the_scratch_table_is_still_dropped_at_the_end(
        self, monkeypatch, tmp_path, fake_embed
    ):
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH + 1)
        fake = _FakePsql()

        _run(monkeypatch, fake, mem)

        assert fake.drops >= 1


class TestAFailureCostsOneBatch:
    def test_earlier_batches_survive_a_later_failure(self, monkeypatch, tmp_path, fake_embed):
        """The 2026-10-20 case, in miniature.

        Before this change a failure on the final INSERT discarded every row.
        Now the batches that already committed stay committed, and `committed`
        says so, so the next run has less to redo.
        """
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH * 3)
        fake = _FakePsql(fail_insert_on=3)  # the LAST batch fails

        with pytest.raises((RuntimeError, reconcile_mod.ReconcileTransportError)):
            _run(monkeypatch, fake, mem)

        # Two batches got through before the third failed.
        assert fake.inserts == 3
        assert fake.truncates == 2, "the two successful batches were each cleared"

    def test_a_first_batch_failure_loses_only_that_batch(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH * 3)
        fake = _FakePsql(fail_insert_on=1)

        with pytest.raises((RuntimeError, reconcile_mod.ReconcileTransportError)):
            _run(monkeypatch, fake, mem)

        assert fake.inserts == 1
        assert fake.truncates == 0


class TestDryRun:
    def test_dry_run_commits_nothing(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flats(mem, BATCH * 2)
        fake = _FakePsql()

        stats = _run(monkeypatch, fake, mem, dry_run=True)

        assert fake.copies == 0
        assert fake.inserts == 0
        assert fake.truncates == 0
        assert stats["committed"] == 0
        assert stats["backfilled"] == BATCH * 2, "dry run still REPORTS what it would do"


def test_committed_is_in_the_report(monkeypatch, tmp_path, fake_embed):
    """`backfilled` counted staged rows; it could not tell you what landed."""
    mem = tmp_path / "memory"
    _write_flats(mem, 3)
    fake = _FakePsql()
    stats = _run(monkeypatch, fake, mem)
    assert "committed" in stats
    assert stats["committed"] == stats["backfilled"]


def test_the_insert_is_still_conflict_safe(monkeypatch, tmp_path, fake_embed):
    """Per-batch commits must not change the ON CONFLICT semantics."""
    mem = tmp_path / "memory"
    _write_flats(mem, 3)
    fake = _FakePsql()
    _run(monkeypatch, fake, mem)
    inserts = [s for s in fake.statements if s.lower().lstrip().startswith("insert into memories")]
    assert inserts
    assert all(re.search(r"on conflict \(id\) do nothing", s, re.I) for s in inserts)
