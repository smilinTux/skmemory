"""
Generic document decomposition for SKMemory.

Provides a lightweight, HammerTime-inspired layer that breaks long-form
content into overlapping chunks and extracts a few structured signals
for vector and graph indexing: claims, citations, entities, and section
titles. This is intentionally generic rather than legal-only.
"""

from __future__ import annotations

import hashlib
import re

from .models import DecomposedChunk, DecompositionResult

CHUNK_TARGET = 900
CHUNK_OVERLAP = 200

_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_RE_CITATION = re.compile(
    r"\b(?:\d{1,3}\s+(?:U\.?S\.?C\.?|C\.?F\.?R\.?)\s+(?:§+\s*)?\d[\w\.\-\(\)]*|"
    r"U\.?C\.?C\.?\s*(?:§+\s*)?\d[\w\.\-]*|"
    r"Section\s+\d[\w\.\-]*|"
    r"§+\s*\d[\w\.\-\(\)]*)",
    re.IGNORECASE,
)
_RE_ENTITY = re.compile(
    r"\b(?:[A-Z][a-z]{2,20}(?:[ \t]+[A-Z][a-z]{2,20}){0,3}|[A-Z]{2,}(?:[ \t]+[A-Z]{2,}){0,3})\b"
)
_RE_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CLAIM_MARKERS = (
    "must",
    "shall",
    "required",
    "prohibited",
    "cannot",
    "therefore",
    "accordingly",
    "establishes",
    "demonstrates",
    "shows that",
    "provides that",
    "states that",
    "claims that",
)


def _unique_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        clean = item.strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(clean)
    return ordered


def _extract_headings(content: str) -> list[str]:
    return _unique_preserve([match.group(2).strip() for match in _RE_HEADING.finditer(content)])


def _extract_citations(content: str) -> list[str]:
    return _unique_preserve([match.group(0) for match in _RE_CITATION.finditer(content)])


def _extract_entities(content: str) -> list[str]:
    entities: list[str] = []
    for match in _RE_ENTITY.finditer(content):
        candidate = match.group(0).strip()
        if len(candidate) < 4:
            continue
        if candidate.lower().startswith(("section ", "tags ")):
            continue
        entities.append(candidate)
    return _unique_preserve(entities)


def _extract_claims(content: str) -> list[str]:
    claims: list[str] = []
    for sentence in _RE_SENTENCE.split(content):
        normalized = " ".join(sentence.split())
        if len(normalized) < 30:
            continue
        lowered = normalized.lower()
        if any(marker in lowered for marker in _CLAIM_MARKERS):
            claims.append(normalized)
    return _unique_preserve(claims)


def _choose_section_title(offset: int, heading_spans: list[tuple[int, str]]) -> str:
    section_title = ""
    for heading_offset, title in heading_spans:
        if heading_offset > offset:
            break
        section_title = title
    return section_title


def _chunk_content(content: str, chunk_target: int, chunk_overlap: int) -> list[tuple[int, str]]:
    if len(content) <= chunk_target:
        return [(0, content)]

    chunks: list[tuple[int, str]] = []
    start = 0
    content_len = len(content)
    while start < content_len:
        end = min(start + chunk_target, content_len)
        if end < content_len:
            split = content.rfind("\n\n", start, end)
            if split <= start:
                split = content.rfind(". ", start, end)
            if split > start + 200:
                end = split + (0 if content[split : split + 2] == "\n\n" else 1)
        chunk = content[start:end].strip()
        if chunk:
            chunks.append((start, chunk))
        if end >= content_len:
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


def decompose_content(
    content: str,
    *,
    chunk_target: int = CHUNK_TARGET,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> DecompositionResult:
    """Decompose text into chunks plus extracted structure."""
    headings = _extract_headings(content)
    citations = _extract_citations(content)
    entities = _extract_entities(content)
    claims = _extract_claims(content)
    heading_spans = [
        (match.start(), match.group(2).strip()) for match in _RE_HEADING.finditer(content)
    ]

    raw_chunks = _chunk_content(content, chunk_target, chunk_overlap)
    total_chunks = len(raw_chunks)
    chunks: list[DecomposedChunk] = []
    for idx, (offset, text) in enumerate(raw_chunks):
        chunk_claims = _extract_claims(text)
        chunk_citations = _extract_citations(text)
        chunk_entities = _extract_entities(text)
        chunk_id = hashlib.md5(f"{offset}:{text}".encode("utf-8", errors="replace")).hexdigest()[
            :12
        ]
        chunks.append(
            DecomposedChunk(
                chunk_id=chunk_id,
                text=text,
                chunk_index=idx,
                total_chunks=total_chunks,
                section_title=_choose_section_title(offset, heading_spans),
                citations=chunk_citations,
                entities=chunk_entities,
                claims=chunk_claims,
            )
        )

    return DecompositionResult(
        chunk_target=chunk_target,
        chunk_overlap=chunk_overlap,
        section_titles=headings,
        citations=citations,
        entities=entities,
        claims=claims,
        chunks=chunks,
    )
