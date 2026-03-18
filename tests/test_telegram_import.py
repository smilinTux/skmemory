"""Tests for the Telegram chat export importer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.importers.telegram import (
    _detect_emotion,
    _extract_text,
    _parse_telegram_export,
    import_telegram,
)
from skmemory.store import MemoryStore


def _make_export(messages: list[dict], name: str = "Test Chat") -> dict:
    """Build a minimal Telegram export structure."""
    return {
        "name": name,
        "type": "personal_chat",
        "id": 12345,
        "messages": messages,
    }


def _msg(
    text: str, sender: str = "Alice", msg_id: int = 1, date: str = "2025-06-15T10:30:00"
) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "date": date,
        "from": sender,
        "text": text,
    }


@pytest.fixture
def tmp_store(tmp_path: Path) -> MemoryStore:
    backend = SQLiteBackend(base_path=str(tmp_path / "mem"))
    return MemoryStore(primary=backend)


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    d = tmp_path / "telegram-export"
    d.mkdir()
    return d


class TestExtractText:
    def test_plain_string(self):
        assert _extract_text("hello world") == "hello world"

    def test_entity_list(self):
        result = _extract_text(
            [
                "Hello ",
                {"type": "bold", "text": "world"},
                "!",
            ]
        )
        assert result == "Hello world!"

    def test_empty(self):
        assert _extract_text("") == ""
        assert _extract_text([]) == ""

    def test_none_fallback(self):
        assert _extract_text(None) == ""


class TestDetectEmotion:
    def test_love_detection(self):
        emo = _detect_emotion("I love you so much!")
        assert "love" in emo.labels
        assert emo.intensity > 0

    def test_joy_detection(self):
        emo = _detect_emotion("haha that's amazing!")
        assert "joy" in emo.labels

    def test_neutral(self):
        emo = _detect_emotion("The meeting is at 3pm.")
        assert "neutral" in emo.labels

    def test_caps_boost(self):
        normal = _detect_emotion("I love this")
        caps = _detect_emotion("I LOVE THIS SO MUCH")
        assert caps.intensity >= normal.intensity


class TestParseExport:
    def test_valid_directory(self, export_dir: Path):
        data = _make_export([_msg("hello")])
        (export_dir / "result.json").write_text(json.dumps(data))
        parsed = _parse_telegram_export(str(export_dir))
        assert parsed["name"] == "Test Chat"

    def test_direct_json(self, tmp_path: Path):
        f = tmp_path / "result.json"
        data = _make_export([_msg("hello")])
        f.write_text(json.dumps(data))
        parsed = _parse_telegram_export(str(f))
        assert "messages" in parsed

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            _parse_telegram_export(str(tmp_path / "nonexistent"))

    def test_invalid_json(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text('{"no_messages": true}')
        with pytest.raises(ValueError, match="missing 'messages'"):
            _parse_telegram_export(str(f))


class TestImportPerMessage:
    def test_imports_messages(self, tmp_store: MemoryStore, export_dir: Path):
        msgs = [
            _msg("This is a meaningful message about our plans for the weekend", msg_id=1),
            _msg("Another important conversation topic here", msg_id=2),
            _msg("hi", msg_id=3),  # too short, should be skipped
        ]
        data = _make_export(msgs, name="Chat with Bob")
        (export_dir / "result.json").write_text(json.dumps(data))

        stats = import_telegram(tmp_store, str(export_dir), mode="message")
        assert stats["mode"] == "message"
        assert stats["imported"] == 2
        assert stats["chat_name"] == "Chat with Bob"

    def test_tags_applied(self, tmp_store: MemoryStore, export_dir: Path):
        msgs = [_msg("A real conversation message that is long enough to import")]
        data = _make_export(msgs)
        (export_dir / "result.json").write_text(json.dumps(data))

        import_telegram(tmp_store, str(export_dir), mode="message", tags=["custom"])
        memories = tmp_store.list_memories(tags=["telegram"])
        assert len(memories) == 1
        assert "custom" in memories[0].tags
        assert "chat:Test Chat" in memories[0].tags


class TestImportDaily:
    def test_consolidates_by_day(self, tmp_store: MemoryStore, export_dir: Path):
        msgs = [
            _msg(
                "Morning chat about interesting things and stuff",
                msg_id=1,
                date="2025-06-15T09:00:00",
            ),
            _msg(
                "Afternoon follow-up discussion on that topic",
                msg_id=2,
                date="2025-06-15T14:00:00",
            ),
            _msg(
                "Next day conversation about something new entirely",
                msg_id=3,
                date="2025-06-16T10:00:00",
            ),
        ]
        data = _make_export(msgs)
        (export_dir / "result.json").write_text(json.dumps(data))

        stats = import_telegram(tmp_store, str(export_dir), mode="daily")
        assert stats["mode"] == "daily"
        assert stats["days_processed"] == 2
        assert stats["messages_imported"] == 3

    def test_daily_memory_content(self, tmp_store: MemoryStore, export_dir: Path):
        msgs = [
            _msg(
                "First message of the day that is long enough",
                msg_id=1,
                date="2025-06-15T09:00:00",
                sender="Alice",
            ),
            _msg(
                "Second message of the day also long enough",
                msg_id=2,
                date="2025-06-15T14:00:00",
                sender="Bob",
            ),
        ]
        data = _make_export(msgs)
        (export_dir / "result.json").write_text(json.dumps(data))

        import_telegram(tmp_store, str(export_dir), mode="daily")
        memories = tmp_store.list_memories(tags=["telegram"])
        assert len(memories) == 1
        assert "[Alice]" in memories[0].content
        assert "[Bob]" in memories[0].content

    def test_invalid_mode(self, tmp_store: MemoryStore, export_dir: Path):
        data = _make_export([_msg("something long enough to pass the filter")])
        (export_dir / "result.json").write_text(json.dumps(data))

        with pytest.raises(ValueError, match="Unknown mode"):
            import_telegram(tmp_store, str(export_dir), mode="invalid")
