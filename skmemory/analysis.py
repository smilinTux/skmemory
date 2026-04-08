"""Analysis helpers for authority, novelty, and session briefs."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .decompose import decompose_content

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}

_AUTHORITY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("statute", ("usc", "u.s.c", "cfr", "§", "statute", "code of federal regulations")),
    ("rule", ("rule", "rules of", "civil procedure", "bankruptcy rule")),
    ("case", (" v. ", "versus", "app.", "f.2d", "f.3d", "u.s.")),
    ("form", ("form", "affidavit", "notice", "declaration", "certificate")),
    ("template", ("template", "fill in", "fillable", "case-strategies", "turnabout")),
    ("secondary", ("guide", "handbook", "practitioner", "jurisprudence", "summary")),
]


def infer_authority(title: str, content: str, source: str, source_ref: str, tags: list[str]) -> dict[str, Any]:
    """Infer a coarse authority tier for a memory or artifact."""
    haystack = " ".join(
        [
            title or "",
            source or "",
            source_ref or "",
            " ".join(tags or []),
            content[:600] if content else "",
        ]
    ).lower()
    signals: list[str] = []
    tier = "memory"
    for candidate, markers in _AUTHORITY_PATTERNS:
        if any(marker in haystack for marker in markers):
            tier = candidate
            signals = [marker for marker in markers if marker in haystack][:4]
            break

    if "seed" in (source or "").lower():
        tier = "memory"
        signals = ["seed"]

    rank_map = {
        "statute": 6,
        "rule": 5,
        "case": 5,
        "form": 4,
        "secondary": 3,
        "template": 2,
        "memory": 1,
    }
    return {
        "tier": tier,
        "rank": rank_map[tier],
        "signals": signals,
    }


def token_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9§\.-]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def score_novelty(
    signals: dict[str, list[str]],
    seen_entities: set[str],
    seen_citations: set[str],
) -> dict[str, Any]:
    """Score novelty based on entities/citations not repeated elsewhere."""
    entities = [entity for entity in signals.get("entities", []) if entity.casefold() not in seen_entities]
    citations = [
        citation for citation in signals.get("citations", []) if citation.casefold() not in seen_citations
    ]
    score = round(min(1.0, 0.12 * len(entities) + 0.18 * len(citations)), 3)
    return {
        "score": score,
        "novel_entities": entities[:6],
        "novel_citations": citations[:6],
    }


def build_query_brief(query: str, result_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a reusable issue/session brief from query plus ranked rows."""
    pivots = decompose_content(query, chunk_target=max(240, len(query) + 64), chunk_overlap=0)
    top = result_rows[:8]
    facts: list[str] = []
    defenses: list[str] = []
    deadlines: list[str] = []
    missing: list[str] = []
    actions: list[str] = []
    seen = Counter()

    for row in top:
        summary = str(row.get("summary", "")).strip()
        if summary:
            facts.append(summary[:220])
        for citation in row.get("citations", [])[:3]:
            seen[f"citation:{citation}"] += 1
        for claim in row.get("claims", [])[:3]:
            lowered = claim.lower()
            if any(marker in lowered for marker in ("must", "shall", "hearing", "deadline", "within")):
                deadlines.append(claim[:220])
            if any(marker in lowered for marker in ("vacate", "service", "jurisdiction", "exempt", "objection")):
                defenses.append(claim[:220])

    if not deadlines:
        missing.append("Exact jurisdiction and filing deadline not established from retrieved materials.")
    if not any("state" in token.lower() for token in pivots.entities):
        missing.append("State-specific procedure not identified in the query.")
    if not any(token in query.lower() for token in ("judgment", "levy", "writ", "repossession", "garnishment")):
        missing.append("Exact enforcement instrument is not explicit.")

    for citation_key, count in seen.most_common(6):
        citation = citation_key.split(":", 1)[1]
        actions.append(f"Verify controlling authority around {citation} and compare it against local procedure.")

    if not actions:
        actions.append("Confirm enforcement posture, deadlines, and exemptions before drafting a response.")

    return {
        "query": query,
        "pivots": {
            "entities": pivots.entities[:8],
            "citations": pivots.citations[:8],
            "claims": pivots.claims[:6],
        },
        "facts": facts[:6],
        "defenses": defenses[:6],
        "deadlines": deadlines[:6],
        "missing_facts": missing[:6],
        "next_actions": actions[:6],
    }
