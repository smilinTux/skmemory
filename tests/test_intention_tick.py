"""Time tick: due selection, delivery order, failure, multi-host rule, alert text (SKMEM-P2-03)."""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from skmemory.eval import fixtures as fx
from skmemory.intention_tick import alert_text, run_tick, sk_alert_deliver
from skmemory.intentions import Intention, IntentionStore

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    monkeypatch.delenv("SKMEMORY_TICK_HOST", raising=False)


@pytest.fixture
def store(tmp_path):
    s = IntentionStore(tmp_path / "mem")
    for i in fx.load_intentions():
        s.save(Intention(**i.model_dump()))
    yield s
    s.close()


def _tick(store, deliver, **kw):
    return run_tick(store, now=NOW, deliver=deliver, any_host=True, **kw)


def test_fixture_vector_by_text(store):
    """The time-tick prompt vector is keyed by text, not number (p-16 vs p-17)."""
    vec = next(
        p for p in fx.load_prompts() if p.text == "time tick at T0: which time intentions are due"
    )
    sent: list[str] = []
    res = _tick(store, lambda t: sent.append(t) or True)
    assert sorted(res.fired) == sorted(vec.expected_fire) == ["int-002"]
    assert len(sent) == 1


def test_fire_records_state_and_count(store):
    _tick(store, lambda t: True)
    i = store.get("int-002")
    assert i.state == "fired" and i.fires == 1 and i.last_fired_at == NOW
    for untouched in ("int-001", "int-003", "int-009", "int-010"):
        assert store.get(untouched).fires == 0


def test_fires_once_across_ticks(store):
    sent: list[str] = []
    _tick(store, lambda t: sent.append(t) or True)
    _tick(store, lambda t: sent.append(t) or True)
    assert len(sent) == 1 and store.get("int-002").fires == 1


def test_armed_and_snooze_boundaries(store):
    # snooze elapsed: int-003 (armed) becomes due
    later = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    res = run_tick(store, now=later, deliver=lambda t: True, any_host=True)
    assert "int-003" in res.fired and "int-002" in res.fired
    # due_at == now is due
    res = run_tick(
        store,
        now=datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc),
        deliver=lambda t: True,
        any_host=True,
    )
    assert res.fired == ["int-001"]


def test_delivery_failure_keeps_intention_and_retries(store):
    res = _tick(store, lambda t: False)
    assert res.failed == ["int-002"] and res.fired == []
    i = store.get("int-002")
    assert i.state == "pending" and i.fires == 0
    res = _tick(store, lambda t: True)
    assert res.fired == ["int-002"]


def test_deliver_happens_before_state_change(store):
    seen = {}

    def deliver(text):
        seen["state_at_delivery"] = store.get("int-002").state
        return True

    _tick(store, deliver)
    assert seen["state_at_delivery"] == "pending"


def test_crash_after_delivery_duplicates_never_loses(store):
    sent: list[str] = []

    def deliver(text):
        sent.append(text)
        return True

    orig = store.apply

    def boom(*a, **k):
        raise RuntimeError("crash")

    store.apply = boom
    with pytest.raises(RuntimeError):
        _tick(store, deliver)
    store.apply = orig
    assert store.get("int-002").state == "pending"  # still due: not lost
    _tick(store, deliver)
    assert len(sent) == 2  # at most a duplicate


def test_alert_text_title_and_origin_id_only(tmp_path):
    i = Intention(
        id="x1",
        kind="time",
        title="Rotate\nthe token",
        origin_memory_id="m-abc",
        due_at=NOW,
        scope={"repo": "secret-repo"},
        trigger_terms=["secret-term"],
    )
    t = alert_text(i)
    assert (
        t
        == "Intention due: Rotate the token (origin: m-abc). Resolve: skmemory intentions done x1"
    )
    assert "secret" not in t and "\n" not in t
    assert "origin: none" in alert_text(i.model_copy(update={"origin_memory_id": None}))


def test_multi_host_rule(store):
    sent: list[str] = []
    d = lambda t: sent.append(t) or True  # noqa: E731
    other = run_tick(store, now=NOW, deliver=d, tick_host="skap01", hostname="skap02.lan")
    assert other.skipped and not sent and store.get("int-002").fires == 0
    mine = run_tick(store, now=NOW, deliver=d, tick_host="skap01", hostname="skap01.lan")
    assert mine.fired == ["int-002"]


def test_unconfigured_tick_host_refuses(store):
    res = run_tick(store, now=NOW, deliver=lambda t: True)
    assert res.skipped and "no tick host" in res.skipped and store.get("int-002").fires == 0


def test_env_tick_host(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_TICK_HOST", "skap01")
    assert run_tick(store, now=NOW, deliver=lambda t: True, hostname="skap01").fired == ["int-002"]


def test_concurrent_tick_on_same_host_skips(store):
    from skmemory.intention_tick import _tick_lock

    with _tick_lock(store) as got:
        assert got
        res = _tick(store, lambda t: True)
    assert res.skipped and store.get("int-002").fires == 0


def _fake_cmd(tmp_path, rc):
    log = tmp_path / "argv.txt"
    script = tmp_path / "fake-alert"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(log)!r},'w').write('\\n'.join(sys.argv[1:]))\nsys.exit({rc})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def test_sk_alert_command_shape(tmp_path, monkeypatch):
    script, log = _fake_cmd(tmp_path, 0)
    monkeypatch.setenv("SKMEMORY_ALERT_CMD", str(script))
    assert sk_alert_deliver("hello") is True
    assert log.read_text().split("\n") == [
        "-g",
        "intentions",
        "--title",
        "skmemory intention due",
        "hello",
    ]


def test_sk_alert_nonzero_and_missing(tmp_path, monkeypatch):
    script, _ = _fake_cmd(tmp_path, 1)
    monkeypatch.setenv("SKMEMORY_ALERT_CMD", str(script))
    assert sk_alert_deliver("x") is False
    monkeypatch.setenv("SKMEMORY_ALERT_CMD", str(tmp_path / "does-not-exist"))
    assert sk_alert_deliver("x") is False


def test_cli_tick_and_done(store, tmp_path, monkeypatch):
    from skmemory.cli import cli

    script, _ = _fake_cmd(tmp_path, 0)
    monkeypatch.setenv("SKMEMORY_ALERT_CMD", str(script))
    base = str(store.base_path)
    r = CliRunner().invoke(cli, ["intentions", "tick", "--base", base])
    assert r.exit_code == 2 and "no tick host" in r.output
    r = CliRunner().invoke(cli, ["intentions", "tick", "--base", base, "--any-host"])
    assert r.exit_code == 0 and "fired" in r.output
    r = CliRunner().invoke(cli, ["intentions", "done", "int-002", "--base", base])
    assert r.exit_code == 0 and "done" in r.output
    monkeypatch.setenv("SKMEMORY_ALERT_CMD", str(tmp_path / "nope"))
    r = CliRunner().invoke(cli, ["intentions", "tick", "--base", base, "--any-host"])
    assert r.exit_code == 0  # nothing due any more
