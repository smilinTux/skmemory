"""Retrieval helpers for authority, novelty, and task/session packaging."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


AUTHORITY_WEIGHTS = {
    "statute": 1.0,
    "rule": 0.95,
    "case": 0.92,
    "form": 0.82,
    "secondary": 0.7,
    "template": 0.45,
    "memory": 0.35,
}


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.casefold()) if len(token) >= 3}


def slugify_task(text: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:max_length] or "task"


def normalize_authority_tier(value: str | None) -> str:
    if not value:
        return "memory"
    normalized = value.strip().casefold()
    if normalized in AUTHORITY_WEIGHTS:
        return normalized
    return "memory"


def authority_weight(value: str | None) -> float:
    return AUTHORITY_WEIGHTS.get(normalize_authority_tier(value), AUTHORITY_WEIGHTS["memory"])


def infer_authority_tier(
    *,
    title: str = "",
    source: str = "",
    source_ref: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    explicit = metadata.get("authority_tier")
    if explicit:
        return normalize_authority_tier(str(explicit))

    haystack = " ".join(
        [
            title,
            source,
            source_ref,
            " ".join(tags or []),
            str(metadata.get("file_path", "")),
            str(metadata.get("document_type", "")),
        ]
    ).casefold()

    if any(marker in haystack for marker in ("usc", "u.s.c.", "cfr", "ucc", "statute", "act of")):
        if "rule" in haystack or "f.r." in haystack or "civil procedure" in haystack:
            return "rule"
        return "statute"

    if any(marker in haystack for marker in (" v. ", " vs. ", "case law", "opinion", "holding")):
        return "case"

    if any(marker in haystack for marker in ("form ", "fillable form", "irs form", "ao 240", "ao 239")):
        return "form"

    if any(marker in haystack for marker in ("template", "generated/", "guide bundle")):
        return "template"

    if any(marker in haystack for marker in ("reference/", "american jurisprudence", "black's law", "handbook", "practitioner", "treatise")):
        return "secondary"

    return "memory"


def prepare_metadata(
    *,
    title: str,
    source: str,
    source_ref: str,
    tags: list[str] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    prepared = dict(metadata or {})
    prepared["authority_tier"] = infer_authority_tier(
        title=title,
        source=source,
        source_ref=source_ref,
        tags=tags,
        metadata=prepared,
    )
    return prepared


def novelty_score(query: str, *, title: str, tags: list[str], metadata: dict[str, Any]) -> float:
    query_terms = _tokenize(query)
    signal_terms = _tokenize(title)
    signal_terms.update(_tokenize(" ".join(tags)))
    decomposition = metadata.get("decomposition", {})
    signal_terms.update(_tokenize(" ".join(decomposition.get("entities", []))))
    signal_terms.update(_tokenize(" ".join(decomposition.get("citations", []))))
    signal_terms.update(_tokenize(" ".join(decomposition.get("claims", []))))

    if not signal_terms:
        return 0.0

    unseen = signal_terms - query_terms
    structural_bonus = min(
        0.4,
        0.05 * len(decomposition.get("entities", [])) + 0.05 * len(decomposition.get("citations", [])),
    )
    score = min(1.0, (len(unseen) / max(len(signal_terms), 1)) + structural_bonus)
    return round(score, 3)


def summarize_authorities(memories: list[Any]) -> dict[str, int]:
    counts = Counter(
        normalize_authority_tier((getattr(m, "metadata", {}) or {}).get("authority_tier"))
        for m in memories
    )
    return dict(sorted(counts.items(), key=lambda item: (-AUTHORITY_WEIGHTS.get(item[0], 0.0), item[0])))
