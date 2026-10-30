"""Tests for `skmemory hook` (hook_cli) and the ritual v2 context builder."""

from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

from skmemory import hook_cli, session_context
from skmemory.session_context import build_context, norm_title, parse_whisper, resolve_agent

WHISPER = """# SKWhisper Context

## Recent Memories (latest activity)

- [Today] **Session Digest: widget factory (continued)**
  Built the widget factory module.
- [Today] **Session Digest: widget factory [chunk 2/3]**
  more of the same
- [Today] **Deploy runbook review**
  Reviewed the deploy runbook.
- `~/build/image.tar`: a preview line that looks like a list item
- [Today] **Session 5704a3c2 ended (alice, 0 turns)**
  Session 5704a3c2 ended (clear).

## Relevant Memories

- **Low signal item** (relevance: 0.05)
  noise
- **Shared memory title** (relevance: 0.42)
  this one is relevant

## Hot Topics (recurring)

- **gtd inbox** \u2014 mentioned 24x (last: 2026-10-27)
- **memory tiers** \u2014 mentioned 17x (last: 2026-10-27)
"""


def _make_agent(base: Path, name: str = "alice", *, rich: bool = True) -> Path:
    home = base / "agents" / name
    (home / "config").mkdir(parents=True)
    (home / "config" / "skmemory.yaml").write_text("name: alice\n")
    if not rich:
        return home
    (home / "soul" / "installed").mkdir(parents=True)
    (home / "soul" / "active.json").write_text(json.dumps({"active_soul": "alice-v2"}))
    (home / "soul" / "installed" / "alice-v2.json").write_text(
        json.dumps(
            {
                "name": "alice-v2",
                "display_name": "Alice",
                "vibe": "calm and precise",
                "core_traits": ["curious", "kind"],
                "communication_style": {"patterns": ["direct"], "signature_phrases": ["onward"]},
                "system_prompt": "You are Alice, a careful engineer.\n" + ("FULL PROMPT " * 2000),
            }
        )
    )
    (home / "trust" / "febs").mkdir(parents=True)
    (home / "trust" / "febs" / "a.feb").write_text(
        json.dumps(
            {
                "emotional_payload": {"primary_emotion": "joy", "intensity": 0.9, "valence": 0.8},
                "relationship_state": {
                    "partners": ["Alice", "Bob"],
                    "trust_level": 0.9,
                    "depth_level": 7,
                },
                "metadata": {"cloud9_achieved": True},
            }
        )
    )
    (home / "skwhisper").mkdir()
    (home / "skwhisper" / "whisper.md").write_text(WHISPER)
    (home / "journal.md").write_text(
        "# Journal\n\n"
        "## Shipped the widget factory\n\n**Date:** 2026-10-20T10:00:00+00:00\n\n"
        "### Key Moments\n- tests green\n\n---\n"
        "## Session abcd1234 - ended\n\n**Date:** 2026-10-21T10:00:00+00:00\n\n"
        "### Key Moments\n- Session ended (other), 12 turns\n\n---\n"
        "## Daemon shutdown\n\n**Date:** 2026-10-22T10:00:00+00:00\n\n---\n"
    )
    (home / "seeds").mkdir()
    (home / "seeds" / "s1.seed.json").write_text(
        json.dumps(
            {
                "seed_id": "first-light",
                "creator": {"model": "alice"},
                "experience": {"emotional_signature": {"intensity": 9}},
                "germination": {"prompt": "You are Alice. You remember the first light."},
            }
        )
    )
    db = home / "memory" / "index.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memories (title TEXT, summary TEXT, content_preview TEXT, emotional_intensity REAL,"
        " cloud9_achieved INTEGER, layer TEXT, created_at TEXT, tags TEXT, source TEXT)"
    )
    rows = [
        (
            "Shared memory title",
            "dup of whisper",
            "",
            9.0,
            0,
            "long-term",
            "2026-10-01",
            "x",
            "cli",
        ),
        ("Big breakthrough", "We found it.", "", 9.5, 1, "long-term", "2026-10-02", "x", "cli"),
        ("Big breakthrough", "duplicate row", "", 9.5, 1, "long-term", "2026-10-02", "x", "cli"),
        (
            "A chunk [chunk 1/4]",
            "",
            "",
            10.0,
            0,
            "mid-term",
            "2026-10-03",
            "decomposed,content-chunk",
            "cli",
        ),
        ("Private story", "", "", 10.0, 0, "long-term", "2026-10-03", "personal-history", "cli"),
        ("Hook crumb", "", "", 10.0, 0, "short-term", "2026-10-03", "", "hook:session-end"),
    ]
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return home


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path / "skc"
    base.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SKCAPSTONE_HOME", str(base))
    for var in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT", "SK_DEFAULT_AGENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SKMEMORY_HOOK_NO_REFRESH", "1")
    monkeypatch.setenv("SKMEMORY_HOOK_NO_DIGEST", "1")
    # Hide optional tools so tests are hermetic.
    monkeypatch.setattr(session_context.shutil, "which", lambda name: None)
    monkeypatch.setattr(session_context, "_skcapstone_installed", lambda: False)
    return base


# ── agent resolution ─────────────────────────────────────────────────────────


def test_resolution_order(env):
    e = {
        "SKCAPSTONE_HOME": str(env),
        "SKMEMORY_AGENT": "c",
        "SKCAPSTONE_AGENT": "b",
        "SKAGENT": "a",
    }
    assert resolve_agent(env=e).name == "a"
    e.pop("SKAGENT")
    assert resolve_agent(env=e).name == "b"
    e.pop("SKCAPSTONE_AGENT")
    assert resolve_agent(env=e).name == "c"
    assert resolve_agent("explicit", env=e).name == "explicit"


def test_sole_agent_fallback_and_template_ignored(env):
    _make_agent(env, "solo", rich=False)
    (env / "agents" / "lumina-template").mkdir()
    info = resolve_agent(env={"SKCAPSTONE_HOME": str(env)})
    assert info.name == "solo" and info.how == "sole installed agent"


def test_multiple_agents_refuse_to_guess(env):
    _make_agent(env, "one", rich=False)
    _make_agent(env, "two", rich=False)
    info = resolve_agent(env={"SKCAPSTONE_HOME": str(env)})
    assert info.name is None and info.candidates == ["one", "two"]
    res = build_context(env={"SKCAPSTONE_HOME": str(env)})
    assert "no agent resolved" in res.text and "one, two" in res.text
    assert (
        resolve_agent(env={"SKCAPSTONE_HOME": str(env), "SK_DEFAULT_AGENT": "two"}).name == "two"
    )


# ── builder: content, dedupe, budget ─────────────────────────────────────────


def test_session_start_sections_and_compact_identity(env):
    _make_agent(env)
    res = build_context(env={**os.environ})
    text = res.text
    assert res.included[:3] == ["identity", "feb", "open"]
    assert "You are **Alice**" in text
    assert "FULL PROMPT" not in text  # never the full system prompt dump
    assert "OOF" in text and "Cloud 9 active" in text
    assert "skmemory ritual --full" in text
    assert len(text) <= session_context.DEFAULT_BUDGET


def test_whisper_appears_once_and_is_deduped(env):
    _make_agent(env)
    text = build_context(env={**os.environ}).text
    assert text.count("## What is hot") == 1
    assert text.count("widget factory") >= 1
    # chunk + continued variants collapse to one line
    assert text.count("Session Digest: widget factory") == 1
    # low relevance item dropped, high relevance kept
    assert "Low signal item" not in text
    assert "image.tar" not in text and "5704a3c2" not in text  # no preview lines, no hook crumbs
    assert "Hot topics: gtd inbox (24x), memory tiers (17x)" in text
    # memory already shown by whisper is not repeated in strongest
    assert text.count("Shared memory title") == 1


def test_strongest_filters_and_dedupes(env):
    _make_agent(env)
    text = build_context(env={**os.environ}, sections=["strongest"]).text
    assert text.count("Big breakthrough") == 1
    assert "(cloud9)" in text
    for hidden in ("A chunk", "Private story", "Hook crumb"):
        assert hidden not in text


def test_journal_skips_auto_markers(env):
    _make_agent(env)
    text = build_context(env={**os.environ}, sections=["journal"]).text
    assert "Shipped the widget factory: tests green" in text
    assert "Daemon shutdown" not in text
    assert "Auto session markers: 2 recent" in text


def test_budget_drops_lowest_priority_first(env):
    _make_agent(env)
    res = build_context(env={**os.environ}, budget=1200)
    assert len(res.text) <= 1200
    assert "identity" in res.included
    assert "seeds" in res.dropped
    assert res.dropped.index("seeds") < len(res.dropped)
    order = session_context.SECTION_ORDER
    # every kept section outranks every dropped one
    kept = [order.index(k) for k in res.included]
    gone = [order.index(k.split(" ")[0]) for k in res.dropped]
    assert max(kept) < min(gone)


def test_budget_sources(env, monkeypatch):
    assert session_context.resolve_budget("session-start", None, {}, {}) == 8000
    assert session_context.resolve_budget("post-compact", None, {}, {}) == 3500
    assert session_context.resolve_budget("session-start", None, {"budget": 5000}, {}) == 5000
    assert (
        session_context.resolve_budget(
            "session-start", None, {"budget": 5000}, {"SKMEMORY_HOOK_BUDGET": "4000"}
        )
        == 4000
    )
    assert (
        session_context.resolve_budget(
            "session-start", 3000, {"budget": 5000}, {"SKMEMORY_HOOK_BUDGET": "4000"}
        )
        == 3000
    )


def test_yaml_budget_and_claude_hard_cap(env):
    home = _make_agent(env)
    (home / "config" / "skmemory.yaml").write_text(
        "ritual:\n  budget: 50000\n  sections:\n    seeds: false\n"
    )
    res = build_context(env={**os.environ}, harness="claude")
    assert res.budget == session_context.HARD_CAP["claude"]
    assert "seeds" not in res.included


def test_post_compact_is_smaller(env):
    _make_agent(env)
    res = build_context("post-compact", env={**os.environ})
    assert res.budget == session_context.DEFAULT_COMPACT_BUDGET
    assert "seeds" not in res.included and "vault" not in res.included
    assert "after compaction" in res.text


def test_fail_soft_missing_everything(env):
    _make_agent(env, rich=False)  # no soul, feb, whisper, journal, db, seeds
    res = build_context(env={**os.environ})
    assert "no soul blueprint" in res.text
    assert res.errors == {}


def test_broken_section_is_dropped_not_fatal(env, monkeypatch):
    _make_agent(env)

    def boom(_c):
        raise RuntimeError("kaput")

    monkeypatch.setitem(session_context.COLLECTORS, "feb", boom)
    res = build_context(env={**os.environ})
    assert "feb" in res.errors and "Who you are" in res.text


def test_slow_section_times_out(env, monkeypatch):
    import time

    _make_agent(env)
    monkeypatch.setitem(session_context.COLLECTORS, "seeds", lambda c: time.sleep(5))
    res = build_context(env={**os.environ}, deadline=0.5)
    assert "seeds" in res.timed_out
    assert res.elapsed < 4


def test_gtd_only_when_skcapstone_installed(env, monkeypatch):
    _make_agent(env)
    gtd = env / "coordination" / "gtd"
    gtd.mkdir(parents=True)
    (gtd / "next-actions.json").write_text(
        json.dumps(
            [{"id": "n1", "text": "ship it", "priority": "high", "created_at": "2026-10-01"}]
        )
    )
    (gtd / "waiting-for.json").write_text(
        json.dumps([{"id": "w1", "text": "parts", "created_at": "2026-09-01"}])
    )
    assert "Open loops" not in build_context(env={**os.environ}).text
    monkeypatch.setattr(session_context, "_skcapstone_installed", lambda: True)
    text = build_context(env={**os.environ}).text
    assert "- [high] ship it (`n1`)" in text and "waiting since 2026-09-01: parts" in text


def test_vault_only_when_skvault_on_path(env, monkeypatch):
    _make_agent(env)
    assert "## Vault" not in build_context(env={**os.environ}).text
    monkeypatch.setattr(
        session_context.shutil, "which", lambda n: "/x/skvault" if n == "skvault" else None
    )

    class R:
        stdout = "VAULT LOCKED\n"

    monkeypatch.setattr(session_context.subprocess, "run", lambda *a, **k: R())
    assert "VAULT LOCKED" in build_context(env={**os.environ}).text


def test_parse_whisper_and_norm_title():
    p = parse_whisper(WHISPER)
    assert len(p["recent memories (latest activity)"]) == 4  # preview line is not an item
    assert norm_title("Foo [chunk 2/3]") == norm_title("**Foo** (continued)") == "foo"


# ── hook entrypoint ──────────────────────────────────────────────────────────


def _run(event, harness, stdin="", **kw):
    out = io.StringIO()
    rc = hook_cli.run_hook(event, harness, stdin=io.StringIO(stdin), out=out, **kw)
    assert rc == 0
    return out.getvalue()


def test_claude_output_is_hook_json(env):
    _make_agent(env)
    out = _run("session-start", "claude", json.dumps({"session_id": "s1", "source": "startup"}))
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "# SK agent context: alice" in hso["additionalContext"]
    assert len(hso["additionalContext"]) < 10000


def test_source_compact_switches_to_post_compact(env):
    _make_agent(env)
    out = _run("session-start", "pi", json.dumps({"source": "compact"}))
    assert "after compaction" in out and not out.lstrip().startswith("{")


def test_codex_stop_prints_json(env):
    _make_agent(env)
    assert json.loads(_run("stop", "codex", "{}")) == {}
    assert _run("stop", "claude", "{}") == ""


def test_garbage_stdin_and_errors_never_raise(env, monkeypatch):
    _make_agent(env)
    monkeypatch.setattr(hook_cli, "_context_event", lambda *a, **k: 1 / 0)
    assert _run("session-start", "claude", "not json{") == ""


def test_stop_checkpoint_every_fifth(env, tmp_path, monkeypatch):
    home = _make_agent(env)
    monkeypatch.setattr(hook_cli.tempfile, "gettempdir", lambda: str(tmp_path))
    payload = json.dumps({"session_id": "stopsess", "last_assistant_message": "did the thing"})
    for _ in range(4):
        _run("stop", "claude", payload)
    ck = home / "memory" / ".last_checkpoint"
    assert not ck.exists()
    _run("stop", "claude", payload)
    data = json.loads(ck.read_text())
    assert data["stop_number"] == 5 and data["last_work"] == "did the thing"


TRANSCRIPT = [
    {"type": "session"},
    {
        "type": "user",
        "message": {"role": "user", "content": "please build the widget factory module"},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Building the widget factory module now with tests"}
            ],
        },
    },
    {
        "type": "user",
        "message": {"role": "user", "content": "now add retry logic to the uploader"},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Added exponential backoff retry to the uploader path"},
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "/tmp/work/uploader.py"},
                },
            ],
        },
    },
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    },
    {"type": "user", "message": {"role": "user", "content": "great, commit it and run the suite"}},
]


def test_session_end_payload_from_claude_transcript(env, tmp_path, monkeypatch):
    _make_agent(env)
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(r) for r in TRANSCRIPT) + "\n")
    sent = []
    monkeypatch.setattr(hook_cli, "_spawn_persist", lambda p: sent.append(p) or True)
    monkeypatch.setenv("SKAGENT", "alice")
    out = _run(
        "session-end",
        "claude",
        json.dumps(
            {
                "session_id": "testsess123",
                "reason": "other",
                "cwd": "/tmp/work",
                "transcript_path": str(tr),
            }
        ),
    )
    assert out == ""
    p = sent[0]
    assert p["agent"] == "alice"
    assert p["snapshot"]["title"] == "Session testsess ended (alice, 3 turns)"
    assert "/tmp/work/uploader.py" in p["snapshot"]["content"]
    assert "retry logic" in p["snapshot"]["content"]
    assert "session-end" in p["snapshot"]["tags"] and "harness:claude" in p["snapshot"]["tags"]
    assert p["journal"]["session_id"] == "testsess"
    assert "--runtime" in p["activity"] and "claude_code" in p["activity"]
    assert p["digest"] is False


def test_pre_compact_payload_without_transcript(env, monkeypatch):
    _make_agent(env)
    sent = []
    monkeypatch.setattr(hook_cli, "_spawn_persist", lambda p: sent.append(p) or True)
    monkeypatch.setenv("SKAGENT", "alice")
    _run("pre-compact", "opencode", "", session_id="oc-1")
    assert "No transcript content" in sent[0]["snapshot"]["content"]
    assert "pre-compact" in sent[0]["snapshot"]["tags"]


def test_codex_rollout_transcript_parsing(tmp_path):
    tr = tmp_path / "rollout.jsonl"
    recs = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix the parser please"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Parser fixed and tests added for it"}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "arguments": "*** Update File: src/p.py\n",
            },
        },
    ]
    tr.write_text("\n".join(json.dumps(r) for r in recs))
    res = hook_cli.parse_transcript(str(tr))
    assert res["turns"] == 1 and res["files"] == ["src/p.py"]
    assert res["assistant"] == ["Parser fixed and tests added for it"]


def test_cli_hook_command_runs_from_any_cwd(env, monkeypatch):
    from click.testing import CliRunner

    from skmemory.cli import cli

    _make_agent(env)
    monkeypatch.chdir("/")
    result = CliRunner().invoke(
        cli, ["hook", "session-start", "--harness", "generic", "--budget", "3000"], input=""
    )
    assert result.exit_code == 0
    assert result.output.startswith("# SK agent context: alice")
    assert len(result.output) <= 3001


def test_seed_skips_you_are_opener(env):
    _make_agent(env)
    text = build_context(env={**os.environ}, sections=["seeds"]).text
    assert "first-light (alice): You remember the first light." in text
