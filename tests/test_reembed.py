"""SKMEM-P1-07: the shared embed text and the reembed tool (hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory import reembed as rb
from skmemory.embed_text import EMBED_TEXT_MAX_CHARS, embed_text, legacy_reconcile_text


def test_embed_text_formula_and_truncation():
    assert embed_text("T", "C", "S") == "T\nC\nS"
    assert embed_text("T", "C", None) == "T\nC"
    assert embed_text(None, "C", "") == "C"
    assert len(embed_text("T", "x" * 5000, "S")) == EMBED_TEXT_MAX_CHARS


def test_save_and_reconcile_share_the_formula():
    from skmemory.backends.pgvector_backend import PGVectorBackend
    from skmemory.models import Memory

    mem = Memory(title="A title", content="body " * 400, summary="short")
    assert PGVectorBackend._searchable(mem) == embed_text(mem.title, mem.content, mem.summary)
    src = Path(rb.__file__).with_name("reconcile.py").read_text()
    assert "[:1100]" not in src, "reconcile must not keep its own truncation"
    assert src.count("embed_text(") >= 3  # backfill, content-changed, null-vector paths


def test_the_old_formula_really_differed():
    assert legacy_reconcile_text("body", "title") == "body"
    assert embed_text("title", "body") != legacy_reconcile_text("body", "title")


class _Cur:
    def __init__(self, db):
        self.db, self.rowcount = db, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.sql.append((sql, params))
        if sql.startswith("SELECT"):
            agent, last, n = params
            rows = [r for r in self.db.rows if r[0] > last and self.db.agents[r[0]] == agent]
            self._rows = sorted(rows)[:n]
        else:
            vec, mid, agent, digest = params
            row = next(r for r in self.db.rows if r[0] == mid)
            ok = (
                self.db.agents[mid] == agent
                and rb.text_digest(*row[1:4]) == digest
                and mid not in self.db.changed
            )
            self.rowcount = 1 if ok else 0
            if ok:
                self.db.embeddings[mid] = vec

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, agents):
        self.rows, self.agents = rows, agents
        self.sql, self.embeddings, self.changed = [], {}, set()

    def cursor(self):
        return _Cur(self)


def _db():
    rows = [(f"id{i:02d}", f"title {i}", f"content {i}", "") for i in range(10)]
    agents = {r[0]: "lumina" for r in rows}
    agents["id03"] = "other"
    return _Conn(rows, agents)


def test_dry_run_writes_nothing_and_never_embeds():
    conn, calls = _db(), []
    rep = rb.reembed(conn, "lumina", None, apply=False, batch_size=4)
    assert rep["applied"] is False and rep["stale_formula"] == 9 and rep["updated"] == 0
    assert all(sql.startswith("SELECT") for sql, _ in conn.sql)
    assert calls == [] and conn.embeddings == {}


def test_apply_without_embedder_is_refused():
    with pytest.raises(rb.ReembedRefused):
        rb.reembed(_db(), "lumina", None, apply=True)


def test_agent_is_mandatory():
    with pytest.raises(rb.ReembedRefused):
        rb.reembed(_db(), "", lambda t: [0.0], apply=False)


def test_apply_writes_only_the_embedding_column_for_that_agent(tmp_path):
    conn = _db()
    rep = rb.reembed(
        conn,
        "lumina",
        lambda t: [1.0, 2.0],
        apply=True,
        state_path=tmp_path / "s.json",
        batch_size=4,
        rows_per_sec=0,
    )
    assert rep["updated"] == 9 and "id03" not in conn.embeddings
    updates = [s for s, _ in conn.sql if s.startswith("UPDATE")]
    assert updates and all(
        s.startswith("UPDATE memories SET embedding = %s::vector WHERE") for s in updates
    )
    assert not any("title =" in s or "content =" in s for s in updates)


def test_a_row_changed_since_it_was_read_is_skipped_not_overwritten(tmp_path):
    conn = _db()
    conn.changed.add("id05")
    rep = rb.reembed(
        conn, "lumina", lambda t: [1.0], apply=True, rows_per_sec=0, state_path=tmp_path / "s.json"
    )
    assert rep["skipped_changed_since_read"] == 1 and "id05" not in conn.embeddings


def test_resumable_after_an_interruption(tmp_path):
    state = tmp_path / "s.json"
    conn = _db()
    first = rb.reembed(
        conn,
        "lumina",
        lambda t: [1.0],
        apply=True,
        state_path=state,
        batch_size=3,
        rows_per_sec=0,
        limit=4,
    )
    assert first["scanned"] == 4
    saved = json.loads(state.read_text())
    assert saved["last_id"] == first["last_id"] and saved["agent"] == "lumina"
    done = set(conn.embeddings)
    rest = rb.reembed(
        conn, "lumina", lambda t: [1.0], apply=True, state_path=state, batch_size=3, rows_per_sec=0
    )
    assert rest["updated"] == 9 and set(conn.embeddings) >= done
    again = [s for s, p in conn.sql if s.startswith("UPDATE")]
    assert len(again) == 9, "no row is embedded twice across the resume"


def test_state_for_another_agent_is_refused(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"version": rb.STATE_VERSION, "agent": "jarvis", "last_id": "x"}))
    with pytest.raises(rb.ReembedRefused):
        rb.reembed(_db(), "lumina", None, apply=False, state_path=state)


def test_rate_limit_sleeps_between_batches(tmp_path):
    slept = []
    rb.reembed(
        _db(),
        "lumina",
        lambda t: [1.0],
        apply=True,
        batch_size=4,
        rows_per_sec=1.0,
        state_path=tmp_path / "s.json",
        sleep=slept.append,
    )
    assert slept and all(s > 0 for s in slept)


def test_cli_without_apply_is_a_dry_run_and_needs_a_dsn(monkeypatch, capsys):
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    assert rb.main(["--agent", "lumina"]) == 2
    assert "SKMEMORY_PG_DSN" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        rb.main([])  # no default agent
