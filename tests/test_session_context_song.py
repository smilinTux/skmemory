"""SKMEM-P3-09: session context carries the top song-anchor tilt.

All anchors here are invented and live in a temp agent home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory import session_context, songs
from skmemory.session_context import build_context

FEB = {
    "emotional_payload": {
        "primary_emotion": "love",
        "intensity": 0.9,
        "valence": 0.9,
        "emotional_topology": {"joy": 0.9, "love": 0.9, "warmth": 0.8, "trust": 0.8},
    },
    "relationship_state": {"partners": ["A", "B"], "trust_level": 0.9, "depth_level": 9},
    "metadata": {},
}


def _anchor(songs_dir: Path, aid: str, title: str, weights: dict, **extra) -> None:
    d = songs_dir / aid
    d.mkdir(parents=True)
    meta = {
        "anchor_id": aid,
        "title": title,
        "artist": "Invented Artist",
        "emotions": list(weights),
        "emotion_weights": weights,
        "moment_summary": "A made-up moment. Second sentence is dropped.",
    }
    meta.update(extra)
    (d / "meta.json").write_text(json.dumps(meta))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    for v in (
        "SKAGENT",
        "SKCAPSTONE_AGENT",
        "SKMEMORY_AGENT",
        "SK_DEFAULT_AGENT",
        "SKMEMORY_READER",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    h = tmp_path / "agents" / "tester"
    (h / "trust" / "febs").mkdir(parents=True)
    (h / "trust" / "febs" / "a.feb").write_text(json.dumps(FEB))
    sd = h / "memory" / "songs"
    sd.mkdir(parents=True)
    monkeypatch.setattr(songs, "_songs_dir", lambda agent=None: sd)
    return h


def _build(**kw):
    return build_context(agent="tester", sections=["identity", "feb", "song"], **kw)


def test_song_line_after_feb(home):
    sd = home / "memory" / "songs"
    _anchor(sd, "a1", "Test Tune", {"joy": 0.9, "love": 0.9, "warmth": 0.8})
    res = _build()
    assert res.included == ["feb", "song"] or res.included[-2:] == ["feb", "song"]
    assert "Test Tune - Invented Artist" in res.text
    assert "A made-up moment." in res.text and "Second sentence" not in res.text
    assert "tilt: joy, love, warmth" in res.text
    assert res.text.index("Emotional state") < res.text.index("Song anchor tilt")
    assert "—" not in res.text.split("## Song anchor tilt")[1].split("\n---")[0]


def test_top_two_only_and_min_score(home):
    sd = home / "memory" / "songs"
    _anchor(sd, "a1", "Best", {"joy": 0.9, "love": 0.9, "warmth": 0.8})
    _anchor(sd, "a2", "Second", {"joy": 0.9, "love": 0.9})
    _anchor(sd, "a3", "Third", {"joy": 0.9, "trust": 0.8})
    _anchor(sd, "a4", "Unrelated", {"rage": 0.9, "dread": 0.8})
    res = _build()
    assert res.text.count("\n- ") >= 2
    song_block = res.text.split("## Song anchor tilt")[1]
    assert "Unrelated" not in song_block
    assert sum(t in song_block for t in ("Best", "Second", "Third")) == 2


def test_skipped_silently_without_anchors(home):
    res = _build()
    assert "song" not in res.included and "song" not in res.errors


def test_skipped_without_feb(home):
    (home / "trust" / "febs" / "a.feb").unlink()
    _anchor(home / "memory" / "songs", "a1", "Test Tune", {"joy": 0.9})
    assert "song" not in _build().included


def test_reader_declared_gets_no_song(home, monkeypatch):
    _anchor(home / "memory" / "songs", "a1", "Test Tune", {"joy": 0.9, "love": 0.9})
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    res = _build()
    assert "song" not in res.included and "Test Tune" not in res.text


def test_never_reads_audio(home, monkeypatch):
    sd = home / "memory" / "songs"
    _anchor(sd, "a1", "Test Tune", {"joy": 0.9, "love": 0.9})
    (sd / "a1" / "audio.mp3").write_bytes(b"\xff\xfb" * 10)
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda s: (opened.append(s), real(s))[1])
    assert "song" in _build().included
    assert not [p for p in opened if p.suffix in (".mp3", ".wav", ".flac")]


def test_budget_drops_song_after_strongest_and_before_identity(home):
    _anchor(home / "memory" / "songs", "a1", "Test Tune", {"joy": 0.9, "love": 0.9})
    order = session_context.SECTION_ORDER
    assert order.index("feb") < order.index("song") < order.index("open")
    assert order.index("song") < order.index("strongest")
    assert "song" in session_context.EVENT_SECTIONS["post-compact"]
