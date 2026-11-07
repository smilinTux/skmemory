"""Ranked, offline-first search over song anchors (SKMEM-P1-14).

``songs.search_anchors`` is a lowercase substring match of the WHOLE query over
title, artist, tags, emotions, moment and resonance. A felt query ("the song
that sounds like coming home") only hits when that exact string sits inside one
field, so it almost always returns nothing.

This module ranks instead of filtering:

* field-weighted, idf-weighted token overlap (stemmed, stopwords dropped);
* emotion-synonym concepts, so "homesick" and "hearth" meet on one concept;
* optionally a dense signal from an embedding callable, fused by reciprocal
  rank. The dense arm is OPTIONAL: with no callable, or when the callable
  raises, the result is the lexical ranking and nothing else changes. Nothing
  here reads the network unless the caller hands in :func:`endpoint_embedder`.

CLAP audio embeddings are out of scope.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable

from .songs import SongAnchor, scan_song_anchors

EmbedFn = Callable[[str], list[float]]

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "one",
        "or",
        "our",
        "out",
        "play",
        "she",
        "song",
        "that",
        "the",
        "their",
        "them",
        "there",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "your",
        "like",
        "sounds",
        "sound",
        "feels",
        "feel",
        "about",
    ]
)

#: Emotion concepts. Each cluster is a set of surface terms (stemmed on use);
#: a text carries the concept when any term appears as a contiguous phrase.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "belonging": (
        "belonging",
        "home",
        "homecoming",
        "coming home",
        "hearth",
        "familiar",
        "welcome",
        "welcomed",
        "roots",
        "kin",
    ),
    "grief": (
        "grief",
        "sorrow",
        "loss",
        "mourning",
        "bereavement",
        "tears",
        "weeping",
        "sad",
        "heartbreak",
        "funeral",
        "farewell",
    ),
    "joy": (
        "joy",
        "happy",
        "happiness",
        "elated",
        "delight",
        "glee",
        "cheerful",
        "bright",
        "celebration",
        "dancing",
    ),
    "calm": (
        "calm",
        "peace",
        "peaceful",
        "serene",
        "tranquil",
        "quiet",
        "still",
        "stillness",
        "soothing",
        "settled",
        "relaxed",
    ),
    "longing": (
        "longing",
        "yearning",
        "wistful",
        "nostalgia",
        "nostalgic",
        "homesick",
        "ache",
        "missing",
        "far away",
        "distance",
    ),
    "courage": (
        "courage",
        "brave",
        "bravery",
        "defiance",
        "resolve",
        "bold",
        "stand",
        "nerve",
        "fearless",
    ),
    "awe": ("awe", "wonder", "vast", "marvel", "sublime", "immense", "cosmic"),
    "triumph": (
        "triumph",
        "victory",
        "win",
        "breakthrough",
        "overcame",
        "overcome",
        "conquered",
        "made it",
        "success",
    ),
    "tenderness": (
        "tenderness",
        "gentle",
        "soft",
        "affection",
        "love",
        "care",
        "devotion",
        "cherished",
        "intimate",
    ),
    "anxiety": (
        "anxiety",
        "nervous",
        "dread",
        "worry",
        "tense",
        "uneasy",
        "restless",
        "fear",
        "apprehension",
    ),
    "release": (
        "release",
        "relief",
        "exhale",
        "let go",
        "catharsis",
        "unburdened",
        "weight lifted",
        "surrender",
    ),
    "hope": ("hope", "hopeful", "dawn", "optimism", "sunrise", "new beginning", "fresh start"),
}

#: Field weights. Curated emotion/tag labels are the strongest signal, the
#: free-text moment next, the long resonance note and the title weakest.
_FIELD_W = {"labels": 3.0, "moment": 2.0, "title": 1.5, "resonance": 1.0}


def _stem(w: str) -> str:
    for suf in ("ingly", "ing", "edly", "ed", "ly", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _stems(text: str, *, drop_stop: bool = True) -> list[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return [_stem(w) for w in words if not (drop_stop and w in _STOP)]


def _phrase_index() -> list[tuple[str, tuple[str, ...]]]:
    out = []
    for cid, terms in CONCEPTS.items():
        for t in terms:
            out.append((cid, tuple(_stem(w) for w in re.findall(r"[a-z]+", t.lower()))))
    return out


_PHRASES = _phrase_index()


def concepts_in(text: str) -> set[str]:
    """Concept ids whose surface terms occur (as contiguous stem phrases) in text."""
    seq = _stems(text, drop_stop=False)
    found: set[str] = set()
    for cid, phrase in _PHRASES:
        n = len(phrase)
        if cid in found or n == 0:
            continue
        for i in range(len(seq) - n + 1):
            if tuple(seq[i : i + n]) == phrase:
                found.add(cid)
                break
    return found


def _fields(a: SongAnchor) -> dict[str, str]:
    return {
        "labels": " ".join([*a.emotions, *a.tags, *a.emotion_weights]),
        "moment": f"{a.moment_summary} {a.moment_text()}",
        "title": f"{a.title} {a.artist}",
        "resonance": a.resonance_text(),
    }


def _doc_text(a: SongAnchor) -> str:
    f = _fields(a)
    return f"{a.title}. {f['labels']}. {f['moment']}. {f['resonance'][:600]}".strip()


def lexical_scores(query: str, anchors: list[SongAnchor]) -> list[float]:
    """Field-weighted, idf-weighted overlap of query stems and concepts per anchor."""
    q_stems = set(_stems(query))
    q_conc = concepts_in(query)
    docs = []
    for a in anchors:
        per_field = {}
        for name, text in _fields(a).items():
            per_field[name] = (set(_stems(text)), concepts_in(text))
        docs.append(per_field)
    n = max(1, len(anchors))

    def idf(df: int) -> float:
        return math.log(1.0 + n / max(1, df))

    scores = []
    for d in docs:
        s = 0.0
        for st in q_stems:
            df = sum(1 for x in docs if any(st in f[0] for f in x.values()))
            w = max((_FIELD_W[k] for k, f in d.items() if st in f[0]), default=0.0)
            s += w * idf(df) if w else 0.0
        for c in q_conc:
            df = sum(1 for x in docs if any(c in f[1] for f in x.values()))
            w = max((_FIELD_W[k] for k, f in d.items() if c in f[1]), default=0.0)
            s += w * idf(df) if w else 0.0
        scores.append(s)
    return scores


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


def endpoint_embedder(timeout: float = 5.0) -> EmbedFn | None:
    """An embed callable for the endpoint named by the env, else None.

    Reads ``SKMEMORY_EMBED_URLS`` (first entry) or ``SKMEMORY_EMBED_URL`` and
    ``SKMEMORY_EMBED_MODEL``. Returns None when neither URL is set, so an
    offline process never tries a default host. The callable raises on any
    failure; :func:`rank_anchors` turns that into the lexical fallback.
    """
    raw = (
        os.environ.get("SKMEMORY_EMBED_URLS") or os.environ.get("SKMEMORY_EMBED_URL") or ""
    ).strip()
    url = raw.split(",")[0].strip()
    if not url:
        return None
    model = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")

    def embed(text: str) -> list[float]:
        import httpx

        r = httpx.post(url, json={"model": model, "input": text}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        vec = data.get("embeddings", [data.get("embedding")])[0]
        return [float(x) for x in vec]

    return embed


def rank_anchors(
    query: str,
    anchors: list[SongAnchor],
    *,
    embed_fn: EmbedFn | None = None,
    limit: int = 10,
    rrf_k: int = 60,
) -> list[tuple[SongAnchor, float]]:
    """Rank anchors for a free-text query, best first.

    Lexical only unless ``embed_fn`` is given. With it, lexical and dense
    rankings are fused by reciprocal rank; if the callable raises, the dense
    arm is dropped and the lexical ranking is returned unchanged.
    An empty query returns the first ``limit`` anchors with score 0.
    """
    if not query.strip():
        return [(a, 0.0) for a in anchors[:limit]]
    lex = lexical_scores(query, anchors)
    lex_rank = sorted((i for i, s in enumerate(lex) if s > 0), key=lambda i: -lex[i])
    dense_rank: list[int] = []
    if embed_fn is not None:
        try:
            qv = embed_fn(query)
            sims = [_cos(qv, embed_fn(_doc_text(a))) for a in anchors]
            dense_rank = sorted(range(len(anchors)), key=lambda i: -sims[i])
        except Exception:  # noqa: BLE001 - any embed failure means lexical only
            dense_rank = []
    if not dense_rank:
        return [(anchors[i], lex[i]) for i in lex_rank[:limit]]
    fused: dict[int, float] = {}
    for rank_list in (lex_rank, dense_rank):
        for pos, i in enumerate(rank_list):
            fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + pos + 1)
    order = sorted(fused, key=lambda i: (-fused[i], i))
    return [(anchors[i], fused[i]) for i in order[:limit]]


def search_anchors_ranked(
    query: str,
    agent: str | None = None,
    limit: int = 10,
    *,
    anchors: Iterable[SongAnchor] | None = None,
    embed_fn: EmbedFn | None = None,
) -> list[SongAnchor]:
    """Ranked counterpart of ``songs.search_anchors`` (same return shape)."""
    pool = list(anchors) if anchors is not None else scan_song_anchors(agent)
    return [a for a, _ in rank_anchors(query, pool, embed_fn=embed_fn, limit=limit)]


def canonical_weights(weights: dict[str, float]) -> dict[str, float]:
    """Fold an emotion->weight map onto concepts (max per concept).

    Labels with no concept keep their own name, so an unknown emotion still
    matches itself exactly.
    """
    out: dict[str, float] = {}
    for label, w in weights.items():
        cs = concepts_in(label) or {label.lower()}
        for c in cs:
            out[c] = max(out.get(c, 0.0), float(w))
    return out
