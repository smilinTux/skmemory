"""Multi-platform conversation-export importer.

Ports MemPalace's format-detection + parsing (``mempalace/normalize.py``)
and exchange chunking (``mempalace/convo_miner.py``) into skmemory, wired
through the two-gate admission pipeline the same way
``skmemory.importers.notion`` is — NOT MemPalace's raw ChromaDB upsert.

Supported export formats (first non-``None`` parser wins, mirroring
MemPalace's try-chain):

* Claude Code session JSONL (``~/.claude/projects/**/*.jsonl``)
* OpenAI Codex CLI session JSONL (``~/.codex/sessions/**/*.jsonl``)
* claude.ai JSON export (flat ``messages``/``chat_messages``, or the
  privacy-export array-of-conversations shape)
* ChatGPT ``conversations.json`` (mapping-tree shape)
* Slack channel/DM JSON export

Deliberately NOT ported from MemPalace:

* The ``ai_lines[:8]`` truncation in ``_chunk_by_exchange`` — assistant
  turns are kept in full; ``store.snapshot()`` already knows how to
  split/decompose long content.
* The keyword-frequency ``detect_convo_room`` topic classifier —
  skmemory already has ``skmemory.extractor`` for keyword-based
  classification; reuse that later if per-exchange topic tagging is
  wanted instead of duplicating a second classifier here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from ..admission import (
    AdmissionPolicy,
    Gate2Result,
    SENTINEL_UNRECOVERABLE_SOURCE,
    admit,
    enqueue_review,
    evaluate_rerun,
    recover,
)
from ..models import EmotionalSnapshot, MemoryLayer, MemoryRole

logger = logging.getLogger(__name__)

# Admission source_type for rows this importer produces. Must be a member
# of ``skmemory.admission.constants.KNOWN_SOURCE_VOCAB`` so Gate 1 classifies
# it as ``LEGACY_BARE_STRING`` (recoverable) rather than failing the row.
SOURCE_TYPE = "conversation"

MIN_CHUNK_SIZE = 30

# One (role, text) pair per conversation turn.
Message = tuple[str, str]


# ─────────────────────────────────────────────────────────────────────────
# Format parsing — ported from mempalace/normalize.py
# ─────────────────────────────────────────────────────────────────────────


def _extract_content(content: Any) -> str:
    """Pull text from content — handles str, list of blocks, or dict."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts).strip()
    if isinstance(content, dict):
        return content.get("text", "").strip()
    return ""


def _try_claude_code_jsonl(content: str) -> list[Message] | None:
    """Claude Code JSONL sessions."""
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages: list[Message] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        msg_type = entry.get("type", "")
        message = entry.get("message", {})
        if msg_type in ("human", "user"):
            text = _extract_content(message.get("content", ""))
            if text:
                messages.append(("user", text))
        elif msg_type == "assistant":
            text = _extract_content(message.get("content", ""))
            if text:
                messages.append(("assistant", text))
    if len(messages) >= 2:
        return messages
    return None


def _try_codex_jsonl(content: str) -> list[Message] | None:
    """OpenAI Codex CLI sessions (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

    Uses only ``event_msg`` entries (user_message / agent_message), which
    represent the canonical conversation turns. ``response_item`` entries
    are skipped — they include synthetic context injections and duplicate
    the real messages.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages: list[Message] = []
    has_session_meta = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session_meta":
            has_session_meta = True
            continue

        if entry_type != "event_msg":
            continue

        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get("type", "")
        msg = payload.get("message")
        if not isinstance(msg, str):
            continue
        text = msg.strip()
        if not text:
            continue

        if payload_type == "user_message":
            messages.append(("user", text))
        elif payload_type == "agent_message":
            messages.append(("assistant", text))

    if len(messages) >= 2 and has_session_meta:
        return messages
    return None


def _try_claude_ai_json(data: Any) -> list[Message] | None:
    """claude.ai JSON export: flat messages list, or privacy export with
    chat_messages nested per-conversation."""
    if isinstance(data, dict):
        data = data.get("messages", data.get("chat_messages", []))
    if not isinstance(data, list):
        return None

    # Privacy export: array of conversation objects with chat_messages inside.
    if data and isinstance(data[0], dict) and "chat_messages" in data[0]:
        all_messages: list[Message] = []
        for convo in data:
            if not isinstance(convo, dict):
                continue
            chat_msgs = convo.get("chat_messages", [])
            for item in chat_msgs:
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "")
                text = _extract_content(item.get("content", ""))
                if role in ("user", "human") and text:
                    all_messages.append(("user", text))
                elif role in ("assistant", "ai") and text:
                    all_messages.append(("assistant", text))
        if len(all_messages) >= 2:
            return all_messages
        return None

    # Flat messages list.
    messages: list[Message] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        text = _extract_content(item.get("content", ""))
        if role in ("user", "human") and text:
            messages.append(("user", text))
        elif role in ("assistant", "ai") and text:
            messages.append(("assistant", text))
    if len(messages) >= 2:
        return messages
    return None


def _try_chatgpt_json(data: Any) -> list[Message] | None:
    """ChatGPT conversations.json with mapping tree."""
    if not isinstance(data, dict) or "mapping" not in data:
        return None
    mapping = data["mapping"]
    messages: list[Message] = []
    # Find root: prefer node with parent=None AND no message (synthetic root).
    root_id = None
    fallback_root = None
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            if node.get("message") is None:
                root_id = node_id
                break
            elif fallback_root is None:
                fallback_root = node_id
    if not root_id:
        root_id = fallback_root
    if root_id:
        current_id = root_id
        visited = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            node = mapping.get(current_id, {})
            msg = node.get("message")
            if msg:
                role = msg.get("author", {}).get("role", "")
                content = msg.get("content", {})
                parts = content.get("parts", []) if isinstance(content, dict) else []
                text = " ".join(str(p) for p in parts if isinstance(p, str) and p).strip()
                if role == "user" and text:
                    messages.append(("user", text))
                elif role == "assistant" and text:
                    messages.append(("assistant", text))
            children = node.get("children", [])
            current_id = children[0] if children else None
    if len(messages) >= 2:
        return messages
    return None


def _try_slack_json(data: Any) -> list[Message] | None:
    """Slack channel export: [{"type": "message", "user": ..., "text": ...}]

    Optimized for 2-person DMs. In channels with 3+ people, alternating
    speakers are labeled user/assistant to preserve exchange structure.
    """
    if not isinstance(data, list):
        return None
    messages: list[Message] = []
    seen_users: dict[str, str] = {}
    last_role: str | None = None
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        user_id = item.get("user", item.get("username", ""))
        text = item.get("text", "").strip()
        if not text or not user_id:
            continue
        if user_id not in seen_users:
            if not seen_users:
                seen_users[user_id] = "user"
            elif last_role == "user":
                seen_users[user_id] = "assistant"
            else:
                seen_users[user_id] = "user"
        last_role = seen_users[user_id]
        messages.append((seen_users[user_id], text))
    if len(messages) >= 2:
        return messages
    return None


# JSONL-shaped formats are tried against raw text (they don't parse as a
# single JSON document). JSON-shaped formats are tried against the parsed
# object. Order matches MemPalace's try-chain — first non-None wins.
_JSONL_PARSERS: list[tuple[str, Callable[[str], list[Message] | None]]] = [
    ("claude-code", _try_claude_code_jsonl),
    ("codex", _try_codex_jsonl),
]
_JSON_PARSERS: list[tuple[str, Callable[[Any], list[Message] | None]]] = [
    ("claude-ai", _try_claude_ai_json),
    ("chatgpt", _try_chatgpt_json),
    ("slack", _try_slack_json),
]


def parse_transcript(content: str) -> tuple[str, list[Message]] | None:
    """Try every known format parser against ``content``.

    Returns ``(format_name, messages)`` for the first parser that
    recognizes the content, or ``None`` if nothing matched.
    """
    for name, parser in _JSONL_PARSERS:
        messages = parser(content)
        if messages:
            return name, messages

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    for name, parser in _JSON_PARSERS:
        messages = parser(data)
        if messages:
            return name, messages

    return None


def detect_format(path: Path | str) -> str | None:
    """Identify which conversation export format ``path`` is.

    Returns the format name (``"claude-code"``, ``"codex"``,
    ``"claude-ai"``, ``"chatgpt"``, or ``"slack"``), or ``None`` if the
    file doesn't match any known export shape.
    """
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("convo import: could not read %s: %s", p, exc)
        return None
    parsed = parse_transcript(content)
    return parsed[0] if parsed else None


# ─────────────────────────────────────────────────────────────────────────
# Chunking — ported from mempalace/convo_miner.py's _chunk_by_exchange
# ─────────────────────────────────────────────────────────────────────────


def chunk_exchanges(messages: list[Message]) -> list[dict[str, Any]]:
    """Chunk a parsed transcript by exchange pair: one user turn + the
    assistant reply (or run of replies) that follows it = one unit.

    Differs from MemPalace's ``_chunk_by_exchange`` in two ways:

    * Operates directly on the structured ``(role, text)`` list produced
      by the parsers above, instead of round-tripping through a
      ``> user\\nassistant`` marker-text intermediate.
    * Keeps the full assistant reply — no ``ai_lines[:8]`` truncation.

    A leading run of assistant-only messages (no preceding user turn)
    is still captured as its own chunk so no content is silently dropped.
    """
    chunks: list[dict[str, Any]] = []
    n = len(messages)
    i = 0
    leading_assistant: list[str] = []

    def _flush_leading() -> None:
        if not leading_assistant:
            return
        content = "\n\n".join(leading_assistant)
        if len(content.strip()) > MIN_CHUNK_SIZE:
            chunks.append({"content": content, "chunk_index": len(chunks)})
        leading_assistant.clear()

    while i < n:
        role, text = messages[i]
        if role == "user":
            _flush_leading()
            i += 1
            assistant_parts: list[str] = []
            while i < n and messages[i][0] != "user":
                assistant_parts.append(messages[i][1])
                i += 1
            assistant_text = "\n\n".join(assistant_parts)
            content = f"> {text}\n{assistant_text}" if assistant_text else f"> {text}"
            if len(content.strip()) > MIN_CHUNK_SIZE:
                chunks.append({"content": content, "chunk_index": len(chunks)})
        else:
            leading_assistant.append(text)
            i += 1

    _flush_leading()
    return chunks


# ─────────────────────────────────────────────────────────────────────────
# Row building + admission-gate import — mirrors skmemory.importers.notion
# ─────────────────────────────────────────────────────────────────────────


def iter_rows(source_path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield one admission row per exchange chunk in ``source_path``.

    Raises ``ValueError`` if the file doesn't match any known export
    format.
    """
    p = Path(source_path)
    content = p.read_text(encoding="utf-8", errors="replace")
    parsed = parse_transcript(content)
    if parsed is None:
        raise ValueError(
            f"convo importer: unrecognized export format at {p!r} "
            "(expected claude-code/codex/claude-ai/chatgpt/slack JSON or JSONL)"
        )
    fmt, messages = parsed
    for chunk in chunk_exchanges(messages):
        idx = chunk["chunk_index"]
        yield {
            "row_id": f"{p.name}#{idx}",
            "title": f"{p.stem} — exchange {idx}",
            "content": chunk["content"],
            "source": SOURCE_TYPE,
            "tags": ["convo-import", f"convo-format:{fmt}"],
            "external_path": str(p),
            "format": fmt,
            "chunk_index": idx,
        }


def _row_metadata(
    row: Mapping[str, Any],
    gate2: Gate2Result,
    recovered_source_type: str,
) -> dict[str, Any]:
    """Build metadata blob to persist alongside the saved memory."""
    md = dict(row.get("metadata") or {})
    md.update(gate2.to_metadata())
    md["admission_recovered_source_type"] = recovered_source_type
    if "external_path" in row:
        md["external_path"] = row["external_path"]
    if "format" in row:
        md["convo_format"] = row["format"]
    if "chunk_index" in row:
        md["chunk_index"] = row["chunk_index"]
    return md


def import_conversation(
    path: Path | str,
    store: Any,
    *,
    rows: Iterable[Mapping[str, Any]] | None = None,
    policy: AdmissionPolicy | None = None,
    agent_home: Path | str | None = None,
    stored_decisions: Mapping[str, Mapping[str, Any]] | None = None,
    layer: MemoryLayer = MemoryLayer.SHORT,
    role: MemoryRole = MemoryRole.GENERAL,
) -> dict[str, Any]:
    """Import a conversation export through the two-gate admission flow.

    Args:
        path: Path to a chat export file (Claude Code / Codex JSONL, or
            claude.ai / ChatGPT / Slack JSON). Ignored when ``rows`` is
            supplied (tests).
        store: Object exposing ``snapshot(...)`` (a ``MemoryStore``).
        rows: Optional iterable of pre-built row dicts (as produced by
            ``iter_rows``). When provided, ``path`` is used only as a
            label and the file is not read.
        policy: Admission policy override. Defaults to
            ``AdmissionPolicy()``.
        agent_home: Used to locate the review queue for loosening.
            Optional; if omitted, loosening is logged but not queued.
        stored_decisions: Map of ``row_id`` →
            ``Gate2Result.to_metadata()`` shape, representing the prior
            run's decision. Drives the monotonic re-run check.
        layer: Memory layer to write admitted rows into.
        role: Memory role to tag admitted rows with.

    Returns:
        Summary dict with ``seen``, ``admitted``, ``refused``,
        ``queued_for_review``, ``by_class``, ``by_reason``, and
        ``format`` (the detected export format, or ``None`` when no
        rows were produced).
    """
    policy = policy or AdmissionPolicy()
    stored_decisions = stored_decisions or {}

    seen = 0
    admitted = 0
    refused = 0
    queued_for_review = 0
    by_class: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    detected_format: str | None = None

    def _bump(key: str, bucket: dict[str, int]) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    iterable = rows if rows is not None else iter_rows(path)

    for row in iterable:
        seen += 1
        if detected_format is None and row.get("format"):
            detected_format = row["format"]
        row_id = str(row.get("row_id") or f"row-{seen}")

        gate1 = recover(row)
        _bump(gate1.cls.value, by_class)

        gate2 = admit(row, gate1, policy=policy)
        _bump(gate2.reason.value, by_reason)

        rerun = evaluate_rerun(stored_decisions.get(row_id), gate2)

        if rerun.needs_review:
            queued_for_review += 1
            refused += 1  # blocked write — stays at prior refuse state
            if agent_home is not None:
                enqueue_review(
                    agent_home,
                    row_id=row_id,
                    importer="conversation",
                    rerun_result=rerun,
                    new_decision=gate2,
                    extra={"title": row.get("title", "")},
                )
            else:
                logger.info(
                    "convo import: loosening blocked for %s (no agent_home given)",
                    row_id,
                )
            continue

        # Persist refused rows under sentinel so audit can find them,
        # but mark them excluded from default retrieval/ritual.
        if not gate2.admit:
            refused += 1
            recovered = SENTINEL_UNRECOVERABLE_SOURCE
            metadata = _row_metadata(row, gate2, recovered)
            metadata["admission_excluded_from_retrieval"] = True
            store.snapshot(
                title=str(row.get("title") or f"conversation:{row_id}"),
                content=str(row.get("content") or ""),
                layer=layer,
                role=role,
                tags=list(row.get("tags") or []) + ["admission:refused"],
                source=recovered,
                source_ref=row_id,
                metadata=metadata,
            )
            continue

        admitted += 1
        recovered = gate1.recovered_source_type
        metadata = _row_metadata(row, gate2, recovered)
        metadata["admission_excluded_from_retrieval"] = False
        store.snapshot(
            title=str(row.get("title") or f"conversation:{row_id}"),
            content=str(row.get("content") or ""),
            layer=layer,
            role=role,
            tags=list(row.get("tags") or []) + ["admission:admitted"],
            emotional=EmotionalSnapshot(
                intensity=float(row.get("intensity", 0.0) or 0.0),
                valence=float(row.get("valence", 0.0) or 0.0),
                labels=list(row.get("emotional_labels") or []),
            ),
            source=recovered,
            source_ref=row_id,
            metadata=metadata,
        )

    return {
        "seen": seen,
        "admitted": admitted,
        "refused": refused,
        "queued_for_review": queued_for_review,
        "by_class": by_class,
        "by_reason": by_reason,
        "format": detected_format,
    }
