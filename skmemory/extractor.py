"""General Extractor — auto-capture memories from conversation text.

Pure regex/keyword extraction of 5 memory types from conversations:
decisions, preferences, milestones, problems, emotional moments.
No LLM needed.

Inspired by MemPalace's general extractor pattern. Designed to supplement
explicit skmemory_snapshot calls with automatic memory capture from session
transcripts and Claude Code hooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ExtractedMemory:
    """A potential memory-worthy moment extracted from conversation text."""
    type: str           # "decision" | "preference" | "milestone" | "problem" | "emotional"
    content: str        # The extracted text
    confidence: float   # 0-1
    source_line: int    # Line number in original text


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

DECISION_PATTERNS = [
    r"\b(?:we|i)\s+(?:decided|agreed|chose|settled|went with)\b",
    r"\b(?:the decision|the call|the verdict)\s+(?:is|was)\b",
    r"\b(?:going with|sticking with|committing to)\b",
    r"\b(?:approved|rejected|vetoed|finalized)\b",
    r"\b(?:let'?s go with|let'?s use|let'?s do)\b",
]

PREFERENCE_PATTERNS = [
    r"\b(?:i|we)\s+(?:prefer|like|want|need|always|never)\b",
    r"\b(?:don'?t|do not|stop|avoid|skip)\b.*\b(?:that|this|doing|using)\b",
    r"\b(?:the right way|the best way|always use|never use)\b",
    r"\b(?:from now on|going forward|rule:?)\b",
]

MILESTONE_PATTERNS = [
    r"\b(?:shipped|deployed|launched|released|merged|completed|finished|done)\b",
    r"\bv\d+\.\d+",
    r"\b(?:first time|finally|at last|breakthrough|milestone)\b",
    r"\b(?:all tests pass|build succeeded|it works)\b",
]

PROBLEM_PATTERNS = [
    r"\b(?:bug|broken|failed|error|crash|issue|problem)\b",
    r"\b(?:doesn'?t work|won'?t|can'?t|unable to)\b",
    r"\b(?:root cause|turns out|the problem was|figured out)\b",
    r"\b(?:fix(?:ed)?|patch(?:ed)?|workaround|hotfix)\b",
]

EMOTIONAL_PATTERNS = [
    r"\b(?:love|proud|excited|grateful|happy|sad|frustrated|angry|scared)\b",
    r"\b(?:breakthrough|cloud 9|oof|feeling)\b",
    r"\b(?:this means|this matters|sacred|important to me)\b",
    r"\b(?:beautiful|incredible|amazing|awful|terrible)\b",
]

_ALL_PATTERNS: list[tuple[list[str], str]] = [
    (DECISION_PATTERNS, "decision"),
    (PREFERENCE_PATTERNS, "preference"),
    (MILESTONE_PATTERNS, "milestone"),
    (PROBLEM_PATTERNS, "problem"),
    (EMOTIONAL_PATTERNS, "emotional"),
]

# Compiled pattern cache
_COMPILED: dict[str, list[re.Pattern]] = {}


def _get_compiled(patterns: list[str]) -> list[re.Pattern]:
    """Cache compiled regex patterns."""
    key = str(id(patterns))
    if key not in _COMPILED:
        _COMPILED[key] = [re.compile(p, re.IGNORECASE) for p in patterns]
    return _COMPILED[key]


# ---------------------------------------------------------------------------
# Code line detection
# ---------------------------------------------------------------------------

_CODE_INDICATORS = frozenset([
    "import ", "from ", "def ", "class ", "return ",
    "if __name__", "#!/", "```", ">>>",
])

_CODE_CHARS = frozenset(["()", "{}", "=>", "->", "==", "!=", "<=", ">=", "+="])


def _is_code_line(line: str) -> bool:
    """Filter out lines that look like code."""
    stripped = line.strip()
    if not stripped or len(stripped) < 5:
        return True
    if any(stripped.startswith(ind) for ind in _CODE_INDICATORS):
        return True
    if stripped.startswith(("    ", "\t")) and any(c in stripped for c in _CODE_CHARS):
        return True
    # Lines that are mostly symbols/operators
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    if alpha_ratio < 0.3:
        return True
    return False


# ---------------------------------------------------------------------------
# Sentence extraction and cleanup
# ---------------------------------------------------------------------------

_MARKDOWN_RE = re.compile(r'[*_`#>~]')
_BULLET_RE = re.compile(r'^[-•]\s*')
_NUMBER_RE = re.compile(r'^\d+\.\s*')


def _extract_sentence(line: str) -> str:
    """Extract a clean sentence from a line."""
    cleaned = _MARKDOWN_RE.sub("", line).strip()
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _NUMBER_RE.sub("", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


def extract_memories(text: str, min_length: int = 20) -> list[ExtractedMemory]:
    """Extract potential memory-worthy moments from conversation text.

    Pure regex/keyword extraction — no LLM needed.
    Filters out code lines (indented, containing operators, etc.).

    Args:
        text: The conversation text to analyze.
        min_length: Minimum sentence length to consider.

    Returns:
        List of ExtractedMemory objects, deduplicated.
    """
    if not text or len(text) < min_length:
        return []

    results: list[ExtractedMemory] = []
    lines = text.split("\n")

    for i, line in enumerate(lines):
        if _is_code_line(line):
            continue

        for pattern_list, mem_type in _ALL_PATTERNS:
            compiled = _get_compiled(pattern_list)
            for pattern in compiled:
                if pattern.search(line):
                    sentence = _extract_sentence(line)
                    if len(sentence) >= min_length:
                        results.append(ExtractedMemory(
                            type=mem_type,
                            content=sentence,
                            confidence=0.6,
                            source_line=i,
                        ))
                    break  # One match per line per type

    return _deduplicate(results)


def _deduplicate(results: list[ExtractedMemory]) -> list[ExtractedMemory]:
    """Remove near-duplicate extractions."""
    seen: set[str] = set()
    unique: list[ExtractedMemory] = []
    for r in results:
        key = r.content[:30].lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
