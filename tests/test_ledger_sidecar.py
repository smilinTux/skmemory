"""Harness sidecar feeding the outcome ledger (SKMEM-P4-02)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from skmemory import hook_cli, hook_install
from skmemory import ledger_sidecar as sc
from skmemory.cli import cli
from skmemory.ledger import Ledger

SECRET = "SECRET-PROMPT-TEXT-do-not-store"
SKILL_MD = """---
name: gog-demo
---
Use the gog CLI:

```bash
gog calendar events -a me --days 3
cat notes.txt
```

Also `git worktree add ../x` and `ls -la`.
"""


def _line(role, content, ts="2026-11-01T10:00:00.000Z", **kw):
    return {
        "type": role,
        "timestamp": ts,
        "sessionId": "sess-1",
        "cwd": "/w",
        "message": {"role": role, "content": content},
        **kw,
    }


def _skill(tid, name):
    return _line(
        "assistant", [{"type": "tool_use", "id": tid, "name": "Skill", "input": {"skill": name}}]
    )


def _bash(tid, cmd):
    return _line(
        "assistant", [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]
    )


def _result(tid, err=False):
    return _line(
        "user", [{"type": "tool_result", "tool_use_id": tid, "content": SECRET, "is_error": err}]
    )


def _human(text=SECRET):
    return _line("user", [{"type": "text", "text": text}])


def _write(path: Path, lines) -> Path:
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "skills" / "gog-demo").mkdir(parents=True)
    (h / ".claude" / "skills" / "gog-demo" / "SKILL.md").write_text(SKILL_MD)
    monkeypatch.setenv("HOME", str(h))
    for v in ("SKMEMORY_READER", "SKMEMORY_HOOKS", "SKMEMORY_LEDGER"):
        monkeypatch.delenv(v, raising=False)
    return h


def _ev(led, kind):
    return next(e for e in led.events() if e["event"] == kind)


def _record(tmp_path, home, lines):
    t = _write(tmp_path / "t.jsonl", lines)
    base = tmp_path / "mem"
    n = sc.record_transcript(t, base, home=home, host="h")
    return n, Ledger(base, host="h"), t, base


def test_declared_commands_skip_generic_and_keep_subcommands():
    sigs = sc.declared_commands(SKILL_MD)
    assert ("gog",) in sigs and ("git", "worktree") in sigs
    assert not any(s[0] in ("cat", "ls") for s in sigs)
    assert sc.command_matches("cd /x && gog calendar list", sigs)
    assert not sc.command_matches("git status", sigs)
    assert not sc.command_matches("ls -la", sigs)


def test_served_and_heuristic_followed_never_claim_success(tmp_path, home):
    lines = [_human(), _skill("t1", "gog-demo"), _bash("b1", "gog calendar events"), _result("b1")]
    n, led, *_ = _record(tmp_path, home, lines)
    assert n == 2 and sorted(e["event"] for e in led.events()) == ["followed", "served"]
    f = _ev(led, "followed")
    assert f["effect"] is None and f["source"] == sc.SRC_HEURISTIC
    assert f["missing"] and f["version"].startswith("sha:")
    assert led.score("gog-demo", f["version"]).net_effect_sign == "0"


def test_unmatched_or_generic_commands_give_served_only(tmp_path, home):
    lines = [_skill("t1", "gog-demo"), _bash("b1", "ls -la"), _result("b1")]
    _, led, *_ = _record(tmp_path, home, lines)
    assert [e["event"] for e in led.events()] == ["served"]


def test_unknown_skill_is_served_with_unknown_version(tmp_path, home):
    _, led, *_ = _record(tmp_path, home, [_skill("t1", "nope"), _bash("b1", "gog x")])
    (e,) = led.events()
    assert e["version"] == "unknown" and e["event"] == "served"


def test_failed_matched_call_is_followed_error_never_harm(tmp_path, home):
    lines = [_skill("t1", "gog-demo"), _bash("b1", "gog calendar"), _result("b1", err=True)]
    _, led, *_ = _record(tmp_path, home, lines)
    assert sorted(e["event"] for e in led.events()) == ["followed_error", "served"]
    f = _ev(led, "followed_error")
    assert f["effect"] is None and f["source"] == sc.SRC_ERROR and "cause unknown" in f["missing"]
    s = led.score("gog-demo", f["version"])
    assert (s.followed, s.net_effect_sign, s.anti_pattern) == (1, "0", False)


def test_imported_counters_do_not_feed_floors_or_rates(tmp_path):
    led = Ledger(tmp_path, host="h")
    for _ in range(3):
        led.record("s", "1", "followed", source=sc.SRC_HERMES, session="x")
        led.record("s", "1", "served", source=sc.SRC_HERMES, session="x")
    led.record("s", "1", "followed", effect="harm", session="real")
    sc_ = led.score("s", "1")
    assert sc_.followed == 1 and not sc_.anti_pattern  # 1 real follow < floor of 3
    assert (sc_.imported_served, sc_.imported_followed) == (3, 3)
    assert sc_.follow_rate == 0.0 and sc_.flag is None  # served 0 first-party
    led.record("t", "1", "served", source=sc.SRC_HERMES, session="x")
    for _ in range(3):
        led.record("t", "1", "served", session="real")
    assert led.score("t", "1").flag == "served-never-followed"
    assert led.score("t", "1").imported_served == 1


def test_failed_unmatched_call_is_not_harm(tmp_path, home):
    lines = [
        _skill("t1", "gog-demo"),
        _bash("b1", "gog calendar"),
        _result("b1"),
        _bash("b2", "make broken"),
        _result("b2", err=True),
    ]
    _, led, *_ = _record(tmp_path, home, lines)
    assert _ev(led, "followed")["effect"] is None


def test_window_closes_at_next_human_message(tmp_path, home):
    lines = [_skill("t1", "gog-demo"), _human("next"), _bash("b1", "gog calendar"), _result("b1")]
    _, led, *_ = _record(tmp_path, home, lines)
    assert [e["event"] for e in led.events()] == ["served"]


def test_rereading_is_idempotent_even_without_index(tmp_path, home):
    lines = [_skill("t1", "gog-demo"), _bash("b1", "gog calendar"), _result("b1")]
    n, led, t, base = _record(tmp_path, home, lines)
    assert n == 2
    assert sc.record_transcript(t, base, home=home, host="h") == 0
    (base / "index.db").unlink()  # derived store lost: readers still dedupe by id
    sc.record_transcript(t, base, home=home, host="h")
    assert len(led.events()) == 2
    assert led.reindex() == 2


def test_new_turns_append_only_new_events(tmp_path, home):
    lines = [_skill("t1", "gog-demo")]
    _, led, t, base = _record(tmp_path, home, lines)
    _write(t, lines + [_human("more"), _skill("t2", "gog-demo")])
    assert sc.record_transcript(t, base, home=home, host="h") == 1
    assert len(led.events()) == 2


def test_no_transcript_text_is_stored(tmp_path, home):
    lines = [_human(), _skill("t1", "gog-demo"), _bash("b1", f"gog {SECRET}"), _result("b1")]
    _, _, _, base = _record(tmp_path, home, lines)
    blob = b"".join(p.read_bytes() for p in (base / "ledger").glob("*.jsonl"))
    assert SECRET.encode() not in blob


def test_transcript_without_skill_is_a_fast_noop(tmp_path, home):
    n, _, _, base = _record(tmp_path, home, [_human(), _bash("b1", "gog x")])
    assert n == 0 and not (base / "ledger").exists()
    assert sc.scan_transcript(tmp_path / "missing.jsonl") == []


def test_sidechain_lines_are_ignored(tmp_path, home):
    ln = _skill("t1", "gog-demo")
    ln["isSidechain"] = True
    n, *_ = _record(tmp_path, home, [ln])
    assert n == 0


# Stop hook ------------------------------------------------------------------


@pytest.fixture
def agent(tmp_path, home, monkeypatch):
    skc = tmp_path / "skc"
    (skc / "agents" / "alice" / "config").mkdir(parents=True)
    (skc / "agents" / "alice" / "config" / "skmemory.yaml").write_text("name: alice\n")
    monkeypatch.setenv("SKCAPSTONE_HOME", str(skc))
    for v in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT", "SK_DEFAULT_AGENT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(hook_cli.tempfile, "gettempdir", lambda: str(tmp_path))
    return skc / "agents" / "alice"


def _stop(tmp_path, harness="claude"):
    t = _write(
        tmp_path / "t.jsonl", [_skill("t1", "gog-demo"), _bash("b1", "gog x"), _result("b1")]
    )
    payload = json.dumps({"session_id": "s", "transcript_path": str(t)})
    out = io.StringIO()
    assert hook_cli.run_hook("stop", harness, stdin=io.StringIO(payload), out=out) == 0


def _count(agent_home):
    return len(Ledger(agent_home / "memory").events())


def test_stop_hook_feeds_ledger_every_stop_and_is_idempotent(tmp_path, agent):
    _stop(tmp_path)
    assert _count(agent) == 2
    _stop(tmp_path)
    assert _count(agent) == 2


@pytest.mark.parametrize(
    "var,val",
    [("SKMEMORY_READER", "casey"), ("SKMEMORY_LEDGER", "off"), ("SKMEMORY_HOOKS", "off")],
)
def test_stop_hook_honors_reader_and_kill_switches(tmp_path, agent, monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    _stop(tmp_path)
    assert not (agent / "memory" / "ledger").exists()


def test_stop_hook_other_harness_and_failure_never_break(tmp_path, agent, monkeypatch):
    _stop(tmp_path, harness="generic")
    assert not (agent / "memory" / "ledger").exists()
    monkeypatch.setattr(sc, "record_transcript", lambda *a, **k: 1 / 0)
    _stop(tmp_path)  # must not raise


# Hermes import ----------------------------------------------------------------

USAGE = {
    "daily-sk-review": {
        "archived_at": None,
        "created_at": "2026-06-01T00:00:00+00:00",
        "created_by": "agent",
        "last_patched_at": None,
        "last_used_at": "2026-11-05T10:00:00+00:00",
        "last_viewed_at": "2026-11-05T10:00:00+00:00",
        "patch_count": 7,
        "pinned": False,
        "state": "active",
        "use_count": 3,
        "view_count": 4,
    },
    "odd": {  # use > view, and a newer-shape entry
        "created_at": "2026-06-01T00:00:00+00:00",
        "created_by": None,
        "first_seen_at": None,
        "last_used_at": "2026-10-01T00:00:00+00:00",
        "last_viewed_at": None,
        "patch_count": 0,
        "patch_generation": 0,
        "state": "active",
        "use_count": 2,
        "view_count": 1,
    },
    "never": {
        "use_count": 0,
        "view_count": 0,
        "last_used_at": None,
        "created_at": "2026-06-01T00:00:00+00:00",
    },
}


def test_hermes_import_shape_idempotent_and_read_only(tmp_path):
    u = tmp_path / ".usage.json"
    u.write_text(json.dumps(USAGE))
    os.chmod(u, 0o400)
    before = (u.read_bytes(), u.stat().st_mtime_ns)
    led = Ledger(tmp_path / "mem", host="h")
    st = sc.import_hermes_usage(u, led)
    assert st == {"skills": 2, "served": 6, "followed": 5, "skipped": 1}
    assert sc.import_hermes_usage(u, led)["served"] == 0
    assert len(led.events()) == 11
    s = led.score("daily-sk-review", "patch-7")
    assert s.net_effect_sign == "0"
    assert led.score("odd", "patch-0").imported_served == 2  # served raised to use
    assert all(e["effect"] is None and e["source"] == sc.SRC_HERMES for e in led.events())
    assert (s.imported_served, s.imported_followed) == (4, 3) and s.served == 0
    assert (u.read_bytes(), u.stat().st_mtime_ns) == before


def test_hermes_import_picks_up_only_new_uses(tmp_path):
    u = tmp_path / ".usage.json"
    u.write_text(json.dumps(USAGE))
    led = Ledger(tmp_path / "mem", host="h")
    sc.import_hermes_usage(u, led)
    USAGE2 = json.loads(json.dumps(USAGE))
    USAGE2["daily-sk-review"].update(
        use_count=4, view_count=5, last_used_at="2026-11-06T00:00:00+00:00"
    )
    u.write_text(json.dumps(USAGE2))
    assert sc.import_hermes_usage(u, led) == {
        "skills": 2,
        "served": 1,
        "followed": 1,
        "skipped": 1,
    }


def test_hermes_import_cli(tmp_path, monkeypatch):
    u = tmp_path / ".usage.json"
    u.write_text(json.dumps(USAGE))
    base = tmp_path / "mem"
    r = CliRunner().invoke(
        cli, ["ledger", "import-hermes", "--usage", str(u), "--base", str(base)]
    )
    assert r.exit_code == 0 and "6 served + 5 followed" in r.output
    monkeypatch.setenv("SKMEMORY_READER", "casey")
    r = CliRunner().invoke(
        cli, ["ledger", "import-hermes", "--usage", str(u), "--base", str(tmp_path / "x")]
    )
    assert r.exit_code == 1 and not (tmp_path / "x").exists()


# Installer (temp home only) -----------------------------------------------------


def test_installer_wires_the_stop_hook_on_a_temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    hook_install.install("claude")
    cfg = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
    assert any("hook stop --harness claude" in c for c in cmds)
    assert all(str(tmp_path) not in c or "skmemory" in c for c in cmds)


def test_ledger_event_metadata_roundtrip(tmp_path):
    led = Ledger(tmp_path, host="h")
    a = led.record("s", "1", "followed", event_id="x" * 8, source="src", missing="why")
    b = led.record("s", "1", "followed", event_id="x" * 8, source="src", missing="why")
    assert a == b and len(led.events()) == 1
    assert led.events()[0]["source"] == "src"
