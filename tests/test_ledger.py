"""Outcome ledger: append-only per-host files, merge-on-read, decayed scoring (SKMEM-P4-01)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from click.testing import CliRunner

from skmemory.cli import cli
from skmemory.eval import fixtures as fx
from skmemory.ledger import Ledger


def _load(ledger: Ledger) -> None:
    for e in fx.load_outcomes():
        ledger.record(e.skill, e.version, e.event, effect=e.effect, session=e.session, at=e.at)


def test_fixture_scores_and_ranks(tmp_path):
    exp = fx.load_outcomes_expected()
    led = Ledger(tmp_path, host="a")
    _load(led)
    scores = {s.key: s for s in led.scores(exp.as_of)}
    assert set(scores) == set(exp.expected)
    for key, want in exp.expected.items():
        s = scores[key]
        assert s.follow_rate == want.follow_rate, key
        assert s.net_effect_sign == want.net_effect_sign, key
        assert s.anti_pattern == want.anti_pattern, key
        assert s.rank == want.rank, key
        assert s.flag == want.flag, key
    assert round(scores["recover-schema@1.2"].net, 1) == 7.2
    assert round(scores["sync-to-clear-red@1.0"].net, 1) == -7.6


def test_anti_pattern_needs_three_follows(tmp_path):
    led = Ledger(tmp_path, host="a")
    for i in range(2):
        led.record("s", "1", "served", session=str(i), at="2026-10-30T00:00:00+00:00")
        led.record(
            "s", "1", "followed", effect="harm", session=str(i), at="2026-10-30T00:00:00+00:00"
        )
    assert led.score("s", "1", "2026-11-01T00:00:00+00:00").anti_pattern is False
    led.record("s", "1", "followed", effect="harm", session="3", at="2026-10-31T00:00:00+00:00")
    assert led.score("s", "1", "2026-11-01T00:00:00+00:00").anti_pattern is True


def test_hosts_write_separate_files_and_merge_deduped(tmp_path):
    a, b = Ledger(tmp_path, host="nor"), Ledger(tmp_path, host="chi")
    when = datetime(2026, 10, 3, tzinfo=timezone.utc)
    a.record("s", "1", "served", session="x", at=when)
    b.record("s", "1", "served", session="x", at=when)  # same logical event, other host
    b.record("s", "1", "served", session="y", at=when)
    files = sorted(p.name for p in (tmp_path / "ledger").iterdir())
    assert files == ["outcomes-chi-2026-10.jsonl", "outcomes-nor-2026-10.jsonl"]
    assert len(a.events()) == len(b.events()) == 2


def test_month_rollover_and_malformed_lines_skipped(tmp_path):
    led = Ledger(tmp_path, host="h")
    led.record("s", "1", "served", at="2026-09-30T23:00:00+00:00")
    led.record("s", "1", "served", at="2026-10-01T01:00:00+00:00")
    assert len(list((tmp_path / "ledger").glob("*.jsonl"))) == 2
    with (tmp_path / "ledger" / "outcomes-h-2026-10.jsonl").open("a") as f:
        f.write("not json\n{}\n")
    assert len(led.events()) == 2


def test_no_free_text_and_no_rewrite_api(tmp_path):
    led = Ledger(tmp_path, host="h")
    led.record("s", "1", "served", session="sess", at="2026-10-01T00:00:00+00:00")
    line = json.loads(next((tmp_path / "ledger").iterdir()).read_text())
    assert set(line) == {"skill", "version", "event", "at", "effect", "session", "id"}
    public = {n for n in dir(led) if not n.startswith("_")}
    assert not {
        n for n in public if n.startswith(("update", "delete", "remove", "rewrite", "edit"))
    }
    for bad in (
        dict(event="nope"),
        dict(event="served", effect="harm"),
        dict(event="followed", effect="x"),
    ):
        try:
            led.record("s", "1", **bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_reindex_rebuilds_table(tmp_path):
    led = Ledger(tmp_path, host="h")
    _load(led)
    db = sqlite3.connect(tmp_path / "index.db")
    db.execute("DELETE FROM ledger_events")
    db.commit()
    assert led.reindex() == 34
    assert db.execute("SELECT count(*) FROM ledger_events").fetchone()[0] == 34


def test_cli_top(tmp_path):
    _load(Ledger(tmp_path, host="h"))
    r = CliRunner().invoke(
        cli, ["ledger", "top", "--base", str(tmp_path), "--as-of", "2026-11-01T12:00:00+00:00"]
    )
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert "recover-schema@1.2" in lines[1] and "anti-pattern" in lines[-1]
    assert "served-never-followed" in lines[3]
