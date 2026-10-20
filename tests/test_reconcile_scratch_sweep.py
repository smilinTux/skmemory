"""Scratch tables from killed runs leaked forever.

The backfill scratch table is named per agent AND per process, which is what
stopped concurrent timer units from dropping each other's table mid-COPY (see
test_reconcile_scratch_table.py). That fixed the collision and created a leak:
the closing DROP only runs on the success path, so any run that is KILLED
leaves its table behind permanently, named after a PID that never comes back.

A `finally` cannot fix it, because SIGKILL runs no Python. So each run sweeps
the corpses of previous ones.

Measured on one node 2026-10-20: 136 orphan tables holding 293 MB, 53 of them
for a single agent, in a 2.4 GB database. Dropping the 134 whose PID was dead
reclaimed 263 MB.
"""

from __future__ import annotations

import os

import pytest

from skmemory.reconcile import (
    _pid_alive,
    _scratch_pid,
    _scratch_table_name,
    sweep_stale_scratch_tables,
)

DEAD_PID = 999_999_999  # above any real pid_max


class _FakePsql:
    """Records statements and answers the pg_class listing."""

    def __init__(self, tables, fail_list=False, fail_drop_on=()):
        self.tables = list(tables)
        self.fail_list = fail_list
        self.fail_drop_on = set(fail_drop_on)
        self.dropped: list[str] = []
        self.logs: list[str] = []

    def __call__(self, sql, want=False):
        if "pg_class" in sql:
            if self.fail_list:
                raise RuntimeError("transport died")
            return "\n".join(self.tables)
        if sql.lstrip().upper().startswith("DROP TABLE"):
            name = sql.split('"')[1]
            if name in self.fail_drop_on:
                raise RuntimeError("permission denied")
            self.dropped.append(name)
            return ""
        return ""


class TestScratchPidParsing:
    def test_it_reads_the_pid_off_a_real_name(self):
        assert _scratch_pid("memories_bf_lumina_12345") == 12345

    def test_an_agent_with_underscores_still_parses(self):
        assert _scratch_pid("memories_bf_test_agent_777") == 777

    @pytest.mark.parametrize(
        "name",
        ["memories", "memories_bf", "memories_bf_lumina", "some_other_table", "memories_bf_x_abc"],
    )
    def test_a_non_scratch_name_yields_none(self, name):
        assert _scratch_pid(name) is None

    def test_the_real_name_generator_round_trips(self):
        """Whatever _scratch_table_name produces must be parseable."""
        assert _scratch_pid(_scratch_table_name("lumina")) == os.getpid()


class TestPidAlive:
    def test_our_own_pid_is_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_an_impossible_pid_is_not(self):
        assert _pid_alive(DEAD_PID) is False


class TestSweep:
    def test_a_dead_pids_table_is_dropped(self):
        fake = _FakePsql([f"memories_bf_lumina_{DEAD_PID}"])
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 1
        assert fake.dropped == [f"memories_bf_lumina_{DEAD_PID}"]

    def test_a_live_pids_table_is_kept(self):
        """Another running sync is mid-COPY into that table."""
        fake = _FakePsql([f"memories_bf_jarvis_{os.getpid()}"])
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 0
        assert fake.dropped == []

    def test_our_own_table_is_never_dropped(self):
        fake = _FakePsql([_scratch_table_name("lumina")])
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 0
        assert fake.dropped == []

    def test_unrelated_tables_are_untouched(self):
        """The one thing this must never do is drop real data."""
        fake = _FakePsql(["memories", "memory_tombstones", "memories_bm25"])
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 0
        assert fake.dropped == []

    def test_it_sorts_the_mixed_case(self):
        fake = _FakePsql(
            [
                f"memories_bf_lumina_{DEAD_PID}",
                f"memories_bf_opus_{DEAD_PID - 1}",
                f"memories_bf_jarvis_{os.getpid()}",
                "memories",
            ]
        )
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 2
        assert sorted(fake.dropped) == sorted(
            [f"memories_bf_lumina_{DEAD_PID}", f"memories_bf_opus_{DEAD_PID - 1}"]
        )

    def test_it_logs_what_it_did(self):
        fake = _FakePsql([f"memories_bf_lumina_{DEAD_PID}"])
        sweep_stale_scratch_tables(fake, fake.logs.append)
        assert any("swept 1" in line for line in fake.logs)

    def test_a_clean_database_logs_nothing(self):
        fake = _FakePsql([])
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 0
        assert fake.logs == []


class TestSweepFailsSoftly:
    def test_a_failed_listing_does_not_break_the_reconcile(self):
        """Cleanup is housekeeping. It must never take the sync down with it."""
        fake = _FakePsql([f"memories_bf_lumina_{DEAD_PID}"], fail_list=True)
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 0
        assert any("could not list" in line for line in fake.logs)

    def test_one_undroppable_table_does_not_stop_the_others(self):
        bad = f"memories_bf_lumina_{DEAD_PID}"
        good = f"memories_bf_opus_{DEAD_PID - 1}"
        fake = _FakePsql([bad, good], fail_drop_on=(bad,))
        assert sweep_stale_scratch_tables(fake, fake.logs.append) == 1
        assert fake.dropped == [good]
        assert any(bad in line for line in fake.logs)
