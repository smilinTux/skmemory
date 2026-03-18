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
from datetime import datetime, timezone
from pathlib import Path

import click

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
    trust_words = {"trust", "believe", "faith", "rely", "depend", "safe"}
    curiosity_words = {"curious", "wonder", "interesting", "fascinated", "hmm", "what if"}
    gratitude_words = {"thank", "thanks", "grateful", "appreciate", "blessed", "thankful"}

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
    if any(w in lower for w in trust_words):
        labels.append("trust")
        intensity = max(intensity, 5.0)
        valence = max(valence, 0.6)
    if any(w in lower for w in curiosity_words):
        labels.append("curiosity")
        intensity = max(intensity, 3.0)
        valence = max(valence, 0.4)
    if any(w in lower for w in gratitude_words):
        labels.append("gratitude")
        intensity = max(intensity, 6.0)
        valence = max(valence, 0.8)

    if "!" in text:
        intensity = min(intensity + 1.0, 10.0)
    if text.isupper() and len(text) > 10:
        intensity = min(intensity + 2.0, 10.0)

    love_emojis = {
        "\u2764",
        "\U0001f495",
        "\U0001f496",
        "\U0001f497",
        "\U0001f498",
        "\U0001f49d",
        "\U0001f970",
        "\U0001f60d",
        "\U0001f49e",
    }
    joy_emojis = {
        "\U0001f602",
        "\U0001f923",
        "\U0001f604",
        "\U0001f60a",
        "\U0001f389",
        "\U0001f973",
        "\u2728",
        "\U0001f38a",
    }
    sad_emojis = {"\U0001f622", "\U0001f62d", "\U0001f494", "\U0001f63f", "\U0001f97a"}
    if any(e in text for e in love_emojis):
        if "love" not in labels:
            labels.append("love")
        intensity = max(intensity, 7.0)
        valence = max(valence, 0.9)
    if any(e in text for e in joy_emojis):
        if "joy" not in labels:
            labels.append("joy")
        intensity = max(intensity, 5.0)
        valence = max(valence, 0.7)
    if any(e in text for e in sad_emojis):
        if "sadness" not in labels:
            labels.append("sadness")
        intensity = max(intensity, 4.0)
        valence = min(valence, -0.3)

    return EmotionalSnapshot(
        intensity=intensity,
        valence=valence,
        labels=labels or ["neutral"],
    )


def _detect_content_type(msg: dict) -> list[str]:
    """Detect content type tags from a message.

    Args:
        msg: Telegram message dict.

    Returns:
        list[str]: Content type tags.
    """
    tags = []
    text = _extract_text(msg.get("text", ""))

    if "http://" in text or "https://" in text:
        tags.append("contains:url")
    if msg.get("media_type") or msg.get("photo") or msg.get("file"):
        tags.append("contains:media")
    if msg.get("file"):
        tags.append("contains:file")
    if msg.get("sticker_emoji") or msg.get("sticker"):
        tags.append("contains:sticker")

    return tags


def _detect_reply(msg: dict) -> str | None:
    """Detect if this message is a reply to another.

    Args:
        msg: Telegram message dict.

    Returns:
        Optional[str]: Reply reference string, or None.
    """
    reply_id = msg.get("reply_to_message_id")
    if reply_id:
        return f"reply_to:{reply_id}"
    return None


def _detect_sender_role(sender: str) -> str:
    """Heuristic to detect if the sender is an AI or human.

    Args:
        sender: Sender name string.

    Returns:
        str: 'ai' or 'human'.
    """
    ai_indicators = {
        "bot",
        "gpt",
        "claude",
        "gemini",
        "llama",
        "assistant",
        "lumina",
        "copilot",
        "ai",
        "opus",
        "sonnet",
        "haiku",
    }
    sender_lower = sender.lower()
    if any(indicator in sender_lower for indicator in ai_indicators):
        return "ai"
    return "human"


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
    chat_name: str | None = None,
    tags: list[str] | None = None,
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
        m
        for m in data["messages"]
        if m.get("type") == "message"
        and len(_extract_text(m.get("text", ""))) >= min_message_length
    ]

    if mode == "message":
        return _import_per_message(store, messages, name, base_tags)
    elif mode == "daily":
        return _import_daily(store, messages, name, base_tags)
    elif mode == "catchup":
        return _import_catchup(store, messages, name, base_tags)
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'message', 'daily', or 'catchup'.")


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

    with click.progressbar(messages, label="  Importing messages", show_pos=True) as bar:
        for msg in bar:
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
                    tags=base_tags
                    + [f"sender:{sender}", f"role:{_detect_sender_role(sender)}"]
                    + _detect_content_type(msg),
                    emotional=emotional,
                    source="telegram",
                    source_ref=f"telegram:{msg.get('id', '')}",
                    metadata={
                        "telegram_msg_id": msg.get("id"),
                        "sender": sender,
                        "date": date_str,
                        "chat": chat_name,
                        "reply_ref": _detect_reply(msg),
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

    sorted_days = sorted(by_day.items())
    with click.progressbar(sorted_days, label="  Importing daily batches", show_pos=True) as bar:
        for day, day_msgs in bar:
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


def _import_catchup(
    store: MemoryStore,
    messages: list[dict],
    chat_name: str,
    base_tags: list[str],
) -> dict:
    """Import across all memory tiers for full context catch-up.

    Distributes messages intelligently across tiers:
    - Last 24 hours → short-term (individual messages, full detail)
    - Last 7 days → mid-term (daily summaries)
    - Older than 7 days → long-term (weekly summaries, key themes)

    Args:
        store: Target MemoryStore.
        messages: Filtered message list.
        chat_name: Chat name for titles.
        base_tags: Tags to apply.

    Returns:
        dict: Import stats per tier.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    cutoff_short = now - timedelta(hours=24)
    cutoff_mid = now - timedelta(days=7)

    short_msgs: list[dict] = []
    mid_msgs: dict[str, list[dict]] = defaultdict(list)
    long_msgs: dict[str, list[dict]] = defaultdict(list)

    for msg in messages:
        date_str = msg.get("date", "")
        if not date_str:
            continue
        try:
            msg_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Try just the date portion
            try:
                msg_dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

        if msg_dt >= cutoff_short:
            short_msgs.append(msg)
        elif msg_dt >= cutoff_mid:
            day = date_str[:10]
            mid_msgs[day].append(msg)
        else:
            # Group by ISO week for long-term
            week_key = msg_dt.strftime("%Y-W%W")
            long_msgs[week_key].append(msg)

    stats = {
        "mode": "catchup",
        "chat_name": chat_name,
        "total_messages": len(messages),
        "short_term": {"count": 0},
        "mid_term": {"days": 0, "messages": 0},
        "long_term": {"weeks": 0, "messages": 0},
    }

    # --- Short-term: individual messages (last 24h) ---
    for msg in short_msgs:
        text = _extract_text(msg.get("text", ""))
        sender = msg.get("from", msg.get("from_id", "unknown"))
        emotional = _detect_emotion(text)
        try:
            store.snapshot(
                title=f"{sender}: {text[:70]}",
                content=text,
                layer=MemoryLayer.SHORT,
                role=MemoryRole.GENERAL,
                tags=base_tags
                + [
                    f"sender:{sender}",
                    f"role:{_detect_sender_role(sender)}",
                    "catchup:short",
                ]
                + _detect_content_type(msg),
                emotional=emotional,
                source="telegram",
                source_ref=f"telegram:{msg.get('id', '')}",
                metadata={
                    "telegram_msg_id": msg.get("id"),
                    "sender": sender,
                    "date": msg.get("date", ""),
                    "chat": chat_name,
                    "reply_ref": _detect_reply(msg),
                },
            )
            stats["short_term"]["count"] += 1
        except Exception:
            pass

    # --- Mid-term: daily summaries (last 7 days) ---
    for day, day_msgs in sorted(mid_msgs.items()):
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

        store.snapshot(
            title=f"{chat_name} — {day} ({len(day_msgs)} messages)",
            content=content,
            layer=MemoryLayer.MID,
            role=MemoryRole.GENERAL,
            tags=base_tags + [f"date:{day}", "catchup:mid"],
            emotional=EmotionalSnapshot(
                intensity=max_intensity,
                labels=unique_labels,
            ),
            source="telegram",
            source_ref=f"telegram:daily:{day}",
            metadata={
                "date": day,
                "message_count": len(day_msgs),
                "participants": ", ".join(sorted(senders)),
                "chat": chat_name,
            },
        )
        stats["mid_term"]["days"] += 1
        stats["mid_term"]["messages"] += len(day_msgs)

    # --- Long-term: weekly summaries (older than 7 days) ---
    for week, week_msgs in sorted(long_msgs.items()):
        lines = []
        senders: set[str] = set()
        topics: set[str] = set()
        max_intensity = 0.0
        all_labels: list[str] = []
        dates_covered: set[str] = set()

        for msg in week_msgs:
            text = _extract_text(msg.get("text", ""))
            sender = msg.get("from", msg.get("from_id", "unknown"))
            senders.add(str(sender))
            dates_covered.add(msg.get("date", "")[:10])

            # For long-term, keep only first 200 chars per message
            lines.append(f"[{sender}] {text[:200]}")
            emo = _detect_emotion(text)
            max_intensity = max(max_intensity, emo.intensity)
            all_labels.extend(emo.labels)

            # Extract potential topics from longer messages
            if len(text) > 100:
                words = text.lower().split()
                for w in words:
                    if len(w) > 6 and w.isalpha():
                        topics.add(w)

        # Summarize: limit content to avoid bloat
        if len(lines) > 50:
            content = "\n".join(lines[:25])
            content += f"\n\n... ({len(lines) - 25} more messages) ...\n\n"
            content += "\n".join(lines[-10:])
        else:
            content = "\n".join(lines)

        unique_labels = list(dict.fromkeys(all_labels))[:5]
        date_range = f"{min(dates_covered)} to {max(dates_covered)}" if dates_covered else week

        store.snapshot(
            title=f"{chat_name} — Week {week} ({len(week_msgs)} messages)",
            content=content,
            layer=MemoryLayer.LONG,
            role=MemoryRole.GENERAL,
            tags=base_tags + [f"week:{week}", "catchup:long"],
            emotional=EmotionalSnapshot(
                intensity=max_intensity,
                labels=unique_labels,
            ),
            source="telegram",
            source_ref=f"telegram:weekly:{week}",
            metadata={
                "week": week,
                "date_range": date_range,
                "message_count": len(week_msgs),
                "participants": ", ".join(sorted(senders)),
                "chat": chat_name,
                "days_covered": len(dates_covered),
            },
        )
        stats["long_term"]["weeks"] += 1
        stats["long_term"]["messages"] += len(week_msgs)

    return stats
