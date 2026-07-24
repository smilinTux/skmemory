"""Query Sanitizer — prevent system prompt pollution in search embeddings.

Inspired by MemPalace's discovery that AI agents often prepend their entire
system prompt to search queries, causing the embedding vector to represent
the system prompt instead of the actual question. Their benchmarks showed
recall dropping from 89.8% to 1.0% with bloated queries.

This module provides a 4-step cascade to extract the actual search intent:
1. Passthrough if query is already short (<=200 chars)
2. Extract last question sentence (scan backwards for ?)
3. Extract last meaningful sentence
4. Tail truncation to 500 chars max
"""

from __future__ import annotations

import re

# Sentence boundary pattern: split on . ! ? followed by whitespace or end
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Common system prompt markers to strip
_SYSTEM_MARKERS = [
    "you are a",
    "you are an",
    "your role is",
    "system:",
    "instructions:",
    "## rules",
    "## context",
    "mandatory rules",
    "important:",
]

MAX_SHORT_QUERY = 200
MAX_OUTPUT_LEN = 500


def sanitize_query(raw_query: str) -> str:
    """Extract the actual search intent from a potentially bloated query.

    AI agents and MCP tools sometimes pass the full system prompt + user
    question as a single query string. This causes the embedding vector
    to represent the system prompt instead of the actual question, which
    destroys retrieval accuracy.

    Args:
        raw_query: The raw query string, possibly containing system prompt.

    Returns:
        A cleaned query string representing the actual search intent.
    """
    if not raw_query or not raw_query.strip():
        return ""

    query = raw_query.strip()

    # Step 1: Passthrough if already short
    if len(query) <= MAX_SHORT_QUERY:
        return query

    # Step 2: Try to extract the last question
    question = _extract_last_question(query)
    if question and len(question) >= 10:
        return question[:MAX_OUTPUT_LEN]

    # Step 3: Extract last meaningful sentence
    sentence = _extract_last_sentence(query)
    if sentence and len(sentence) >= 10:
        return sentence[:MAX_OUTPUT_LEN]

    # Step 4: Tail truncation
    return query[-MAX_OUTPUT_LEN:].strip()


def _extract_last_question(text: str) -> str | None:
    """Scan backwards for the last sentence ending with '?'."""
    positions = [i for i, c in enumerate(text) if c == "?"]
    if not positions:
        return None

    qmark_pos = positions[-1]

    # Walk backwards to find the start of that sentence
    start = 0
    for i in range(qmark_pos - 1, -1, -1):
        if text[i] in ".!?" and i < qmark_pos - 1:
            if i + 1 < len(text) and text[i + 1] == " ":
                start = i + 1
                break
        elif text[i] == "\n":
            start = i + 1
            break

    result = text[start : qmark_pos + 1].strip()
    result = _strip_system_prefix(result)

    return result if result else None


def _extract_last_sentence(text: str) -> str | None:
    """Extract the last meaningful sentence from the text."""
    sentences = _SENTENCE_SPLIT.split(text)
    for sentence in reversed(sentences):
        cleaned = sentence.strip()
        if len(cleaned) >= 10 and not _is_system_prompt_line(cleaned):
            return _strip_system_prefix(cleaned)
    return None


def _is_system_prompt_line(line: str) -> bool:
    """Check if a line looks like a system prompt fragment."""
    lower = line.lower().strip()
    return any(lower.startswith(marker) for marker in _SYSTEM_MARKERS)


def _strip_system_prefix(text: str) -> str:
    """Remove common system prompt prefixes from extracted text."""
    lines = text.split("\n")
    while lines and _is_system_prompt_line(lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()
