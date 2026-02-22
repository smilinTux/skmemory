"""
Telegram chat export importer for SKMemory.

Reads the ``result.json`` file produced by Telegram Desktop's
"Export Chat History" feature and converts conversations into
searchable memories.

Two modes:
    - **message**: one memory per substantial message (fine-grained)
    - **daily**: consolidate all messages per day into a single
      mid-term memory (recommended for large exports)

Usage (CLI):
    skmemory import-telegram /path/to/telegram-export/
    skmemory import-telegram /path/to/result.json --mode daily

Usage (Python):
    from skmemory.importers.telegram import import_telegram
    from skmemory import SKMemoryPlugin

    plugin = SKMemoryPlugin()
    stats = import_telegram(plugin.store, "/path/to/export/")
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import EmotionalSnapshot, MemoryLayer, MemoryRole
from ..store import MemoryStore


def _extract_text(text_field) -> str:
    """Extract plain text from Telegram's text field.

    Telegram stores text as either a string or a list of mixed
    string/object segments (for entities like bold, links, etc.).

    Args:
        text_field: Raw text field from result.json.

    Returns:
        str: Flat plain-text string.
    """
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        parts = []
        for segment in text_field:
            if isinstance(segment, str):
                parts.append(segment)
            elif isinstance(segment, dict):
                parts.append(segment.get("text", ""))
        return "".join(parts)
    return ""


def _detect_emotion(text: str) -> EmotionalSnapshot:
    """Simple keyword-based emotion detection for chat messages.

    Args:
        text: Message text.

    Returns:
        EmotionalSnapshot: Basic emotional metadata.
    """
    lower = text.lower()

    intensity = 0.0
    valence = 0.0
    labels: list[str] = []

    love_words = {"love", "adore", "heart", "miss you", "xoxo", "kisses"}
    joy_words = {"haha", "lol", "rofl", "lmao", "amazing", "awesome", "yay", "woohoo"}
    sad_words = {"sad", "sorry", "miss", "cry", "tears", "hurt"}
    anger_words = {"angry", "furious", "hate", "ugh", "frustrated"}

    if any(w in lower for w in love_words):
        labels.append("love")
        intensity = max(intensity, 7.0)
        valence = 0.9
    if any(w in lower for w in joy_words):
        labels.append("joy")
        intensity = max(intensity, 5.0)
        valence = max(valence, 0.7)
    if any(w in lower for w in sad_words):
        labels.append("sadness")
        intensity = max(intensity, 4.0)
        valence = min(valence, -0.3)
    if any(w in lower for w in anger_words):
        labels.append("anger")
        intensity = max(intensity, 5.0)
        valence = min(valence, -0.5)

    if "!" in text:
        intensity = min(intensity + 1.0, 10.0)
    if text.isupper() and len(text) > 10:
        intensity = min(intensity + 2.0, 10.0)

    return EmotionalSnapshot(
        intensity=intensity,
        valence=valence,
        labels=labels or ["neutral"],
    )


def _parse_telegram_export(export_path: str) -> dict:
    """Locate and parse the Telegram result.json.

    Args:
        export_path: Path to the export directory or result.json file.

    Returns:
        dict: Parsed JSON data.

    Raises:
        FileNotFoundError: If result.json cannot be found.
        ValueError: If the file is not valid Telegram export JSON.
    """
    path = Path(export_path)

    if path.is_file() and path.suffix == ".json":
        json_path = path
    elif path.is_dir():
        json_path = path / "result.json"
        if not json_path.exists():
            candidates = list(path.glob("*.json"))
            if len(candidates) == 1:
                json_path = candidates[0]
            else:
                raise FileNotFoundError(
                    f"No result.json found in {export_path}. "
                    f"Point to the Telegram Desktop export folder or the JSON file directly."
                )
    else:
        raise FileNotFoundError(f"Path not found: {export_path}")

    data = json.loads(json_path.read_text(encoding="utf-8"))

    if "messages" not in data:
        raise ValueError(
            "Not a valid Telegram export: missing 'messages' array. "
            "Use Telegram Desktop > Export Chat History > JSON format."
        )

    return data


def import_telegram(
    store: MemoryStore,
    export_path: str,
    *,
    mode: str = "daily",
    min_message_length: int = 30,
    chat_name: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """Import a Telegram chat export into SKMemory.

    Args:
        store: The MemoryStore to import into.
        export_path: Path to the export directory or result.json file.
        mode: Import mode — 'message' (one per message) or 'daily'
            (consolidated per day). Default: 'daily'.
        min_message_length: Skip messages shorter than this (default: 30).
        chat_name: Override the chat name from the export.
        tags: Extra tags to apply to all imported memories.

    Returns:
        dict: Import statistics with counts and details.

    Raises:
        FileNotFoundError: If the export path is invalid.
        ValueError: If the file format is wrong.
    """
    data = _parse_telegram_export(export_path)

    name = chat_name or data.get("name", "Telegram Chat")
    extra_tags = tags or []
    base_tags = ["telegram", "chat-import", f"chat:{name}"] + extra_tags

    messages = [
        m for m in data["messages"]
        if m.get("type") == "message"
        and len(_extract_text(m.get("text", ""))) >= min_message_length
    ]

    if mode == "message":
        return _import_per_message(store, messages, name, base_tags)
    elif mode == "daily":
        return _import_daily(store, messages, name, base_tags)
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'message' or 'daily'.")


def _import_per_message(
    store: MemoryStore,
    messages: list[dict],
    chat_name: str,
    base_tags: list[str],
) -> dict:
    """Import each message as its own short-term memory.

    Args:
        store: Target MemoryStore.
        messages: Filtered message list.
        chat_name: Chat name for titles.
        base_tags: Tags to apply.

    Returns:
        dict: Import stats.
    """
    imported = 0
    skipped = 0

    for msg in messages:
        text = _extract_text(msg.get("text", ""))
        sender = msg.get("from", msg.get("from_id", "unknown"))
        date_str = msg.get("date", "")

        emotional = _detect_emotion(text)

        try:
            store.snapshot(
                title=f"{sender}: {text[:70]}",
                content=text,
                layer=MemoryLayer.SHORT,
                role=MemoryRole.GENERAL,
                tags=base_tags + [f"sender:{sender}"],
                emotional=emotional,
                source="telegram",
                source_ref=f"telegram:{msg.get('id', '')}",
                metadata={
                    "telegram_msg_id": msg.get("id"),
                    "sender": sender,
                    "date": date_str,
                    "chat": chat_name,
                },
            )
            imported += 1
        except Exception:
            skipped += 1

    return {
        "mode": "message",
        "chat_name": chat_name,
        "total_messages": len(messages),
        "imported": imported,
        "skipped": skipped,
    }


def _import_daily(
    store: MemoryStore,
    messages: list[dict],
    chat_name: str,
    base_tags: list[str],
) -> dict:
    """Consolidate messages by day into mid-term memories.

    Args:
        store: Target MemoryStore.
        messages: Filtered message list.
        chat_name: Chat name for titles.
        base_tags: Tags to apply.

    Returns:
        dict: Import stats.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)

    for msg in messages:
        date_str = msg.get("date", "")
        try:
            day = date_str[:10]
            if day:
                by_day[day].append(msg)
        except Exception:
            continue

    imported = 0
    days_processed = 0

    for day, day_msgs in sorted(by_day.items()):
        lines = []
        senders: set[str] = set()
        max_intensity = 0.0
        all_labels: list[str] = []

        for msg in day_msgs:
            text = _extract_text(msg.get("text", ""))
            sender = msg.get("from", msg.get("from_id", "unknown"))
            senders.add(str(sender))
            lines.append(f"[{sender}] {text}")

            emo = _detect_emotion(text)
            max_intensity = max(max_intensity, emo.intensity)
            all_labels.extend(emo.labels)

        content = "\n".join(lines)
        unique_labels = list(dict.fromkeys(all_labels))[:5]
        participant_str = ", ".join(sorted(senders))

        store.snapshot(
            title=f"{chat_name} — {day} ({len(day_msgs)} messages)",
            content=content,
            layer=MemoryLayer.MID,
            role=MemoryRole.GENERAL,
            tags=base_tags + [f"date:{day}"],
            emotional=EmotionalSnapshot(
                intensity=max_intensity,
                labels=unique_labels,
            ),
            source="telegram",
            source_ref=f"telegram:daily:{day}",
            metadata={
                "date": day,
                "message_count": len(day_msgs),
                "participants": participant_str,
                "chat": chat_name,
            },
        )
        imported += len(day_msgs)
        days_processed += 1

    return {
        "mode": "daily",
        "chat_name": chat_name,
        "total_messages": len(messages),
        "days_processed": days_processed,
        "messages_imported": imported,
    }
