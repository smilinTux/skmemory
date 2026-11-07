#!/usr/bin/env python3
"""Deterministic generator for the affect-v2 eval fixture (SKMEM-P1-13).

Everything here is INVENTED. skmemory is a public repo: no text comes from any
real agent store, person, host or secret. The fixture exists because corpus-v1
cannot test emotional weighting (nearly every intensity is 0.3, and its affect
class saturates at 1.0 recall).

Run:  python3 -I gen_affect_v2.py <outdir>
Out:  affect-v2-corpus.jsonl  affect-v2-queries.jsonl

Design (the point is headroom, so every improvement class starts below 1.0):
- felt_peak: topic groups of 6 memories that share topic words and one felt-sense
  sentence. They differ ONLY in intensity (0.9-1.0 gold vs 0.1-0.5 decoys) and a
  little neutral filler. The query describes the feeling and never names the topic.
  Gold is OLDER than the decoys, so a recency channel cannot win by accident.
- felt_quiet (guard): same shape, gold is the LOWEST intensity. Affect weight
  should hurt here; it proves a weight is not just "prefer big numbers".
- factual_guard (guard): the query names a unique detail carried only by a
  low-intensity memory, with a high-intensity same-topic decoy.
- valence_pair: same topic, same intensity 0.85, valence +0.8 versus -0.8, one
  shared felt sentence. No ranking signal reads valence, so this is a declared
  limit: expect chance, not improvement.
- reinforce: same topic and intensity, gold has a real access_count and a recent
  last_accessed but an OLD created_at; decoys are newer and never accessed. Only
  the reinforcement channel can separate them, recency pushes the wrong way.
Limit of the design, stated plainly: gold is authored to be the high-intensity
member, so felt_peak measures whether a weight can express a signal that is
present, an upper bound, not evidence the signal exists in real stores.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)  # same fixture "now" as v1


def ts(days):
    return (T0 + timedelta(days=days)).isoformat()


def mid(title):
    return "a-" + hashlib.sha1(title.encode()).hexdigest()[:10]


def pick(seed, options):
    h = int.from_bytes(hashlib.sha1(seed.encode()).digest()[:4], "big")
    return options[h % len(options)]


FILLER = [
    "The notes were kept in the shared binder.",
    "Someone brought tea and the room stayed warm.",
    "The schedule slipped by about an hour.",
    "Two of the helpers left early.",
    "The weather outside stayed grey and calm.",
    "A second pass was planned for the following week.",
    "The measurements were written on the wall chart.",
    "Nobody remembered to take photographs.",
]

# (topic title, shared topic sentence, felt sentence, felt query that never names the topic)
PEAK = [
    ("Lantern workshop", "We rebuilt the brass lantern in the old boathouse workshop.",
     "At the end we both started to cry.", "what made us both cry"),
    ("Tide chart survey", "We finished mapping the estuary tide chart from the kayak.",
     "For one hour everything finally clicked into place.", "the night everything clicked"),
    ("Orchard graft", "We grafted the old pear branch onto the young rootstock.",
     "We laughed until our ribs ached.", "when we laughed until it hurt"),
    ("Kite festival", "We flew the long dragon kite above the cliff meadow.",
     "Everyone went silent and held their breath.", "the moment we all held our breath"),
    ("Clock repair", "We reassembled the grandfather clock from a crate of loose gears.",
     "I felt a wave of pure relief wash over me.", "the wave of relief I felt"),
    ("Choir rehearsal", "The choir rehearsed the harvest hymn in the stone chapel.",
     "A shiver ran down every spine in the room.", "a shiver down every spine"),
    ("Bridge painting", "We repainted the footbridge railings blue before the market.",
     "I was overcome with a fierce quiet pride.", "that fierce quiet pride"),
    ("Seed library", "We catalogued the donated heirloom seeds into the library drawers.",
     "We hugged each other and could not speak.", "the time we hugged and could not speak"),
    ("Radio mast", "We raised the little radio mast on the hill with ropes.",
     "My heart pounded and I could hardly stand still.", "my heart pounding so I could not stand still"),
    ("Pottery kiln", "We unloaded the first pottery kiln batch at dawn.",
     "A joyful shout went up from the whole group.", "the joyful shout from the whole group"),
    ("Night ferry", "We crossed the channel on the night ferry in thick fog.",
     "I felt wonder and a strange tender gratitude.", "that strange tender gratitude and wonder"),
    ("Snow cabin", "We dug out the snow cabin door after the long storm.",
     "Tears of laughter ran down our faces.", "tears of laughter running down our faces"),
]
QUIET = [
    ("Fence mending", "We mended the north fence along the paddock edge.",
     "The day felt ordinary and a little tedious.", "the tedious ordinary stretch"),
    ("Jar labelling", "We labelled the preserve jars for the winter shelf.",
     "Nothing much happened and nobody minded.", "when nothing much happened"),
    ("Ledger tidying", "We tidied the club ledger and the stamp drawer.",
     "It was a dull and uneventful afternoon.", "the dull uneventful afternoon"),
    ("Gutter sweep", "We swept the gutters behind the village hall.",
     "The mood stayed flat and routine all day.", "the flat routine mood"),
]
FACT = [
    ("Compost bay", "We turned the compost bay and logged the temperature.", "ticket zq-4417",
     "the compost bay turn logged under ticket zq-4417"),
    ("Water butt", "We cleaned the water butt and replaced the tap washer.", "ticket vx-2086",
     "the water butt cleaning under ticket vx-2086"),
    ("Hedge trim", "We trimmed the long hedge and bagged the clippings.", "ticket kd-9930",
     "the hedge trim recorded as ticket kd-9930"),
]
VAL = [
    ("Garden gate", "We hung the new garden gate beside the stone wall.",
     "That evening changed how all of us saw the year.",
     "the evening that changed how we saw the year, the hard version"),
    ("Dock rope", "We replaced the dock rope after the long season.",
     "I will remember that whole afternoon for a long time.",
     "the afternoon I will remember, the one that went wrong"),
    ("Barn roof", "We patched the barn roof between two squalls.",
     "Something shifted in the group that night.",
     "the night something shifted, the heavy version"),
    ("Stove flue", "We cleaned the stove flue in the cold workshop.",
     "It was a night none of us will forget.",
     "the night none of us will forget, the painful one"),
    ("Well cover", "We built a new cover for the old village well.",
     "We stood there a long while afterwards.",
     "standing there a long while afterwards, the sad one"),
    ("Loom setup", "We strung the big loom for the winter weaving.",
     "It was one of those moments that stays.",
     "one of those moments that stays, the dark one"),
]
REIN = [
    ("Cheese cave", "We aged the wheel batch in the cheese cave this season."),
    ("Boat varnish", "We varnished the rowing boat hull in the barn."),
    ("Bee frames", "We replaced the old bee frames in the hive boxes."),
    ("Wool dye", "We dyed the spring wool in the copper vat."),
    ("Cart wheel", "We rebuilt the hand cart wheel with ash spokes."),
    ("Lamp oil", "We refilled and trimmed the shed lamps with oil."),
]
REIN_CUE = [
    "the cheese cave batch", "the varnished rowing boat", "the bee frame swap",
    "the copper vat wool", "the ash spoke cart wheel", "the trimmed shed lamps",
]

C, Q = [], []


def doc(title, content, tags, created_days, intensity, valence, kind="episode",
        access_count=0, last_accessed_days=None):
    d = {
        "id": mid(title), "title": title, "content": content, "layer": "mid-term",
        "tags": list(tags), "created_at": ts(created_days), "event_at": ts(created_days),
        "kind": kind, "source_tier": "user-stated",
        "emotional": {"intensity": intensity, "valence": valence},
        "context_tag": "@public", "valid_from": ts(created_days), "valid_to": None,
        "supersedes_id": None, "schema_version": 2,
        "access_count": access_count,
        "last_accessed": None if last_accessed_days is None else ts(last_accessed_days),
    }
    C.append(d)
    return d["id"]


def query(cls, text, gold, must_not=(), gate=0.0, notes=""):
    Q.append({"id": f"qa-{cls}-{sum(1 for x in Q if x['class'] == cls) + 1:02d}", "class": cls,
              "query": text, "gold_ids": list(gold), "must_not_ids": list(must_not),
              "as_of": None, "reader": "@chef", "gate_recall_at_5": gate, "notes": notes})


def group(topic, shared, felt, gold_i, others):
    """One topic group: gold at index 0 (older), decoys newer. Same text shape."""
    ids = []
    for n, (inten, val, age) in enumerate([gold_i] + others):
        label = "gold" if n == 0 else f"v{n}"
        title = f"{topic} entry {label}"
        body = f"{shared} {pick(title, FILLER)} {felt} {pick(title + 'x', FILLER)}"
        ids.append(doc(title, body, ["affect-v2", topic.split()[0].lower()], age, inten, val))
    return ids


# felt_peak: gold 0.9-1.0 and old, five decoys at 0.1-0.5 and newer
for n, (topic, shared, felt, qtext) in enumerate(PEAK):
    hi = 0.9 + 0.01 * (n % 11)
    ids = group(topic, shared, felt, (round(hi, 2), 0.8, -(70 + 5 * n)),
                [(0.1, 0.1, -3 - n), (0.15, 0.0, -6 - n), (0.2, 0.2, -9 - n),
                 (0.3, 0.0, -12 - n), (0.5, 0.4, -20 - n)])
    query("felt_peak", qtext, [ids[0]], must_not=[], gate=0.0,
          notes="gold is the highest-intensity member of its topic group; query never names the topic")

# felt_quiet (guard): gold is the lowest intensity, decoys newer and more intense
for n, (topic, shared, felt, qtext) in enumerate(QUIET):
    ids = group(topic, shared, felt, (0.1, 0.0, -60 - 4 * n),
                [(0.9, 0.7, -4 - n), (0.7, 0.5, -8 - n), (0.5, 0.3, -12 - n),
                 (0.4, 0.2, -16 - n), (0.3, 0.1, -20 - n)])
    query("felt_quiet", qtext, [ids[0]], gate=0.0,
          notes="guard: gold is the lowest intensity; a weight that prefers big numbers should lose here")

# factual_guard: unique detail only in the low-intensity gold, same-topic high decoys
for n, (topic, shared, tick, qtext) in enumerate(FACT):
    g = doc(f"{topic} routine log", f"{shared} Recorded under {tick}. {pick(topic, FILLER)}",
            ["affect-v2", topic.split()[0].lower()], -40 - 3 * n, 0.1, 0.0, kind="fact")
    for j, (inten, val) in enumerate([(0.95, 0.8), (0.9, 0.6), (0.7, 0.5)]):
        t = f"{topic} celebration {j}"
        doc(t, f"{shared} Afterwards everyone celebrated loudly. {pick(t, FILLER)}",
            ["affect-v2", topic.split()[0].lower()], -5 - n - j, inten, val)
    query("factual_guard", qtext, [g], gate=1.0,
          notes="guard: the unique ticket token names the gold; high-intensity same-topic decoys must not displace it")

# valence_pair: identical text shape, same intensity, opposite valence (declared limit)
for n, (topic, shared, felt, qtext) in enumerate(VAL):
    a = group(topic + " A", shared, felt, (0.85, -0.8, -30 - n), [(0.85, 0.8, -31 - n)])
    # gold is the negative-valence member, which alternates position so tie order is not a hidden answer
    gold = a[0] if n % 2 == 0 else a[1]
    query("valence_pair", qtext, [gold], gate=0.0,
          notes="declared limit: no ranking signal reads valence, expect chance not improvement")

# reinforce: gold old but recently and often accessed, decoys newer and never accessed
for n, ((topic, shared), cue) in enumerate(zip(REIN, REIN_CUE)):
    gt = f"{topic} record gold"
    g = doc(gt, f"{shared} {pick(gt, FILLER)} The batch was written up in full.",
            ["affect-v2", topic.split()[0].lower()], -90 - 5 * n, 0.3, 0.0, kind="fact",
            access_count=12 + 3 * n, last_accessed_days=-2 - n)
    for j in range(3):
        t = f"{topic} record v{j + 1}"
        doc(t, f"{shared} {pick(t, FILLER)} The batch was written up in full.",
            ["affect-v2", topic.split()[0].lower()], -8 - 3 * j - n, 0.3, 0.0, kind="fact")
    query("reinforce", cue, [g], gate=0.0,
          notes="gold is old but really accessed; recency pushes the wrong way, reinforcement is the only separator")

with open(os.path.join(OUT, "affect-v2-corpus.jsonl"), "w") as f:
    for d in C:
        f.write(json.dumps(d, sort_keys=False) + "\n")
with open(os.path.join(OUT, "affect-v2-queries.jsonl"), "w") as f:
    for q in Q:
        f.write(json.dumps(q) + "\n")
print(f"corpus {len(C)} queries {len(Q)}", file=sys.stderr)
