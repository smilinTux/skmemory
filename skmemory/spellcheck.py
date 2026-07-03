#!/usr/bin/env python3
"""
spellcheck.py — Entity-gated offline spell correction for skmemory.

Ported from MemPalace's ``mempalace/spellcheck.py``. That original relied on
the third-party ``autocorrect`` package for the actual correction step; this
port drops that dependency entirely and does correction with pure stdlib
(``difflib`` + a small curated common-typo table), optionally consulting the
system dictionary (``/usr/share/dict/words``) if present — exactly as
MemPalace did for its "is this already a real word" check.

The other half of MemPalace's design — *never* correct a token that matches
a known proper name/entity — is preserved and rewired to skmemory's own
Entity graph (see ``skmemory/graph_queries.py``: ``UPSERT_ENTITY`` /
``SEARCH_BY_ENTITY``, nodes created as ``(:Entity {name: ...})`` and linked
``(:Memory)-[:MENTIONS]->(:Entity)``) instead of MemPalace's separate
``EntityRegistry`` file. There is no registry file in skmemory, so the
allow-list is passed in by the caller (``protected_terms``) rather than
loaded implicitly — see ``protected_terms_from_store()`` for the convenience
loader that pulls names straight from a store's graph backend.

This module is intentionally a standalone, OPT-IN helper. It is NOT wired
into ``MemoryStore.snapshot()`` or any other store write path — nothing
calls it automatically. A caller runs it explicitly before storing text if
it wants typo correction:

    from skmemory.spellcheck import correct_text, protected_terms_from_store

    protected = protected_terms_from_store(store)   # entity names from graph
    clean = correct_text(raw_text, protected)

Why this matters for a verbatim-first memory store: an uncorrected typo in
a key term is exactly where embedding recall dies (a misspelled term won't
match a correctly-spelled query later). But over-aggressive "correction" is
worse — it can silently mangle a name, a piece of jargon, or a technical
identifier into the wrong word. So this stays conservative by construction:

  - A token is only corrected if it's a well-known common-word typo (curated
    table) OR has exactly one, close (edit distance <= 1-2), unambiguous
    match in the system dictionary.
  - Any protected/entity term, technical token (digits, hyphens, underscores,
    CamelCase, ALL_CAPS), URL/path-like token, capitalized token (likely a
    proper noun), or already-valid word is left untouched.
  - When a fuzzy match is ambiguous (a second candidate is just as close),
    the token is left untouched rather than guessed at.

Usage:
    from skmemory.spellcheck import correct_text
    corrected = correct_text("teh skmemory server")
    # → "the skmemory server"
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Optional system word list — used to (a) skip already-valid words and
# (b) supply fuzzy-match candidates. Loaded once, cached. Absence is fine:
# correction then falls back to the curated COMMON_TYPOS table only.
# ─────────────────────────────────────────────────────────────────────────────

_system_words: Optional[set] = None
_SYSTEM_DICT = Path("/usr/share/dict/words")


def _get_system_words() -> set:
    """Load /usr/share/dict/words once and cache it. Empty set if absent."""
    global _system_words
    if _system_words is None:
        if _SYSTEM_DICT.exists():
            try:
                with open(_SYSTEM_DICT) as f:
                    _system_words = {w.strip().lower() for w in f if w.strip()}
            except OSError:
                _system_words = set()
        else:
            _system_words = set()
    return _system_words


# ─────────────────────────────────────────────────────────────────────────────
# Curated common-typo table — deterministic, portable, no dictionary file
# required. This is the primary correction path; it never depends on what
# system dictionary (if any) happens to be installed.
# ─────────────────────────────────────────────────────────────────────────────

COMMON_TYPOS: dict[str, str] = {
    "teh": "the",
    "recieve": "receive",
    "recieved": "received",
    "definately": "definitely",
    "seperate": "separate",
    "seperated": "separated",
    "occured": "occurred",
    "occuring": "occurring",
    "becuase": "because",
    "thier": "their",
    "wich": "which",
    "untill": "until",
    "wierd": "weird",
    "freind": "friend",
    "adress": "address",
    "arguement": "argument",
    "begining": "beginning",
    "beleive": "believe",
    "calender": "calendar",
    "catagory": "category",
    "cemetary": "cemetery",
    "changable": "changeable",
    "collegue": "colleague",
    "comming": "coming",
    "commited": "committed",
    "completly": "completely",
    "concious": "conscious",
    "curiousity": "curiosity",
    "embarass": "embarrass",
    "enviroment": "environment",
    "existance": "existence",
    "familar": "familiar",
    "finaly": "finally",
    "foriegn": "foreign",
    "goverment": "government",
    "grammer": "grammar",
    "harrass": "harass",
    "knoe": "know",
    "kno": "know",
    "befor": "before",
    "befroe": "before",
    "pleese": "please",
    "chekc": "check",
    "realy": "really",
    "writte": "write",
    "alredy": "already",
    "diferent": "different",
    "meny": "many",
    "tesing": "testing",
    "lsresdy": "already",
    "acheive": "achieve",
    "accross": "across",
    "aparent": "apparent",
    "apparant": "apparent",
    "assasin": "assassin",
    "basicaly": "basically",
    "beggining": "beginning",
    "buisness": "business",
    "dissapear": "disappear",
    "dissapoint": "disappoint",
    "enviromental": "environmental",
    "exagerate": "exaggerate",
    "explaination": "explanation",
    "gaurd": "guard",
    "gaurantee": "guarantee",
    "hieght": "height",
    "immediatly": "immediately",
    "independant": "independent",
    "intergrate": "integrate",
    "knowlege": "knowledge",
    "libary": "library",
    "maintainance": "maintenance",
    "millenium": "millennium",
    "neccessary": "necessary",
    "noticable": "noticeable",
    "occassion": "occasion",
    "paralel": "parallel",
    "peice": "piece",
    "posession": "possession",
    "prefered": "preferred",
    "priviledge": "privilege",
    "publically": "publicly",
    "recomend": "recommend",
    "refered": "referred",
    "relevent": "relevant",
    "reccommend": "recommend",
    "succesful": "successful",
    "supercede": "supersede",
    "tommorow": "tomorrow",
    "truely": "truly",
    "usualy": "usually",
    "wether": "whether",
}


# ─────────────────────────────────────────────────────────────────────────────
# Patterns that mark a token as "don't touch this" (never a spelling issue)
# ─────────────────────────────────────────────────────────────────────────────

_HAS_DIGIT = re.compile(r"\d")
_IS_CAMEL = re.compile(r"[A-Z][a-z]+[A-Z]")
_IS_ALLCAPS = re.compile(r"^[A-Z_@#$%^&*()+=\[\]{}|<>?.:/\\]+$")
_IS_TECHNICAL = re.compile(r"[-_]")
_IS_URL = re.compile(r"https?://|www\.|/Users/|~/|\.[a-z]{2,4}$", re.IGNORECASE)
_IS_CODE_OR_EMOJI = re.compile(r"[`*_#{}\[\]\\]")

# Fuzzy dictionary matching is only attempted at this length or longer —
# short tokens are covered by the curated COMMON_TYPOS table instead, since
# fuzzy-matching 3-letter tokens against a dictionary is inherently
# ambiguous (e.g. "kno" is equally close to "no" and "know").
_MIN_LENGTH_FUZZY = 4


def _should_skip(token: str, protected_terms: frozenset) -> bool:
    """Return True if this token should never be touched by correction."""
    if not token:
        return True
    if _HAS_DIGIT.search(token):
        return True
    if _IS_CAMEL.search(token):
        return True
    if _IS_ALLCAPS.match(token):
        return True
    if _IS_TECHNICAL.search(token):
        return True
    if _IS_URL.search(token):
        return True
    if _IS_CODE_OR_EMOJI.search(token):
        return True
    if token.lower() in protected_terms:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Edit distance — used to guard against over-aggressive fuzzy correction
# ─────────────────────────────────────────────────────────────────────────────


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Core correction
# ─────────────────────────────────────────────────────────────────────────────

# Split on whitespace but keep punctuation attached to tokens.
_TOKEN_RE = re.compile(r"(\S+)")


def _fuzzy_correct(lower: str, sys_words: set) -> Optional[str]:
    """
    Best-effort dictionary fuzzy match. Returns a correction only when it's
    unambiguous: exactly one close candidate, within a tight edit-distance
    budget, with no equally-close runner-up. Returns None otherwise.
    """
    if not sys_words or len(lower) < _MIN_LENGTH_FUZZY:
        return None
    candidates = difflib.get_close_matches(lower, sys_words, n=2, cutoff=0.84)
    if not candidates:
        return None

    top = candidates[0]
    dist = _edit_distance(lower, top)
    if dist == 0:
        return None  # already a valid word under a different casing check
    max_edits = 1 if len(lower) <= 5 else 2
    if dist > max_edits:
        return None

    if len(candidates) > 1:
        dist2 = _edit_distance(lower, candidates[1])
        if dist2 <= dist:
            return None  # ambiguous — a second candidate is just as close

    return top


def correct_text(text: str, protected_terms: frozenset = frozenset()) -> str:
    """
    Spell-correct free text, never touching protected terms.

    Args:
        text: Raw text (e.g. a user message, before storing to skmemory).
        protected_terms: Lowercase names/terms to preserve verbatim — pass
            entity names from the graph via ``protected_terms_from_store()``,
            or any other allow-list the caller wants honored.

    Returns:
        Corrected text. Conservative: unless a token is a well-known typo
        or has one clear, unambiguous dictionary match, it is left
        unchanged. Never modifies protected terms, technical tokens, URLs,
        capitalized (likely proper-noun) words, or already-valid words.
    """
    if not text:
        return text

    protected = frozenset(t.lower() for t in protected_terms)
    sys_words = _get_system_words()

    def _fix(match: re.Match) -> str:
        token = match.group(0)
        stripped = token.rstrip(".,!?;:'\")")
        punct = token[len(stripped):]

        if not stripped or _should_skip(stripped, protected):
            return token

        # Only correct lowercase words — capitalized words are likely
        # proper nouns (names, acronyms mid-sentence, etc.).
        if stripped[0].isupper():
            return token

        lower = stripped.lower()

        # 1. Curated common-typo table — deterministic, no dictionary needed.
        if lower in COMMON_TYPOS:
            return COMMON_TYPOS[lower] + punct

        # 2. Already a valid word — leave it alone.
        if sys_words and lower in sys_words:
            return token

        # 3. Fuzzy dictionary match, only if unambiguous.
        corrected = _fuzzy_correct(lower, sys_words)
        if corrected is None:
            return token
        return corrected + punct

    return _TOKEN_RE.sub(_fix, text)


# ─────────────────────────────────────────────────────────────────────────────
# Protected-term allow-list sourced from skmemory's own Entity graph
# ─────────────────────────────────────────────────────────────────────────────


def protected_terms_from_store(store) -> set[str]:
    """
    Pull known entity names from ``store``'s graph backend (if any) to use
    as the protected-terms allow-list for ``correct_text()``.

    skmemory's graph backend (``SKGraphBackend``, see graph_queries.py)
    indexes ``(:Entity {name: ...})`` nodes extracted during decomposition
    and linked to memories via ``MENTIONS``. This walks that graph directly
    to list every known entity name.

    Best-effort and always safe: returns an empty set if ``store`` is None,
    has no graph backend attached, the graph backend isn't initialized/
    reachable, or anything goes wrong talking to it. Never raises.
    """
    if store is None:
        return set()

    graph = getattr(store, "graph", None)
    if graph is None:
        return set()

    try:
        ensure_initialized = getattr(graph, "_ensure_initialized", None)
        if callable(ensure_initialized) and not ensure_initialized():
            return set()

        raw_graph = getattr(graph, "_graph", None)
        if raw_graph is None:
            return set()

        result = raw_graph.query("MATCH (e:Entity) RETURN DISTINCT e.name")
        names: set[str] = set()
        for row in getattr(result, "result_set", None) or []:
            if row and row[0]:
                names.add(str(row[0]).strip().lower())
        return names
    except Exception:
        return set()
