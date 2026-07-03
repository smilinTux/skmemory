"""Tests for the multi-platform conversation-export importer."""

from __future__ import annotations

import json
from pathlib import Path

from skmemory.admission import SENTINEL_UNRECOVERABLE_SOURCE, review_queue_path
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.importers.convo import (
    chunk_exchanges,
    detect_format,
    import_conversation,
    iter_rows,
    parse_transcript,
)
from skmemory.store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    backend = SQLiteBackend(base_path=str(tmp_path / "mem"))
    return MemoryStore(primary=backend)


def _row(rid: str, **overrides):
    base = {
        "row_id": rid,
        "title": f"Exchange {rid}",
        "content": f"> question {rid}\nanswer {rid}",
        "source": "conversation",
        "tags": ["convo-import"],
        "format": "claude-code",
    }
    base.update(overrides)
    return base


# ── Fixtures: two real export shapes ────────────────────────────────────


def _claude_code_jsonl(tmp_path: Path) -> Path:
    """A tiny Claude Code session JSONL — 2 exchanges."""
    lines = [
        {"type": "user", "message": {"content": "How do I write a decorator?"}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Use functools.wraps like this."}]
            },
        },
        {"type": "user", "message": {"content": "Thanks, show a full example"}},
        {"type": "assistant", "message": {"content": "def foo(): return 'bar'"}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _chatgpt_json(tmp_path: Path) -> Path:
    """A tiny ChatGPT conversations.json mapping-tree export — 1 exchange."""
    data = {
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None, "children": ["m1"]},
            "m1": {
                "id": "m1",
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["What is a monad, briefly?"]},
                },
                "children": ["m2"],
            },
            "m2": {
                "id": "m2",
                "parent": "m1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": ["A monad is a design pattern for chaining computations."]
                    },
                },
                "children": [],
            },
        }
    }
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _junk_file(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_text("just some plain notes, not a chat export at all", encoding="utf-8")
    return path


# ── detect_format ────────────────────────────────────────────────────────


class TestDetectFormat:
    def test_claude_code(self, tmp_path: Path):
        assert detect_format(_claude_code_jsonl(tmp_path)) == "claude-code"

    def test_chatgpt(self, tmp_path: Path):
        assert detect_format(_chatgpt_json(tmp_path)) == "chatgpt"

    def test_junk_returns_none(self, tmp_path: Path):
        assert detect_format(_junk_file(tmp_path)) is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert detect_format(tmp_path / "does-not-exist.jsonl") is None


# ── parse_transcript / chunk_exchanges structure ────────────────────────


class TestParseAndChunk:
    def test_claude_code_transcript_structure(self, tmp_path: Path):
        content = _claude_code_jsonl(tmp_path).read_text(encoding="utf-8")
        parsed = parse_transcript(content)
        assert parsed is not None
        fmt, messages = parsed
        assert fmt == "claude-code"
        assert messages == [
            ("user", "How do I write a decorator?"),
            ("assistant", "Use functools.wraps like this."),
            ("user", "Thanks, show a full example"),
            ("assistant", "def foo(): return 'bar'"),
        ]

    def test_chatgpt_transcript_structure(self, tmp_path: Path):
        content = _chatgpt_json(tmp_path).read_text(encoding="utf-8")
        parsed = parse_transcript(content)
        assert parsed is not None
        fmt, messages = parsed
        assert fmt == "chatgpt"
        assert messages == [
            ("user", "What is a monad, briefly?"),
            ("assistant", "A monad is a design pattern for chaining computations."),
        ]

    def test_junk_returns_none(self, tmp_path: Path):
        content = _junk_file(tmp_path).read_text(encoding="utf-8")
        assert parse_transcript(content) is None

    def test_chunk_exchanges_pairs_user_and_assistant(self):
        messages = [
            ("user", "q1 that is long enough on its own to clear the min size"),
            ("assistant", "a1 that is long enough on its own to clear the min size"),
            ("user", "q2 that is long enough on its own to clear the min size"),
            ("assistant", "a2 that is long enough to pass the min size check"),
        ]
        chunks = chunk_exchanges(messages)
        assert len(chunks) == 2
        assert chunks[0]["chunk_index"] == 0
        assert "q1" in chunks[0]["content"]
        assert "a1" in chunks[0]["content"]
        assert chunks[1]["chunk_index"] == 1

    def test_chunk_exchanges_keeps_full_assistant_reply_no_truncation(self):
        long_reply = "\n".join(f"line {i} of a long assistant reply" for i in range(20))
        messages = [("user", "explain everything"), ("assistant", long_reply)]
        chunks = chunk_exchanges(messages)
        assert len(chunks) == 1
        # MemPalace truncates to ai_lines[:8] — we deliberately do not.
        assert "line 19" in chunks[0]["content"]

    def test_chunk_exchanges_captures_leading_assistant_only_run(self):
        messages = [
            ("assistant", "a stray system-ish message with no preceding user turn at all"),
            ("user", "q1"),
            ("assistant", "a1 with enough length to clear the minimum chunk size check"),
        ]
        chunks = chunk_exchanges(messages)
        assert len(chunks) == 2
        assert "stray system-ish" in chunks[0]["content"]
        assert "q1" in chunks[1]["content"]


class TestIterRows:
    def test_claude_code_row_count_and_fields(self, tmp_path: Path):
        path = _claude_code_jsonl(tmp_path)
        rows = list(iter_rows(path))
        assert len(rows) == 2
        assert rows[0]["format"] == "claude-code"
        assert rows[0]["source"] == "conversation"
        assert rows[0]["external_path"] == str(path)
        assert rows[0]["row_id"] == f"{path.name}#0"

    def test_chatgpt_row_count(self, tmp_path: Path):
        path = _chatgpt_json(tmp_path)
        rows = list(iter_rows(path))
        assert len(rows) == 1
        assert rows[0]["format"] == "chatgpt"

    def test_unrecognized_format_raises(self, tmp_path: Path):
        import pytest

        path = _junk_file(tmp_path)
        with pytest.raises(ValueError):
            list(iter_rows(path))


# ── import_conversation — real SQLite-backed store, no live services ────


class TestImportConversation:
    def test_admitted_exchange_lands_with_known_source(self, tmp_path: Path):
        store = _store(tmp_path)
        summary = import_conversation(
            tmp_path / "unused.jsonl",
            store,
            rows=[_row("r1")],
            agent_home=tmp_path,
        )
        assert summary["seen"] == 1
        assert summary["admitted"] == 1
        assert summary["refused"] == 0
        assert summary["format"] == "claude-code"

        rows = store.primary.list_memories()
        assert len(rows) == 1
        mem = rows[0]
        assert mem.source == "conversation"
        assert mem.metadata["admission_admit"] is True
        assert mem.metadata["admission_excluded_from_retrieval"] is False
        assert "admission:admitted" in mem.tags

    def test_refused_exchange_lands_under_sentinel(self, tmp_path: Path):
        store = _store(tmp_path)
        summary = import_conversation(
            tmp_path / "unused.jsonl",
            store,
            rows=[_row("r2", tags=["egregore"])],
            agent_home=tmp_path,
        )
        assert summary["refused"] == 1
        assert summary["admitted"] == 0
        rows = store.primary.list_memories()
        assert len(rows) == 1
        assert rows[0].source == SENTINEL_UNRECOVERABLE_SOURCE
        assert rows[0].metadata["admission_excluded_from_retrieval"] is True
        assert "admission:refused" in rows[0].tags

    def test_end_to_end_from_real_claude_code_file(self, tmp_path: Path):
        """Full path: real file -> iter_rows -> admission gates -> store."""
        path = _claude_code_jsonl(tmp_path)
        store = _store(tmp_path)
        summary = import_conversation(path, store, agent_home=tmp_path)
        assert summary["seen"] == 2
        assert summary["admitted"] == 2
        assert summary["format"] == "claude-code"
        rows = store.primary.list_memories()
        assert len(rows) == 2

    def test_end_to_end_from_real_chatgpt_file(self, tmp_path: Path):
        path = _chatgpt_json(tmp_path)
        store = _store(tmp_path)
        summary = import_conversation(path, store, agent_home=tmp_path)
        assert summary["seen"] == 1
        assert summary["admitted"] == 1
        assert summary["format"] == "chatgpt"

    def test_loosening_blocks_and_queues_review(self, tmp_path: Path):
        stored = {
            "row-loose": {
                "admission_admit": False,
                "admission_policy_version": "0.9.0",
                "admission_reason": "refuse_collective_echo",
            }
        }
        store = _store(tmp_path)
        summary = import_conversation(
            tmp_path / "unused.jsonl",
            store,
            rows=[_row("row-loose")],
            agent_home=tmp_path,
            stored_decisions=stored,
        )
        assert summary["queued_for_review"] == 1
        assert summary["admitted"] == 0
        assert store.primary.list_memories() == []

        queue = review_queue_path(tmp_path)
        assert queue.exists()
        record = json.loads(queue.read_text(encoding="utf-8").strip())
        assert record["row_id"] == "row-loose"
        assert record["importer"] == "conversation"
