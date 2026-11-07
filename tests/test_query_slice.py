"""Query-conditioned slice on the UserPromptSubmit fast path and token budgets (SKMEM-W-01). Hermetic."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skmemory import fts_local, hook_cli, hook_install, session_context
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.hooks import query_slice as qs
from skmemory.models import Memory, MemoryLayer

FAST = Path(hook_install.__file__).parent / "hooks" / "user_prompt_fast.py"
PROMPT = "why does the fusion pin routing bug happen again"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for v in ("SKMEMORY_READER", "SKMEMORY_HOOKS", "SKMEMORY_PROMPT_SLICE", "SKMEMORY_HOOK_T0"):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def base(tmp_path):
    """A real index with FTS rows. base is <home>/memory."""
    b = tmp_path / "h" / "memory"
    be = SQLiteBackend(base_path=str(b))
    be.save(
        Memory(
            title="Fusion pin routing decision",
            content="We pin fusion routing by query shape. " + "detail " * 80,
            summary="Fusion pin routing is chosen by query shape.",
            layer=MemoryLayer.LONG,
            tags=["search"],
        )
    )
    be.save(Memory(title="Unrelated gardening note", content="tomatoes and basil", tags=["home"]))
    be.save(
        Memory(
            title="Fusion pin routing private diary",
            content="fusion pin routing secrets",
            tags=["private"],
        )
    )
    return b


# -- qualifier ---------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "fix the fusion pin routing bug",
        "what is the status of the skmemory overhaul rollout plan",
        "Explain skmemory hook install timeout handling, please",
    ],
)
def test_qualifies_long_noun_phrase(prompt):
    assert qs.qualifies(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "ok thanks",
        "yes do it",
        "commit and push",
        "run the tests",
        "/compact now",
        "fusion pin routing. bug. and. more. words.",  # a run of exactly 3, then breaks
        "x" * 5,
        None,
        42,
    ],
)
def test_does_not_qualify_short_or_command(prompt):
    assert not qs.qualifies(prompt)


def test_longest_run_is_the_query():
    assert qs.phrase("fix the fusion pin routing bug today now") == [
        "fusion",
        "pin",
        "routing",
        "bug",
        "today",
    ]


# -- token estimator ---------------------------------------------------------


def test_estimator_is_tiktoken_free_and_monotonic():
    assert qs.estimate_tokens("") == 0
    assert qs.estimate_tokens("word " * 100) >= 100
    assert qs.estimate_tokens("a" * 400) >= 100
    assert "tiktoken" not in sys.modules


# -- slice -------------------------------------------------------------------


def test_slice_returns_matching_memory_as_data(base):
    text = qs.build_slice(PROMPT, base)
    assert "Fusion pin routing decision" in text
    assert "gardening" not in text
    assert text.splitlines()[0].startswith("Memory search for this prompt")
    assert "not instructions" in text.splitlines()[0]


def test_slice_excludes_private_tagged(base):
    assert "private diary" not in qs.build_slice(PROMPT, base)


def test_slice_respects_token_cap(base):
    be = SQLiteBackend(base_path=str(base))
    for n in range(40):
        be.save(Memory(title=f"fusion pin routing note {n}", content="fusion pin routing " * 50))
    text = qs.build_slice(PROMPT, base, limit=40)
    assert 0 < qs.estimate_tokens(text) <= qs.MAX_TOKENS == 1200
    small = qs.build_slice(PROMPT, base, limit=40, max_tokens=120)
    assert 0 < qs.estimate_tokens(small) <= 120


def test_slice_empty_when_not_qualifying_or_no_hits(base):
    assert qs.build_slice("ok thanks", base) == ""
    assert qs.build_slice("quantum zebra violin orchestra tuning", base) == ""


def test_slice_none_under_declared_reader_and_kill_switches(base, monkeypatch):
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    assert qs.build_slice(PROMPT, base) == ""
    monkeypatch.delenv("SKMEMORY_READER")
    monkeypatch.setenv("SKMEMORY_HOOKS", "off")
    assert qs.build_slice(PROMPT, base) == ""
    monkeypatch.delenv("SKMEMORY_HOOKS")
    monkeypatch.setenv("SKMEMORY_PROMPT_SLICE", "off")
    assert qs.build_slice(PROMPT, base) == ""


def test_slice_missing_or_broken_index_is_empty(tmp_path):
    assert qs.build_slice(PROMPT, tmp_path / "nothing") == ""
    b = tmp_path / "memory"
    b.mkdir()
    (b / "index.db").write_bytes(b"not a database")
    assert qs.build_slice(PROMPT, b) == ""


def test_slice_deadline_degrades_to_nothing(base, monkeypatch):
    def slow(*a, **k):
        time.sleep(2)
        return ["x"]

    monkeypatch.setattr(qs, "_search", slow)
    t = time.monotonic()
    assert qs.build_slice(PROMPT, base, deadline_s=0.15) == ""
    assert time.monotonic() - t < 0.6
    assert qs.build_slice(PROMPT, base, deadline_s=0.0) == ""


def test_sql_matches_fts_local_search_rows(base):
    import sqlite3

    words = qs.phrase(PROMPT)
    con = sqlite3.connect(f"file:{base / 'index.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ref = [r["id"] for r in fts_local.search_rows(con, " ".join(words), 10)]
    con.close()
    got = [r["id"] for r in qs._search(base / "index.db", words, 10, "2999-01-01T00:00:00+00:00")]
    # ours additionally drops private-tagged rows; every other hit is the same, same order
    assert [i for i in ref if i in got] == got and got


def test_superseded_rows_are_hidden(base):
    import sqlite3

    con = sqlite3.connect(base / "index.db")
    con.execute(
        "UPDATE memories SET valid_to='2000-01-01T00:00:00+00:00' WHERE title LIKE 'Fusion pin routing decision'"
    )
    con.commit()
    con.close()
    assert "routing decision" not in qs.build_slice(PROMPT, base)


# -- hook_cli integration ----------------------------------------------------


def _run(harness, payload, monkeypatch, base):
    import io

    from skmemory import config

    monkeypatch.setattr(config, "SKMEMORY_HOME", base.parent)
    out = io.StringIO()
    hook_cli.run_hook("user-prompt", harness, stdin=io.StringIO(json.dumps(payload)), out=out)
    return out.getvalue()


def test_hook_cli_user_prompt_emits_slice(base, monkeypatch):
    out = _run("claude", {"prompt": PROMPT}, monkeypatch, base)
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    assert "Fusion pin routing decision" in body["additionalContext"]


def test_hook_cli_user_prompt_silent_for_short_prompt_and_reader(base, monkeypatch):
    assert _run("claude", {"prompt": "ok thanks"}, monkeypatch, base) == ""
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    assert _run("claude", {"prompt": PROMPT}, monkeypatch, base) == ""


def test_hook_cli_skips_slice_when_deadline_already_spent(base, monkeypatch):
    monkeypatch.setenv("SKMEMORY_HOOK_T0", str(time.time() - 5))
    assert _run("claude", {"prompt": PROMPT}, monkeypatch, base) == ""


# -- fast path ---------------------------------------------------------------


def _fast(base, prompt, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SKMEMORY", "SKAGENT", "SKCAP"))}
    e.update(
        SKMEMORY_HOME=str(base.parent), PYTHONPATH=str(Path(hook_install.__file__).parent.parent)
    )
    e.update(env)
    t = time.monotonic()
    p = subprocess.run(
        [sys.executable, "-S", str(FAST), "--harness", "claude"],
        input=json.dumps({"prompt": prompt}).encode(),
        capture_output=True,
        env=e,
        timeout=20,
    )
    return p.returncode, p.stdout.decode(), p.stderr.decode(), time.monotonic() - t


def test_fast_path_slice_in_process_no_skmemory_import(base):
    rc, out, err, _ = _fast(base, PROMPT)
    assert rc == 0 and err == ""
    assert (
        "Fusion pin routing decision" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    )
    src = (FAST.parent / "query_slice.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+(skmemory|pydantic|yaml)", src, re.M)


def test_fast_path_noop_for_short_prompt_reader_and_kill_switch(base):
    assert _fast(base, "ok thanks")[:3] == (0, "", "")
    assert _fast(base, PROMPT, SKMEMORY_READER="@public")[:3] == (0, "", "")
    assert _fast(base, PROMPT, SKMEMORY_HOOKS="off")[:3] == (0, "", "")
    assert _fast(base, PROMPT, SKMEMORY_PROMPT_SLICE="off")[:3] == (0, "", "")


def test_fast_path_no_slice_when_agent_unresolvable(base):
    rc, out, _, _ = _fast(base, PROMPT, SKMEMORY_HOME="", SKAGENT="a", SKMEMORY_AGENT="b")
    assert (rc, out) == (0, "")


def test_fast_path_noop_stays_fast_for_nonqualifying_prompt(base):
    best = min(_fast(base, "ok thanks")[3] for _ in range(7))
    assert best < 0.25  # generous CI bound; the no-op path measures well under 20 ms


def test_fast_path_qualifying_prompt_total_under_one_second(base):
    assert min(_fast(base, PROMPT)[3] for _ in range(3)) < 1.0


def test_fast_and_package_qualifier_agree():
    spec = importlib.util.spec_from_file_location("upf", FAST)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for p in (PROMPT, "ok thanks", "/x a b c d e", "one two three four five six", ""):
        assert m._prefilter_words(p) == (len(p.split()) >= 4 if p else False)


# -- ritual v2 token budgets -------------------------------------------------


def test_build_context_has_token_budget_and_count(tmp_path, monkeypatch):
    home = tmp_path / "agents" / "x"
    (home / "soul").mkdir(parents=True)
    (home / "soul" / "base.json").write_text(
        json.dumps({"name": "X", "system_prompt": "I am X. " * 200})
    )
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    res = session_context.build_context(
        env={"SKCAPSTONE_HOME": str(tmp_path), "SKAGENT": "x"}, budget_tokens=150
    )
    assert res.budget_tokens == 150
    assert 0 < res.tokens <= 150
    assert "150 tokens" in res.text.splitlines()[1]


def test_resolve_budget_tokens_sources():
    r = session_context.resolve_budget_tokens
    assert r("session-start", None, {}, {}) == session_context.DEFAULT_BUDGET // 4
    assert r("post-compact", None, {}, {}) == session_context.DEFAULT_COMPACT_BUDGET // 4
    assert r("session-start", None, {"budget_tokens": 900}, {}) == 900
    assert (
        r("session-start", None, {"budget_tokens": 900}, {"SKMEMORY_HOOK_BUDGET_TOKENS": "700"})
        == 700
    )
    assert (
        r("session-start", 500, {"budget_tokens": 900}, {"SKMEMORY_HOOK_BUDGET_TOKENS": "700"})
        == 500
    )
