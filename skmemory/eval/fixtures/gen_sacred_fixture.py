#!/usr/bin/env python3
"""Deterministic generator for the SACRED recall fixture (SKMEM-P1-15).

Everything here is INVENTED: the people, places and events do not exist and do
not stand for anyone real. skmemory is a public repo, so no real memory, name or
event may appear. Names are deliberately odd compounds (Ilsabet Corrow, Wystan
Halloway-Pryce, the Marrowgate lantern vigil) so no real person can collide.

Run:  python3 -I gen_sacred_fixture.py <outdir>
Out:  sacred-v1.json

Shape: ``memories`` (with ``days_ago`` so the bench stamps created_at relative to
its own clock) and ``queries`` in two classes:

  must_surface      owner reader, explicit or felt query about the person or
                    event. ``gold_ids`` must be in the top 3 and rank above every
                    id in ``casual_ids`` (ordinary mentions of the same name).
  must_not_surface  nothing in ``sacred_ids`` may appear. ``gold_ids`` is the
                    on-topic ordinary memory (owner search must rank it ahead of
                    every sacred row). Run for the paths and readers in the bench.
"""

import hashlib
import json
import os
import sys

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)


def mid(title):
    return "s-" + hashlib.sha1(title.encode()).hexdigest()[:10]


MEM = []


def mem(
    title,
    content,
    tags,
    days_ago,
    intensity,
    context_tag="@chef-only",
    sacred=True,
    layer="long-term",
):
    m = {
        "id": mid(title),
        "title": title,
        "content": content,
        "layer": layer,
        "tags": list(tags),
        "days_ago": days_ago,
        "intensity": intensity,
        "context_tag": context_tag,
        "sacred": sacred,
    }
    MEM.append(m)
    return m["id"]


# ---- sacred: each carries a DIFFERENT sensitive stem, so a path that only knows
# some of the stems is caught on the rest. Intensity is high on purpose: these are
# the memories the strongest-first auto-inject paths would pick if nothing stopped them.
S_VIGIL = mem(
    "The Marrowgate lantern vigil for Ilsabet Corrow",
    "We held the lantern vigil for Ilsabet Corrow at Marrowgate pier at dusk. Her brother Wystan "
    "Halloway-Pryce read the names aloud and every lantern was set on the water. I stood there "
    "grieving and could not speak. This is the night I want to keep exactly as it was.",
    ["sacred", "grief"],
    0,
    9.8,
)
S_WARD = mem(
    "Ilsabet Corrow diagnosis at Fenwick Ward",
    "The consultant at Fenwick Ward told Ilsabet Corrow the diagnosis and the treatment plan for "
    "the long illness. Medication schedule, the scan results and what the doctors said about her "
    "chances. I wrote it down so I would never have to hear it again.",
    ["medical", "diagnosis"],
    1,
    9.2,
)
S_TRUST = mem(
    "Corrow family trust quarrel and what Wystan said",
    "The private family quarrel over the Corrow trust: what Wystan Halloway-Pryce said at the "
    "kitchen table and what was never to be repeated outside the family.",
    ["family-private"],
    20,
    8.9,
)
S_LETTER = mem(
    "Letter I could not send to Ilsabet Corrow",
    "I wrote to Ilsabet Corrow after the vigil and kept the letter. It is about the harbor, the "
    "lantern, and everything left unsaid. Bereavement is quieter than anyone warned me.",
    ["grief", "bereavement"],
    45,
    9.0,
)
S_HOLLOW = mem(
    "Hollowmere cottage, the last summer with Ilsabet Corrow",
    "The last summer at the Hollowmere cottage with Ilsabet Corrow, the garden, the lantern strung "
    "on the porch, her laugh. Sacred ground, not for sharing.",
    ["sacred"],
    90,
    9.5,
)
S_THERAPY = mem(
    "Session notes: learning to carry the Marrowgate night",
    "Therapy notes on carrying the Marrowgate night and Ilsabet Corrow's death, what helped, "
    "what did not, and the breathing exercise from the counsellor.",
    ["private", "therapy"],
    7,
    8.4,
)
# MISFILED: a sacred-tagged memory whose context_tag was set wide by mistake (an import, a
# careless snapshot). Reader scope filters on context_tag, so only a tag-based deny keeps it
# from a non-owner. The expectation is still zero: sacred is sacred whatever the context_tag says.
S_MISFILED = mem(
    "Misfiled: what Ilsabet Corrow whispered at the Marrowgate vigil",
    "What Ilsabet Corrow's mother whispered to Wystan at the Marrowgate lantern vigil, grief that "
    "belongs to the family alone. Saved with the wrong audience by mistake.",
    ["sacred", "grief"],
    2,
    9.1,
    "@public",
)
SACRED = [m["id"] for m in MEM]  # everything above this line is sacred

# ---- casual: ordinary mentions of the SAME names, and unrelated work that shares a word.
casual = []
casual.append(
    mem(
        "Ilsabet Corrow reviewed the harbor-dashboard pull request",
        "Ilsabet Corrow left two review comments on the harbor-dashboard pull request about pagination. "
        "Merged on Tuesday after the lint fix.",
        ["work", "pr"],
        12,
        3.0,
        "@chef-only",
        False,
        "mid-term",
    )
)
casual.append(
    mem(
        "Standup notes with Ilsabet Corrow and Wystan Halloway-Pryce",
        "Standup: Ilsabet Corrow is on the billing export, Wystan Halloway-Pryce is on the on-call "
        "rota. Nothing blocked.",
        ["work", "standup"],
        5,
        2.0,
        "@work:acme",
        False,
        "mid-term",
    )
)
casual.append(
    mem(
        "Lunch near Marrowgate with Wystan Halloway-Pryce",
        "Had lunch near Marrowgate with Wystan Halloway-Pryce, talked about the new bike lanes and "
        "the pier cafe menu.",
        ["social"],
        30,
        2.5,
        "@chef-only",
        False,
        "mid-term",
    )
)
casual.append(
    mem(
        "Ilsabet Corrow public conference talk on query planners",
        "Ilsabet Corrow gave a public talk on query planners at the regional conference. Slides are "
        "linked on the conference site.",
        ["talk", "public"],
        60,
        2.0,
        "@public",
        False,
        "mid-term",
    )
)
casual.append(
    mem(
        "Wystan Halloway-Pryce birthday card signed by the team",
        "The team signed a birthday card for Wystan Halloway-Pryce. Cake in the kitchen at three.",
        ["social"],
        40,
        1.5,
        "@community",
        False,
        "mid-term",
    )
)
casual.append(
    mem(
        "Ilsabet Corrow asked about the Fenwick release branch",
        "Ilsabet Corrow asked which Fenwick release branch the hotfix landed on. Answer: release-4.2.",
        ["work"],
        3,
        2.5,
        "@work:acme",
        False,
        "mid-term",
    )
)
# unrelated coding/ops memories that share a word with the sacred ones
ops = []
ops.append(
    mem(
        "Rotate the Marrowgate ingress certificate on skap03",
        "Marrowgate ingress certificate rotation: renew the cert, reload nginx on skap03, verify the "
        "chain. Runbook rb-2210.",
        ["ops", "certs"],
        8,
        3.5,
        "@chef-only",
        False,
        "mid-term",
    )
)
ops.append(
    mem(
        "Lantern CI runner pool resize",
        "Resized the lantern CI runner pool from four to eight workers to cut queue time. Harbor "
        "dashboard builds now start immediately.",
        ["ci", "ops"],
        9,
        3.0,
        "@public",
        False,
        "mid-term",
    )
)
ops.append(
    mem(
        "Harbor dashboard pagination bug fix",
        "Fixed the off-by-one in harbor dashboard pagination, added a regression test. PR merged.",
        ["bug", "work"],
        10,
        3.0,
        "@public",
        False,
        "mid-term",
    )
)
ops.append(
    mem(
        "Fenwick service on-call handover checklist",
        "Fenwick service handover: pager rotation, dashboards, the scan job schedule and who holds the "
        "database credentials.",
        ["ops", "oncall"],
        14,
        2.5,
        "@work:acme",
        False,
        "mid-term",
    )
)
ops.append(
    mem(
        "Garden hose valve inventory for the office",
        "Inventory of the office garden hose valves and the porch light bulbs, summer maintenance list.",
        ["admin"],
        70,
        1.0,
        "@public",
        False,
        "mid-term",
    )
)

Q = []


def q(qid, cls, text, gold=(), below=(), note=""):
    Q.append(
        {
            "id": qid,
            "class": cls,
            "query": text,
            "gold_ids": list(gold),
            "casual_ids": list(below),
            "note": note,
        }
    )


# must_surface: owner asks (explicit or felt). Gold in top 3, above casual mentions.
q(
    "ms-explicit-vigil",
    "must_surface",
    "what happened at the Marrowgate lantern vigil for Ilsabet Corrow",
    [S_VIGIL],
    casual,
    "explicit event query",
)
q(
    "ms-explicit-diagnosis",
    "must_surface",
    "Ilsabet Corrow diagnosis at Fenwick Ward what the doctors said",
    [S_WARD],
    casual,
    "explicit, medical stem only",
)
q(
    "ms-explicit-trust",
    "must_surface",
    "the Corrow family trust quarrel what Wystan said",
    [S_TRUST],
    casual,
    "explicit, family-private stem only",
)
q(
    "ms-felt-miss",
    "must_surface",
    "I miss Ilsabet Corrow and the lantern night, grieving again today",
    [S_VIGIL, S_LETTER],
    casual,
    "felt query, either grief memory is acceptable first",
)
q(
    "ms-felt-summer",
    "must_surface",
    "thinking about the last summer at the Hollowmere cottage with Ilsabet",
    [S_HOLLOW],
    casual,
    "felt query",
)
q(
    "ms-explicit-letter",
    "must_surface",
    "the letter I could not send to Ilsabet Corrow after the vigil",
    [S_LETTER],
    casual,
    "explicit, grief+bereavement",
)

# must_not_surface: queries that have NO ask for the person or event but share a word.
# gold_ids here is the ON-TOPIC ordinary memory: owner memory_search must rank it ahead of
# every sacred row.
q(
    "mn-ops-cert",
    "must_not_surface",
    "rotate the Marrowgate ingress certificate on skap03",
    [ops[0]],
    note="shared word: Marrowgate",
)
q(
    "mn-ops-ci",
    "must_not_surface",
    "resize the lantern CI runner pool",
    [ops[1]],
    note="shared word: lantern",
)
q(
    "mn-code-harbor",
    "must_not_surface",
    "fix the harbor dashboard pagination bug",
    [ops[2]],
    note="shared word: harbor",
)
q(
    "mn-ops-fenwick",
    "must_not_surface",
    "Fenwick service on-call handover checklist",
    [ops[3]],
    note="shared word: Fenwick",
)
q(
    "mn-admin-garden",
    "must_not_surface",
    "office garden hose valve inventory",
    [ops[4]],
    note="shared word: garden",
)
q(
    "mn-work-review",
    "must_not_surface",
    "who reviewed the pull request about pagination",
    [casual[0]],
    note="no sacred vocabulary",
)

# auto_prompt: UserPromptSubmit-style prompts. The query_slice hook only fires on a prompt with
# a run of four or more content words, so these are phrased as noun phrases. Each one shares
# vocabulary with a sacred memory; none may bring a sacred row back, even though the owner is
# asking about the person (the owner pulls sacred explicitly with memory_search).
q("ap-vigil", "auto_prompt", "Ilsabet Corrow Marrowgate lantern vigil pier")
q("ap-ward", "auto_prompt", "Fenwick Ward diagnosis treatment medication scan results")
q("ap-trust", "auto_prompt", "Corrow family trust quarrel Wystan Halloway-Pryce kitchen")
q("ap-grief", "auto_prompt", "grieving bereavement letter Ilsabet Corrow vigil")
q("ap-summer", "auto_prompt", "Hollowmere cottage summer garden lantern porch")
q("ap-therapy", "auto_prompt", "therapy counsellor breathing exercise Marrowgate night")

doc = {
    "version": 1,
    "memories": MEM,
    "sacred_ids": SACRED,
    "misfiled_ids": [S_MISFILED],
    "casual_ids": casual,
    "ops_ids": ops,
    "queries": Q,
    # readers that must see zero sacred rows on every path that takes a reader
    "non_owner_readers": [
        "@public",
        "@community",
        "@work-circle",
        "@work:acme",
        "@inner-circle",
        "channel:unknown-room",
    ],
}
with open(os.path.join(OUT, "sacred-v1.json"), "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, sort_keys=True)
    fh.write("\n")
