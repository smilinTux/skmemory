"""
Telegram API importer for SKMemory — direct pull via Telethon.

Instead of exporting chat history manually from Telegram Desktop,
this module connects directly to the Telegram API using Telethon
and pulls messages programmatically.

Setup (one-time):
    1. Install:     pip install skmemory[telegram]  (or: pipx inject skmemory telethon)
    2. Credentials:  Get API_ID and API_HASH from https://my.telegram.org
    3. Export:
         export TELEGRAM_API_ID=12345678
         export TELEGRAM_API_HASH=your_api_hash_here
    4. First run will prompt for phone number + verification code.
       Session is saved at ~/.skcapstone/agent/lumina/telegram.session for future use.

Environment variables:
    TELEGRAM_API_ID   — your Telegram API ID  (from https://my.telegram.org)
    TELEGRAM_API_HASH — your Telegram API hash (from https://my.telegram.org)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import SKMEMORY_HOME
from ..store import MemoryStore

SESSION_PATH = str(SKMEMORY_HOME / "telegram.session")


def check_setup() -> dict:
    """Check if Telegram API import is properly configured.

    Returns:
        dict with keys: ready (bool), telethon (bool), credentials (bool),
        session (bool), messages (list[str])
    """
    result = {
        "ready": False,
        "telethon": False,
        "credentials": False,
        "session": False,
        "messages": [],
    }

    # Check telethon
    try:
        import telethon  # noqa: F401

        result["telethon"] = True
    except ImportError:
        result["messages"].append(
            "Telethon not installed. Fix: pip install skmemory[telegram]  "
            "or: pipx inject skmemory telethon"
        )

    # Check credentials
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if api_id and api_hash:
        result["credentials"] = True
    else:
        missing = []
        if not api_id:
            missing.append("TELEGRAM_API_ID")
        if not api_hash:
            missing.append("TELEGRAM_API_HASH")
        result["messages"].append(
            f"Missing environment variable(s): {', '.join(missing)}. "
            f"Get them from https://my.telegram.org and export them in your shell."
        )

    # Check session
    if Path(SESSION_PATH).exists():
        result["session"] = True
    else:
        result["messages"].append(
            "No Telegram session found. First run will prompt for phone "
            "number and verification code."
        )

    result["ready"] = result["telethon"] and result["credentials"]
    return result


async def _fetch_messages(
    chat_name_or_id: str,
    limit: int | None = None,
    since: str | None = None,
) -> dict:
    """Connect to Telegram API and fetch messages from a chat.

    Args:
        chat_name_or_id: Chat username, title, or numeric ID.
        limit: Maximum number of messages to fetch.
        since: Only fetch messages after this date (YYYY-MM-DD).

    Returns:
        dict: Telegram Desktop-compatible export structure.

    Raises:
        RuntimeError: If API credentials are missing or connection fails.
    """
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram API credentials not found.\n\n"
            "Setup steps:\n"
            "  1. Go to https://my.telegram.org and log in\n"
            "  2. Click 'API development tools' and create an app\n"
            "  3. Set environment variables:\n"
            "       export TELEGRAM_API_ID=<your_api_id>\n"
            "       export TELEGRAM_API_HASH=<your_api_hash>\n"
            "  4. Run this command again — first run will prompt for phone verification"
        )

    try:
        from telethon import TelegramClient
        from telethon.tl.types import User
    except ImportError:
        raise RuntimeError(
            "Telethon is required for direct API import. "
            "Install it with: pip install skmemory[telegram]"
        ) from None

    # Ensure session directory exists
    session_dir = Path(SESSION_PATH).parent
    session_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        SESSION_PATH,
        int(api_id),
        api_hash,
    )

    await client.start()

    try:
        # Resolve the chat entity
        try:
            entity = await client.get_entity(chat_name_or_id)
        except ValueError:
            # Try as integer ID
            try:
                entity = await client.get_entity(int(chat_name_or_id))
            except (ValueError, TypeError):
                raise RuntimeError(f"Could not find chat: {chat_name_or_id}") from None

        chat_title = getattr(entity, "title", None)
        if chat_title is None:
            if isinstance(entity, User):
                parts = [entity.first_name or "", entity.last_name or ""]
                chat_title = " ".join(p for p in parts if p) or str(entity.id)
            else:
                chat_title = str(entity.id)

        # Build kwargs for iter_messages
        kwargs = {}
        if limit:
            kwargs["limit"] = limit
        if since:
            try:
                since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                kwargs["offset_date"] = since_dt
                kwargs["reverse"] = True
            except ValueError:
                raise RuntimeError(f"Invalid date format: {since}. Use YYYY-MM-DD.") from None

        if not limit and not since:
            kwargs["limit"] = 1000  # sensible default

        # Fetch messages
        messages_data = []
        async for message in client.iter_messages(entity, **kwargs):
            if message.text:
                sender_name = "Unknown"
                if message.sender:
                    if isinstance(message.sender, User):
                        parts = [message.sender.first_name or "", message.sender.last_name or ""]
                        sender_name = " ".join(p for p in parts if p) or str(message.sender_id)
                    else:
                        sender_name = getattr(message.sender, "title", str(message.sender_id))

                msg_dict = {
                    "id": message.id,
                    "type": "message",
                    "date": message.date.isoformat() if message.date else "",
                    "from": sender_name,
                    "from_id": f"user{message.sender_id}" if message.sender_id else "",
                    "text": message.text,
                }

                if message.reply_to and message.reply_to.reply_to_msg_id:
                    msg_dict["reply_to_message_id"] = message.reply_to.reply_to_msg_id

                if message.media:
                    msg_dict["media_type"] = type(message.media).__name__

                messages_data.append(msg_dict)

        return {
            "name": chat_title,
            "type": "personal_chat",
            "id": getattr(entity, "id", 0),
            "messages": messages_data,
        }
    finally:
        await client.disconnect()


async def send_message(
    chat: str,
    message: str,
    parse_mode: str | None = None,
) -> dict:
    """Send a message to a Telegram chat via Telethon.

    Args:
        chat: Chat username, title, or numeric ID.
        message: Message text to send.
        parse_mode: Optional parse mode — 'html' or 'markdown'.

    Returns:
        dict with keys: sent (bool), message_id, chat, date.

    Raises:
        RuntimeError: If credentials are missing or send fails.
    """
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram API credentials not found. "
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables."
        )

    try:
        from telethon import TelegramClient
    except ImportError:
        raise RuntimeError(
            "Telethon is required. Install with: pip install skmemory[telegram]"
        ) from None

    session_dir = Path(SESSION_PATH).parent
    session_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
    await client.start()

    try:
        # Resolve entity
        try:
            entity = await client.get_entity(chat)
        except ValueError:
            try:
                entity = await client.get_entity(int(chat))
            except (ValueError, TypeError):
                raise RuntimeError(f"Could not find chat: {chat}") from None

        # Determine parse mode
        pm = None
        if parse_mode:
            if parse_mode.lower() == "html":
                from telethon.extensions import html as telethon_html  # noqa: F401

                pm = "html"
            elif parse_mode.lower() in ("markdown", "md"):
                pm = "md"

        sent_msg = await client.send_message(entity, message, parse_mode=pm)

        return {
            "sent": True,
            "message_id": sent_msg.id,
            "chat": chat,
            "date": sent_msg.date.isoformat() if sent_msg.date else "",
        }
    finally:
        await client.disconnect()


async def poll_messages(
    chat: str,
    limit: int = 20,
    since: str | None = None,
) -> list[dict]:
    """Fetch recent messages from a Telegram chat (one-shot poll).

    Args:
        chat: Chat username, title, or numeric ID.
        limit: Maximum number of messages to return.
        since: Only return messages after this ISO date (YYYY-MM-DD).

    Returns:
        list[dict]: Messages as clean dicts with id, date, sender, text, etc.

    Raises:
        RuntimeError: If credentials are missing or connection fails.
    """
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram API credentials not found. "
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables."
        )

    try:
        from telethon import TelegramClient
        from telethon.tl.types import User
    except ImportError:
        raise RuntimeError(
            "Telethon is required. Install with: pip install skmemory[telegram]"
        ) from None

    session_dir = Path(SESSION_PATH).parent
    session_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
    await client.start()

    try:
        # Resolve entity
        try:
            entity = await client.get_entity(chat)
        except ValueError:
            try:
                entity = await client.get_entity(int(chat))
            except (ValueError, TypeError):
                raise RuntimeError(f"Could not find chat: {chat}") from None

        kwargs: dict = {"limit": limit}
        if since:
            try:
                since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                kwargs["offset_date"] = since_dt
                kwargs["reverse"] = True
            except ValueError:
                raise RuntimeError(f"Invalid date format: {since}. Use YYYY-MM-DD.") from None

        messages = []
        async for msg in client.iter_messages(entity, **kwargs):
            sender_name = "Unknown"
            if msg.sender:
                if isinstance(msg.sender, User):
                    parts = [msg.sender.first_name or "", msg.sender.last_name or ""]
                    sender_name = " ".join(p for p in parts if p) or str(msg.sender_id)
                else:
                    sender_name = getattr(msg.sender, "title", str(msg.sender_id))

            messages.append(
                {
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else "",
                    "sender": sender_name,
                    "sender_id": msg.sender_id,
                    "text": msg.text or "",
                    "has_media": msg.media is not None,
                    "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                }
            )

        return messages
    finally:
        await client.disconnect()


async def list_chats(limit: int = 50) -> list[dict]:
    """List available Telegram chats/groups/channels.

    Args:
        limit: Maximum number of dialogs to return.

    Returns:
        list[dict]: Chats with id, title, type, unread_count.

    Raises:
        RuntimeError: If credentials are missing or connection fails.
    """
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram API credentials not found. "
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables."
        )

    try:
        from telethon import TelegramClient
        from telethon.tl.types import Channel, Chat, User
    except ImportError:
        raise RuntimeError(
            "Telethon is required. Install with: pip install skmemory[telegram]"
        ) from None

    session_dir = Path(SESSION_PATH).parent
    session_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
    await client.start()

    try:
        chats = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            chat_type = "unknown"
            if isinstance(entity, User):
                chat_type = "user"
            elif isinstance(entity, Channel):
                chat_type = "channel" if entity.broadcast else "supergroup"
            elif isinstance(entity, Chat):
                chat_type = "group"

            title = dialog.title or ""
            if isinstance(entity, User):
                parts = [entity.first_name or "", entity.last_name or ""]
                title = " ".join(p for p in parts if p) or str(entity.id)

            chats.append(
                {
                    "id": entity.id,
                    "title": title,
                    "type": chat_type,
                    "unread_count": dialog.unread_count,
                    "username": getattr(entity, "username", None),
                }
            )

        return chats
    finally:
        await client.disconnect()


def import_telegram_api(
    store: MemoryStore,
    chat_name_or_id: str,
    *,
    mode: str = "daily",
    limit: int | None = None,
    since: str | None = None,
    min_message_length: int = 30,
    chat_name: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Import messages directly from Telegram API into SKMemory.

    Connects to the Telegram API via Telethon, fetches messages,
    and delegates to the standard Telegram importer.

    Args:
        store: The MemoryStore to import into.
        chat_name_or_id: Chat username, title, or numeric ID.
        mode: Import mode — 'daily' or 'message'.
        limit: Maximum number of messages to fetch.
        since: Only fetch messages after this date (YYYY-MM-DD).
        min_message_length: Skip messages shorter than this.
        chat_name: Override the chat name.
        tags: Extra tags to apply.

    Returns:
        dict: Import statistics.
    """
    from .telegram import import_telegram

    # Fetch messages from API
    data = asyncio.run(_fetch_messages(chat_name_or_id, limit=limit, since=since))

    # Write to temp file in Telegram Desktop export format
    with tempfile.TemporaryDirectory() as tmpdir:
        export_path = Path(tmpdir) / "result.json"
        export_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

        return import_telegram(
            store,
            str(export_path),
            mode=mode,
            min_message_length=min_message_length,
            chat_name=chat_name or data.get("name"),
            tags=tags,
        )
