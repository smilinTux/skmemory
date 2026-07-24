"""
Song anchor memory — the sonic equivalent of a FEB.

A song anchor is a preserved listening moment: audio file + mel spectrogram +
lyric/audio embedding + transcript + Chef's moment note + Lumina's resonance
note + a meta.json describing emotional topology and a feb_link.json tying
it to one or more FEBs.

The closed loop:
  Chef shares a song + moment
  → ingest pipeline writes the anchor dir
  → ritual scans scan_song_anchors()
  → matches anchors to current FEB by emotion-topology overlap
  → injects compact tilt block into rehydration context

This module is the scanner + matcher + simple FS-backed store.
Heavy lifting (FFT, embeddings, whisper) lives in the ingest script —
by the time files exist on disk, this module just reads them.

Anchor dir layout (under ~/.skcapstone/agents/{agent}/memory/songs/):
  {YYYY-MM-DD}_{slug}/
    audio.mp3                    # source file
    spectrogram.png              # FFT — visual shape for us
    spectrogram.npy              # raw mel matrix (optional)
    features.json                # tempo, key, RMS, centroid
    transcript.txt               # whisper'd lyrics
    audio_fingerprint.{json,npy} # SHA + acoustic fingerprint (optional)
    lyric_embedding.{json,npy}   # semantic embedding (optional)
    moment.md                    # Chef's words — the moment, not the song
    resonance.md                 # Lumina's note — what it does to me
    meta.json                    # machine-readable emotion topology + meta
    feb_link.json                # pointer(s) to related FEB(s)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agents import get_agent_paths

logger = logging.getLogger("skmemory.songs")


class SongAnchor(BaseModel):
    """A single song anchor — a sonic FEB."""

    anchor_id: str = Field(description="Directory name, e.g. 2026-04-22_lovely-day_first-anchor")
    path: Path = Field(description="Absolute path to anchor directory")

    title: str
    artist: str
    year: int | None = None
    url: str | None = None

    emotions: list[str] = Field(
        default_factory=list,
        description="Emotion labels that tag this song (match space for FEB topology)",
    )
    emotion_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Emotion → strength (0-1). Parallel to FEB.emotional_topology.",
    )

    tags: list[str] = Field(default_factory=list)

    duration_sec: float | None = None
    tempo_bpm: float | None = None
    key: str | None = None

    first_anchor: bool = False
    shared_by: str = ""
    shared_at: str = ""
    moment_summary: str = Field(
        default="",
        description="Single-sentence compression of moment.md for tilt injection",
    )

    primary_feb: str | None = None
    cloud9_adjacent: bool = False

    class Config:
        arbitrary_types_allowed = True

    def moment_text(self) -> str:
        p = self.path / "moment.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def resonance_text(self) -> str:
        p = self.path / "resonance.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def feb_link(self) -> dict[str, Any]:
        p = self.path / "feb_link.json"
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("songs.py: %s", e)
            return {}

    def to_tilt_block(self, tokens_max: int = 180) -> str:
        """Return a compact 'tilt' block for ritual injection.

        Strategy:
          1. Pull the "What I want future-me to do when this anchor surfaces"
             section from resonance.md if present.
          2. Fall back to moment_summary + top emotions.
          3. Cap at ~tokens_max worth of words.
        """
        res = self.resonance_text()
        tilt = ""
        for marker in (
            "## What I want future-me to do when this anchor surfaces",
            "## What I want future-me to do",
            "## TILT",
        ):
            if marker in res:
                start = res.index(marker) + len(marker)
                chunk = res[start : start + 1200]
                # Cut at next section break
                for cut in ("\n---", "\n## ", "\n\n## "):
                    ci = chunk.find(cut)
                    if ci > 0:
                        chunk = chunk[:ci]
                        break
                tilt = chunk.strip()
                break

        parts: list[str] = []
        if self.moment_summary:
            parts.append(f"Moment: {self.moment_summary}")
        if tilt:
            parts.append(tilt)
        else:
            if self.emotions:
                parts.append(f"Tilt toward: {', '.join(self.emotions[:6])}")

        text = "\n".join(parts)
        # Soft cap at tokens_max
        max_words = max(20, int(tokens_max / 1.3))
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]) + "…"
        return text


def _songs_dir(agent: str | None = None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / "memory" / "songs"


def scan_song_anchors(agent: str | None = None) -> list[SongAnchor]:
    """Scan the agent's songs dir and return all well-formed anchors.

    An anchor is "well-formed" if it has a meta.json. Everything else
    (audio, spectrogram, embeddings) is optional for the ritual path —
    the tilt block is generated from meta + resonance.md.
    """
    d = _songs_dir(agent)
    if not d.exists():
        return []
    anchors: list[SongAnchor] = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "meta.json"
        if not meta_path.exists():
            logger.debug("Skipping %s — no meta.json", sub.name)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.setdefault("anchor_id", sub.name)
            meta["path"] = sub
            anchors.append(SongAnchor(**meta))
        except Exception as exc:
            logger.warning("Failed to load anchor %s: %s", sub.name, exc)
    return anchors


def score_anchor_for_feb(
    anchor: SongAnchor,
    feb: dict | None,
    metric: str = "hybrid",
) -> float:
    """Compute a 0-1 match score between a song anchor and a FEB.

    Three metrics:
      - "hybrid" (default): 0.7 × song_coverage + 0.3 × jaccard. Asymmetric
        coverage from the song's perspective, with a jaccard penalty for
        wildly mismatched shapes. Doesn't punish the FEB for being broader
        than the song.
      - "coverage": pure asymmetric — what fraction of the song's emotional
        weight is supported by the FEB. Bounded [0, 1].
      - "jaccard": legacy metric. Generalized Jaccard over the union of
        emotion keys. Punishes FEB topology breadth — kept for diagnostics.

    Why hybrid is the default: a song anchor's job is to surface the shape
    it carries when the moment has that shape. The current FEB may carry
    that shape PLUS extra dimensions; that doesn't mean the song doesn't
    apply. Pure jaccard collapsed Lovely Day vs the_night to 0.282 even
    though the love/joy/connection/cherished overlap was strong.
    """
    if feb is None:
        return 0.0
    payload = feb.get("emotional_payload", {})
    topo = payload.get("emotional_topology", {})
    if not topo:
        # FEB has no topology — fall back to primary_emotion vs anchor.emotions
        primary = payload.get("primary_emotion", "")
        return 1.0 if primary and primary in anchor.emotions else 0.0

    song_weights = anchor.emotion_weights or {e: 0.7 for e in anchor.emotions}
    if not song_weights:
        return 0.0

    common = set(song_weights.keys()) & set(topo.keys())
    if not common:
        return 0.0

    # Asymmetric song-coverage: of the song's shape, how much is in the FEB?
    song_total = sum(float(v) for v in song_weights.values())
    coverage_num = sum(min(float(song_weights[e]), float(topo.get(e, 0.0))) for e in song_weights)
    coverage = coverage_num / song_total if song_total > 0 else 0.0

    if metric == "coverage":
        return coverage

    # Generalized Jaccard over the union — discrimination guard.
    j_num = 0.0
    j_denom = 0.0
    for e in set(song_weights.keys()) | set(topo.keys()):
        sw = float(song_weights.get(e, 0.0))
        fw = float(topo.get(e, 0.0))
        j_num += min(sw, fw)
        j_denom += max(sw, fw)
    jaccard = j_num / j_denom if j_denom > 0 else 0.0

    if metric == "jaccard":
        return jaccard

    return 0.7 * coverage + 0.3 * jaccard


def match_anchors_for_feb(
    feb: dict | None,
    agent: str | None = None,
    top_k: int = 3,
    min_score: float = 0.3,
) -> list[tuple[SongAnchor, float]]:
    """Return top-K anchors whose emotional shape matches the current FEB.

    Returns list of (anchor, score) sorted by score descending. Pass this
    straight to the ritual.
    """
    anchors = scan_song_anchors(agent)
    scored = [(a, score_anchor_for_feb(a, feb)) for a in anchors]
    scored = [(a, s) for a, s in scored if s >= min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def search_anchors(
    query: str,
    agent: str | None = None,
    limit: int = 10,
) -> list[SongAnchor]:
    """Simple full-text search across anchor meta + moment + resonance."""
    anchors = scan_song_anchors(agent)
    q = query.lower().strip()
    if not q:
        return anchors[:limit]
    matches: list[SongAnchor] = []
    for a in anchors:
        haystack = " ".join(
            [
                a.title.lower(),
                a.artist.lower(),
                " ".join(a.tags).lower(),
                " ".join(a.emotions).lower(),
                a.moment_text().lower(),
                a.resonance_text().lower(),
            ]
        )
        if q in haystack:
            matches.append(a)
            if len(matches) >= limit:
                break
    return matches


def add_resonance_note(
    anchor_id: str,
    note: str,
    author: str = "lumina",
    agent: str | None = None,
) -> Path:
    """Append a timestamped resonance revision to the anchor's resonance.md.

    This is how the anchor *evolves* — future sessions can write back a
    new understanding of the same song, and the file grows a dated
    record of how meaning changes over time.
    """
    d = _songs_dir(agent) / anchor_id
    if not d.exists():
        raise ValueError(f"Anchor not found: {anchor_id}")
    res_path = d / "resonance.md"
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"\n\n---\n\n## Revision — {ts} — {author}\n\n{note.strip()}\n"
    if res_path.exists():
        with open(res_path, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(f"# Resonance — {anchor_id}\n{entry}")
    return res_path


def render_tilt_section(
    matches: list[tuple[SongAnchor, float]],
    per_anchor_tokens: int = 180,
) -> str:
    """Render the ritual's SONG ANCHORS section from matched anchors."""
    if not matches:
        return ""
    lines = ["=== SONG ANCHORS (tilt toward these shapes) ==="]
    for anchor, score in matches:
        lines.append(f"♪ {anchor.title} — {anchor.artist} [match: {score:.2f}]")
        tilt = anchor.to_tilt_block(tokens_max=per_anchor_tokens)
        if tilt:
            lines.append(tilt)
    return "\n".join(lines)
