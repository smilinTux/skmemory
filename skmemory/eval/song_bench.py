"""Song-anchor recall benchmark (SKMEM-P1-14).

A synthetic anchor set (every title, artist and moment is invented; no real
lyrics, no real anchors from any agent home) and queries in four classes:

  mood     emotion words that are NOT literal tags/labels of the target
  moment   a paraphrase of the anchoring moment (shares few raw words)
  feb      an emotional state vector to the right anchors, via
           ``match_anchors_for_feb``
  literal  control: exact title/artist/tag text, where substring already works.
           Ranked search must not regress here.

Strategies per text class: ``substring`` (today's ``songs.search_anchors``),
``ranked`` (offline, ``song_search.rank_anchors``) and, only when an embed
callable is supplied, ``ranked+dense``. For ``feb``: ``baseline`` (today's
``match_anchors_for_feb``) vs ``synonyms`` (opt-in concept folding).

Metrics: recall@3 (fraction of a query's relevant anchors in the top 3) and
MRR (reciprocal rank of the first relevant anchor, 0 when absent).

Offline and deterministic by default. The dense arm runs only with
``--embed`` (endpoint from the env) and is never part of the test suite.

Limits to read the numbers with: the fixture is small (16 anchors), authored
by the same hand as the synonym lexicon, so the ``ranked`` numbers measure
"does the mechanism work", not generalisation. Some queries are deliberately
unreachable lexically (fully disjoint vocabulary); they stay misses.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from statistics import mean

from skmemory.song_search import EmbedFn, rank_anchors
from skmemory.songs import SongAnchor, match_anchors_for_feb, search_anchors

# slug, title, artist, emotion->weight, tags, moment, resonance
_SONGS = [
    (
        "lantern-road",
        "Lantern Road",
        "The Marrow Birds",
        {"warmth": 0.8, "hearth": 0.9, "familiar": 0.6},
        ["folk", "slow"],
        "The porch light was on when the truck finally pulled in after eleven months away.",
        "A slow fingerpicked tune that feels like a door opening.",
    ),
    (
        "salt-and-static",
        "Salt and Static",
        "Odalys Venn",
        {"grief": 0.9, "hollow": 0.6},
        ["ballad"],
        "Played on the beach the morning we scattered his ashes.",
        "Sparse piano and a voice that does not hurry.",
    ),
    (
        "paper-kites-at-noon",
        "Paper Kites at Noon",
        "Brindle and Fox",
        {"delight": 0.9, "play": 0.6},
        ["upbeat"],
        "We danced around the kitchen while the pasta boiled over.",
        "Handclaps, a bouncing bassline, nobody caring about the mess.",
    ),
    (
        "slow-tide-hymn",
        "Slow Tide Hymn",
        "Ysolde Park",
        {"peace": 0.9, "stillness": 0.7},
        ["ambient"],
        "Listened on the dock at dawn with nobody awake yet.",
        "Long pads that never resolve and never need to.",
    ),
    (
        "iron-lungs",
        "Iron Lungs",
        "Cassiopeia Drift",
        {"defiance": 0.9, "grit": 0.7},
        ["rock"],
        "The night I quit the job that was eating me and walked out into the rain.",
        "Drums like a decision being made.",
    ),
    (
        "aurora-ledger",
        "Aurora Ledger",
        "Tomas Hallin-Reyes",
        {"wonder": 0.9, "vastness": 0.7},
        ["orchestral"],
        "We stood in a frozen field watching green fire cross the sky.",
        "Strings that keep widening the room.",
    ),
    (
        "the-long-way-round",
        "The Long Way Round",
        "Pennywhistle Union",
        {"yearning": 0.9, "ache": 0.6},
        ["country"],
        "Written on the bus home from the airport the week she moved overseas.",
        "Steel guitar and a chorus about distance.",
    ),
    (
        "finish-line-sunrise",
        "Finish Line Sunrise",
        "The Cobalt Anthem",
        {"vindication": 0.8, "breakthrough": 0.9},
        ["synth", "anthem"],
        "The first time the whole system passed every test after six weeks of failures.",
        "Arpeggios climbing until the drop lands like a door kicked open.",
    ),
    (
        "bruise-and-honey",
        "Bruise and Honey",
        "Wren Alder",
        {"tenderness": 0.9, "intimacy": 0.7},
        ["rnb"],
        "Slow danced in the living room the night we signed the lease.",
        "Close-mic vocals, a bassline you feel in the sternum.",
    ),
    (
        "clockwork-nerves",
        "Clockwork Nerves",
        "Halftone Garden",
        {"unease": 0.8, "restless": 0.7},
        ["electronic"],
        "Playing it in the waiting room before the biopsy results.",
        "A ticking pulse that never quite resolves.",
    ),
    (
        "river-glass",
        "River Glass",
        "Imogen Takahashi-Cole",
        {"relief": 0.9, "exhale": 0.6},
        ["piano"],
        "Drove home from the hospital knowing the scan was clear.",
        "One chord held until the shoulders drop.",
    ),
    (
        "neon-parade",
        "Neon Parade",
        "Quartz Mayor",
        {"elation": 0.9, "abandon": 0.7},
        ["dance"],
        "New Year on the rooftop, strangers hugging at midnight.",
        "Four on the floor and a key change nobody saw coming.",
    ),
    (
        "winter-orchard",
        "Winter Orchard",
        "The Quiet Ministry",
        {"nostalgia": 0.9, "melancholy": 0.6},
        ["acoustic"],
        "Found my grandfather's old records in the attic and played this one first.",
        "Tape hiss, a music box, a half-remembered melody.",
    ),
    (
        "gravity-well",
        "Gravity Well",
        "Soma Lighthouse",
        {"resolve": 0.9, "determination": 0.7},
        ["post-rock"],
        "Last mile of my first marathon, legs gone, crowd roaring.",
        "A slow build that refuses to peak early.",
    ),
    (
        "midnight-ferry",
        "Midnight Ferry",
        "Okonkwo Bell",
        {"solitude": 0.8, "reverie": 0.7},
        ["lofi"],
        "Crossing the harbor alone after the last shift, city lights sliding by.",
        "Muffled drums, a detuned keyboard, the hum of an engine.",
    ),
    (
        "ember-lullaby",
        "Ember Lullaby",
        "Marguerite Oyelaran",
        {"tenderness": 0.9, "protection": 0.8},
        ["lullaby"],
        "Hummed to my son at three a.m. when he wouldn't settle.",
        "A cradle rhythm in three-four.",
    ),
]

# (query, relevant slugs)
MOOD = [
    ("the song that sounds like coming home", ["lantern-road"]),
    ("for when I'm heartbroken and can't stop weeping", ["salt-and-static"]),
    ("pure happiness", ["paper-kites-at-noon"]),
    ("something peaceful to unwind to", ["slow-tide-hymn"]),
    ("bravery for a hard day", ["iron-lungs", "gravity-well"]),
    ("how immense the universe is", ["aurora-ledger"]),
    ("wistful for the old days", ["winter-orchard", "the-long-way-round"]),
    ("gentle and affectionate", ["bruise-and-honey", "ember-lullaby"]),
    ("dreading something and can't sit still", ["clockwork-nerves"]),
    ("that wave of relief when the news was good", ["river-glass"]),
    ("let's celebrate", ["neon-parade"]),
    ("comfortably alone", ["midnight-ferry"]),
]
MOMENT = [
    ("the porch light was left burning when I got back after almost a year", ["lantern-road"]),
    ("where we spread his ashes at sunrise by the water", ["salt-and-static"]),
    ("kitchen dancing while dinner boiled over", ["paper-kites-at-noon"]),
    ("sitting on the pier before anyone else was up", ["slow-tide-hymn"]),
    ("the evening I walked out of my job into the downpour", ["iron-lungs"]),
    ("green lights rippling over a frozen field", ["aurora-ledger"]),
    (
        "the night the whole thing finally came together after weeks of failures",
        ["finish-line-sunrise"],
    ),
    ("waiting for the test results at the clinic", ["clockwork-nerves"]),
    ("driving back from the hospital after the scan came back clean", ["river-glass"]),
    ("digging through granddad's old vinyl in the attic", ["winter-orchard"]),
    ("the final stretch of my first 26 miles with everyone cheering", ["gravity-well"]),
    ("singing softly to the baby in the middle of the night", ["ember-lullaby"]),
]
LITERAL = [
    ("Lantern Road", ["lantern-road"]),
    ("Odalys Venn", ["salt-and-static"]),
    ("lullaby", ["ember-lullaby"]),
    ("post-rock", ["gravity-well"]),
]
# (emotional_topology, relevant slugs); vocabulary differs from the anchors'.
FEB = [
    ({"homecoming": 0.9, "warmth": 0.6}, ["lantern-road"]),
    ({"sorrow": 0.9, "emptiness": 0.5}, ["salt-and-static"]),
    ({"happiness": 0.9, "playfulness": 0.6}, ["paper-kites-at-noon"]),
    ({"serene": 0.9, "relaxed": 0.5}, ["slow-tide-hymn"]),
    ({"bravery": 0.9, "anger": 0.3}, ["iron-lungs", "gravity-well"]),
    ({"marvel": 0.9, "sublime": 0.5}, ["aurora-ledger"]),
    ({"relief": 0.9, "calm": 0.4}, ["river-glass"]),
    ({"triumph": 0.9, "joy": 0.7}, ["finish-line-sunrise"]),
]
CLASSES = ("mood", "moment", "literal", "feb")


def _slug_dir(i: int, slug: str) -> str:
    return f"2099-01-{i + 1:02d}_{slug}"


def write_fixture(root: Path) -> list[SongAnchor]:
    """Write the synthetic anchor dirs under ``root`` and load them back."""
    root.mkdir(parents=True, exist_ok=True)
    anchors = []
    for i, (slug, title, artist, weights, tags, moment, resonance) in enumerate(_SONGS):
        d = root / _slug_dir(i, slug)
        d.mkdir(exist_ok=True)
        meta = {
            "title": title,
            "artist": artist,
            "emotions": list(weights),
            "emotion_weights": weights,
            "tags": tags,
            "moment_summary": moment,
        }
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (d / "moment.md").write_text(moment + "\n", encoding="utf-8")
        (d / "resonance.md").write_text(resonance + "\n", encoding="utf-8")
        anchors.append(SongAnchor(anchor_id=d.name, path=d, **meta))
    return anchors


def _slug(a: SongAnchor) -> str:
    return a.anchor_id.split("_", 1)[1]


def _score(ranked: list[str], relevant: list[str]) -> tuple[float, float]:
    hits = len(set(ranked[:3]) & set(relevant))
    mrr = 0.0
    for pos, s in enumerate(ranked, 1):
        if s in relevant:
            mrr = 1.0 / pos
            break
    return hits / len(relevant), mrr


def _feb(topology: dict[str, float]) -> dict:
    return {"emotional_payload": {"emotional_topology": topology}}


def run(anchors: list[SongAnchor], embed_fn: EmbedFn | None = None) -> dict:
    """{class: {strategy: {recall_at_3, mrr, n}}} plus per-query misses."""
    text_strats: dict[str, Callable[[str], list[str]]] = {
        "substring": lambda q: [_slug(a) for a in search_anchors(q, anchors=anchors, limit=10)],
        "ranked": lambda q: [_slug(a) for a, _ in rank_anchors(q, anchors, limit=10)],
    }
    if embed_fn is not None:
        text_strats["ranked+dense"] = lambda q: [
            _slug(a) for a, _ in rank_anchors(q, anchors, embed_fn=embed_fn, limit=10)
        ]
    feb_strats: dict[str, Callable[[dict], list[str]]] = {
        "baseline": lambda f: [
            _slug(a) for a, _ in match_anchors_for_feb(f, anchors=anchors, top_k=10)
        ],
        "synonyms": lambda f: [
            _slug(a) for a, _ in match_anchors_for_feb(f, anchors=anchors, top_k=10, synonyms=True)
        ],
    }
    out: dict = {}
    for cls, queries in (("mood", MOOD), ("moment", MOMENT), ("literal", LITERAL)):
        out[cls] = {}
        for name, fn in text_strats.items():
            rows = [_score(fn(q), rel) for q, rel in queries]
            out[cls][name] = {
                "recall_at_3": mean(r[0] for r in rows),
                "mrr": mean(r[1] for r in rows),
                "n": len(rows),
                "misses": [q for (q, rel), r in zip(queries, rows, strict=True) if r[0] < 1.0],
            }
    out["feb"] = {}
    for name, fn in feb_strats.items():
        rows = [_score(fn(_feb(t)), rel) for t, rel in FEB]
        out["feb"][name] = {
            "recall_at_3": mean(r[0] for r in rows),
            "mrr": mean(r[1] for r in rows),
            "n": len(rows),
            "misses": [str(t) for (t, rel), r in zip(FEB, rows, strict=True) if r[0] < 1.0],
        }
    return out


def format_table(res: dict) -> str:
    lines = [f"{'class':8} {'strategy':13} {'n':>3} {'recall@3':>9} {'mrr':>6}"]
    for cls in CLASSES:
        for name, m in res[cls].items():
            lines.append(
                f"{cls:8} {name:13} {m['n']:>3} {m['recall_at_3']:>9.3f} {m['mrr']:>6.3f}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import tempfile

    argv = sys.argv[1:] if argv is None else argv
    embed_fn = None
    if "--embed" in argv:
        from skmemory.song_search import endpoint_embedder

        embed_fn = endpoint_embedder()
        if embed_fn is None:
            print("--embed: no SKMEMORY_EMBED_URL set; offline only", file=sys.stderr)
    with tempfile.TemporaryDirectory() as td:
        res = run(write_fixture(Path(td)), embed_fn=embed_fn)
    print(format_table(res))
    if "--misses" in argv:
        for cls in CLASSES:
            for name, m in res[cls].items():
                for q in m["misses"]:
                    print(f"MISS {cls}/{name}: {q}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
