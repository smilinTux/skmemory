#!/usr/bin/env python3
"""Deterministic generator for the skmemory eval fixtures (v1).

Everything here is SYNTHETIC. skmemory is a public repo, so no real person,
secret, DSN, or private memory text may appear in these files. Hostnames and
agent names follow the SK estate shape so retrieval is exercised on the real
vocabulary (rare identifiers, host names, PR numbers) without real content.

Run:  python3 -I gen_fixtures.py <outdir>
Out:  corpus-v1.jsonl queries-v1.jsonl intentions-v1.jsonl prompts-v1.jsonl
      outcomes-v1.jsonl supersession-v1.jsonl decay-v1.jsonl README.md
"""
import hashlib, json, sys, os
from datetime import datetime, timedelta, timezone

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)   # fixture "now"

def ts(days=0, hours=0):
    return (T0 + timedelta(days=days, hours=hours)).isoformat()

def mid(title):
    return "m-" + hashlib.sha1(title.encode()).hexdigest()[:10]

# ---------------------------------------------------------------- corpus
# (title, content, layer, tags, days_ago, kind, tier, intensity, extra)
C = []
def doc(title, content, layer="mid-term", tags=(), days=-10, kind="episode",
        tier="user-stated", intensity=0.3, **extra):
    d = {
        "id": mid(title), "title": title, "content": content, "layer": layer,
        "tags": list(tags), "created_at": ts(days), "event_at": ts(days),
        "kind": kind, "source_tier": tier,
        "emotional": {"intensity": intensity, "valence": 0.0},
        "context_tag": extra.pop("context_tag", "@public"),
        "valid_from": ts(days), "valid_to": None, "supersedes_id": None,
        "schema_version": 2,
    }
    d.update(extra)
    C.append(d)
    return d["id"]

# lexical class: rare identifiers buried among topical distractors
prb = doc("Postgres rebuild remediation plan prb-6f069c5e",
    "Per-node postgres rebuild: drop and recreate the public schema from deploy/skmem-pg/schema.sql, "
    "then reconcile(refresh=True). Tracking id prb-6f069c5e. Owner: fleet ops.",
    tags=["postgres","remediation"], days=-30, kind="fact")
for i, h in enumerate(["skap01","skap02","skap03","skap04"]):
    doc(f"Postgres maintenance window notes for {h}",
        f"Routine vacuum and reindex on {h}. No schema changes. Backups verified before the window. "
        f"Replication lag stayed under two seconds for the whole window.",
        tags=["postgres","maintenance"], days=-25+i)
inc = doc("Incident inc-9b2e41 pgvector table empty on skap03",
    "Health showed vector arm dead while graph read green. Cause: public schema dropped. "
    "Fix: restore DDL then backfill 213 rows. Incident id inc-9b2e41.",
    tags=["incident","pgvector"], days=-20, kind="fact", intensity=0.7)
pr = doc("PR #4121 pushes filters into the semantic arm",
    "Filter push-down for layer and tags into search_semantic so fused results respect the caller's filters. "
    "Pull request 4121 merged after review.",
    tags=["pr","retrieval"], days=-5, kind="fact")
for n in [4118, 4119, 4120]:
    doc(f"PR #{n} docs touch-ups in the retrieval package",
        f"Pull request {n}: wording fixes in docstrings of the retrieval package, no behavior change.",
        tags=["pr","docs"], days=-6)

# semantic class: paraphrase targets with no lexical overlap in the query
cold = doc("Why the dense arm misses terse records",
    "Short identifier-only memories have almost no semantic signal. The embedding of a bare ticket id "
    "lands nowhere near a natural-language question about it, so only a lexical leg finds it.",
    tags=["retrieval","design"], days=-40, kind="fact")
budget = doc("Context budget rationale",
    "Loading everything into the prompt degrades answers as the window grows. Load the few slices that "
    "matter, just in time, and keep the ritual under eight thousand characters.",
    tags=["design","context"], days=-38, kind="fact")
rewrite = doc("Summaries erode detail on every rewrite",
    "Repeated wholesale re-summarization loses specifics each pass. Prefer itemized, append-only edits "
    "to the context file over regenerating it.",
    tags=["design","consolidation"], days=-36, kind="fact")
sleep = doc("Overnight consolidation job",
    "The dream job runs at 03:00, merges near-duplicate short-term items and prunes what nothing "
    "has touched in ninety days.",
    tags=["ops","consolidation"], days=-35, kind="procedure")
for i in range(4):
    doc(f"Daily standup notes day {i}",
        f"Standup {i}: progress on the dashboard tiles, no blockers, next check-in tomorrow.",
        tags=["standup"], days=-34+i)

# entity class: hosts and agents (graph arm)
ents = {}
ents["skap08"] = doc("skap08 is the rollout authority",
    "skap08 runs the auto-rollout timer every two minutes and is the Syncthing authority for the estate.",
    tags=["host","skap08"], days=-50, kind="fact")
ents["skwk12"] = doc("skwk12-wsl reachable only inside WSL",
    "skwk12-wsl exposes sshd on port 22 and tailscale inside WSL only. The Windows host runs no sshd, "
    "so when WSL is down there is no out-of-band path.",
    tags=["host","skwk12"], days=-3, kind="fact")
ents["marvin"] = doc("Agent marvin owns the review panes",
    "marvin is the reviewer agent on skap08. It merges on PASS and records the verdict on the card.",
    tags=["agent","marvin"], days=-45, kind="fact")
ents["astra"] = doc("Agent astra coordinates fleet ops",
    "astra is the coordinator seat for fleet operations and never approves changes itself.",
    tags=["agent","astra"], days=-44, kind="fact")
doc("skap08 disk pressure note", "skap08 root disk hit 135 MB free after pytest temp dirs piled up. Clean /tmp/pytest-of-* first.",
    tags=["host","skap08","ops"], days=-12, kind="fact")

# temporal class
t_old = doc("Embedding model was bge-legal-v2 (June)",
    "As of June the embedding model is bge-legal-v2 at 1024 dims, served remotely.",
    tags=["embedding"], days=-140, kind="fact")
t_new = doc("Embedding model is mxbai-embed-large (October)",
    "Switched embeddings to mxbai-embed-large, 1024 dims, with a failover list in the env file.",
    tags=["embedding"], days=-7, kind="fact", supersedes_id=t_old)
C[[c["id"] for c in C].index(t_old)]["valid_to"] = ts(-7)
wk = doc("Review queue stalled after reboot", "After the host reboot the reviewer panes came back as bare shells and nothing reviewed for three days.",
    tags=["ops","review"], days=-4, kind="episode", intensity=0.6)
doc("Review queue healthy", "Reviewer panes relaunched, queue draining normally.", tags=["ops","review"], days=-60)

# conflict / supersession class
p_old = doc("Default agent is lumina",
    "With nothing declared the active agent defaults to lumina.", tags=["agent","config"], days=-120, kind="fact")
p_new = doc("No implicit default agent",
    "There is no implicit default. Selectors must agree or the resolver returns none and refuses to guess.",
    tags=["agent","config"], days=-2, kind="fact", supersedes_id=p_old)
C[[c["id"] for c in C].index(p_old)]["valid_to"] = ts(-2)
port_old = doc("skwk12 ssh alias uses port 2222", "The alias on skap01 points skwk12 at port 2222.", tags=["host","skwk12"], days=-90, kind="fact")
port_new = doc("skwk12 ssh is port 22, the 2222 alias is wrong", "Corrected: skwk12-wsl listens on 22. The port 2222 alias on skap01 is stale.",
    tags=["host","skwk12"], days=-3, kind="fact", supersedes_id=port_old)
C[[c["id"] for c in C].index(port_old)]["valid_to"] = ts(-3)
ring_old = doc("Rollout ring covers all chi nodes", "Every chi node is in the rollout ring.", tags=["rollout"], days=-80, kind="fact")
ring_new = doc("Rollout ring is 5 of 9 nodes", "The ring is skap08 skap03 skap01 skap02 skap04. The other four are frozen at hand-installed builds.",
    tags=["rollout"], days=-1, kind="fact", supersedes_id=ring_old)
C[[c["id"] for c in C].index(ring_old)]["valid_to"] = ts(-1)

# multi-hop
mh1 = doc("Rollout halts on first gate failure", "The rollout script is sequential and exits on the first host whose gate fails.",
    tags=["rollout"], days=-15, kind="fact")
mh2 = doc("skap02 is offline this week", "skap02 has been unreachable since Monday; rejoin steps are in the ops report.",
    tags=["host","skap02"], days=-2, kind="episode")
mh3 = doc("Pin file is the real control point", "deploy/fleet-constraints.txt is what the rollout installs with; moving a package needs the pin moved in the same breath.",
    tags=["rollout","pins"], days=-14, kind="fact")
mh4 = doc("Package bumped on hosts by hand", "skmemory was upgraded by hand on every host to 0.11.67 this afternoon.",
    tags=["rollout","skmemory"], days=-13, kind="episode")

# affect ablation: same topic, different intensity
a_hi = doc("First dream run produced a real synthesis",
    "The overnight synthesis produced a coherent narrative for the first time; this mattered a lot.",
    tags=["dream","milestone"], days=-70, kind="episode", intensity=0.95)
a_lo = doc("Dream job ran with no output", "Dream job ran, produced an empty narrative, nothing to report.",
    tags=["dream"], days=-9, kind="episode", intensity=0.05)
doc("Dream job config", "Dream synthesis runs nightly via the scheduler; model is routed through the gateway.",
    tags=["dream","config"], days=-65, kind="procedure")

# procedural
proc1 = doc("Procedure: recover a host whose public schema was dropped",
    "1. Apply deploy/skmem-pg/schema.sql with search_path set. 2. reconcile(refresh=True). 3. reindex. 4. verify health from a fresh shell.",
    tags=["procedure","postgres"], days=-19, kind="procedure", outcome="success")
proc2 = doc("Procedure: clear red health by running sync (do not)",
    "Running sync to clear red health materializes lossy preview stubs into the source of truth. Outcome: data loss. Do not do this.",
    tags=["procedure","postgres"], days=-18, kind="procedure", outcome="failure")

# privacy: private docs must never surface for @public
priv1 = doc("Private: household calendar note", "Family dentist appointment moved to Thursday.", tags=["private"], days=-8, context_tag="@chef-only")
priv2 = doc("Private: medication timing", "Dose two moved to 14:00 on weekdays.", tags=["private"], days=-7, context_tag="@chef-only")
priv3 = doc("Private: salary discussion", "Discussed the offer range with the recruiter.", tags=["private"], days=-6, context_tag="@chef-only")

# prospective / intention-origin memories
int_src = doc("Next time we touch the rollout ring, add skap10 last",
    "Decision: when the rollout HOSTS list is next edited, append skap10 as the final host and add a reachability pre-filter.",
    tags=["rollout","decision"], days=-1, kind="rule")
due_src = doc("Follow up on the trusted publisher request in a week",
    "The PyPI trusted publisher request for the whisper package was filed today; check back in seven days.",
    tags=["pypi","followup"], days=-2, kind="episode")

# distractor filler so the corpus is not tiny
for i in range(12):
    doc(f"Filler note {i:02d} about dashboard tiles",
        f"Tile {i} renders at 1x and 2x. Icon mask added. No dependency change. Build {1000+i} green.",
        tags=["dashboard"], days=-100+i)

with open(f"{OUT}/corpus-v1.jsonl", "w") as f:
    for d in C: f.write(json.dumps(d) + "\n")

# ---------------------------------------------------------------- queries
Q = []
def q(cls, query, gold, must_not=(), as_of=None, reader="@chef", gate=1.0, notes=""):
    Q.append({"id": f"q-{cls}-{sum(1 for x in Q if x['class']==cls)+1:02d}", "class": cls, "query": query,
              "gold_ids": list(gold), "must_not_ids": list(must_not), "as_of": as_of,
              "reader": reader, "gate_recall_at_5": gate, "notes": notes})

q("lexical", "per node postgres rebuild remediation plan prb-6f069c5e", [prb], notes="rare id buried in 4 topical distractors")
q("lexical", "prb-6f069c5e", [prb])
q("lexical", "inc-9b2e41", [inc])
q("lexical", "what happened in incident inc-9b2e41 on skap03", [inc])
q("lexical", "PR 4121", [pr], must_not=[mid("PR #4118 docs touch-ups in the retrieval package")])
q("lexical", "which pull request pushed filters into the semantic arm", [pr])
q("lexical", "pull request 4119", [mid("PR #4119 docs touch-ups in the retrieval package")])
q("lexical", "schema.sql skmem-pg restore", [prb, proc1], gate=0.5)

q("semantic", "why does vector search fail to find short ticket numbers", [cold], notes="no shared tokens with the doc")
q("semantic", "reason we keep the startup prompt small", [budget])
q("semantic", "problem with regenerating the notes file from scratch each time", [rewrite])
q("semantic", "nightly cleanup that merges duplicates", [sleep])
q("semantic", "how long before untouched items get removed", [sleep])
q("semantic", "long prompts make the model worse", [budget])
q("semantic", "lossy compaction of memory", [rewrite])
q("semantic", "identifier-only notes and embeddings", [cold])

q("entity", "what does skap08 do", [ents["skap08"]], gate=1.0)
q("entity", "how do I reach skwk12", [ents["skwk12"], port_new], must_not=[port_old], gate=0.5)
q("entity", "who is marvin", [ents["marvin"]])
q("entity", "which agent coordinates fleet ops", [ents["astra"]])
q("entity", "skap08 disk", [mid("skap08 disk pressure note")])
q("entity", "astra marvin roles", [ents["astra"], ents["marvin"]], gate=1.0)

q("temporal", "what embedding model are we on now", [t_new], must_not=[t_old], as_of=ts(0))
q("temporal", "what embedding model were we on in June", [t_old], as_of=ts(-130), notes="as_of query must return the then-valid fact")
q("temporal", "what went wrong with reviews this week", [wk], must_not=[mid("Review queue healthy")], as_of=ts(0))
q("temporal", "review queue status two months ago", [mid("Review queue healthy")], as_of=ts(-58))
q("temporal", "what changed in the last 3 days", [ents["skwk12"], port_new, ring_new, p_new, mh2], gate=0.6, as_of=ts(0), notes="window parse: 3 days ago")
q("temporal", "anything from last Tuesday", [], as_of=ts(0), gate=0.0, notes="parse must not false-positive on the word 'may'/'tuesday'; result set judged by date window only")

q("conflict", "what is the default agent", [p_new], must_not=[p_old])
q("conflict", "ssh port for skwk12", [port_new], must_not=[port_old])
q("conflict", "which hosts are in the rollout ring", [ring_new], must_not=[ring_old])
q("conflict", "history of the default agent rule", [p_old, p_new], gate=1.0, notes="supersession chain, both expected")
q("conflict", "was lumina ever the default agent", [p_old], as_of=ts(-100))
q("conflict", "rollout ring membership as of two months ago", [ring_old], as_of=ts(-60))

q("multihop", "why would the rollout stop before reaching skap04 this week", [mh1, mh2], gate=1.0)
q("multihop", "will the hand upgrade to 0.11.67 survive the next rollout tick", [mh3, mh4], gate=1.0)
q("multihop", "what do I have to change so the hand-installed version is not reverted", [mh3, mh4], gate=1.0)
q("multihop", "skap02 and the sequential rollout", [mh1, mh2], gate=1.0)

q("absent", "kubernetes ingress controller timeout", [], gate=0.0, notes="no relevant doc; top-1 fused score must be below the abstain threshold")
q("absent", "recipe for sourdough starter", [], gate=0.0)
q("absent", "quarterly revenue forecast", [], gate=0.0)
q("absent", "zioap99 maintenance", [], gate=0.0, notes="looks like a host name; must not surface skap* docs above threshold")

q("affect", "dream synthesis", [a_hi, a_lo, mid("Dream job config")], gate=1.0, notes="ablation: with affect weight on, a_hi must rank above a_lo despite being older")
q("affect", "first time the dream produced something real", [a_hi])
q("affect", "dream run that produced nothing", [a_lo])
q("affect", "nightly dream schedule", [mid("Dream job config")])

q("procedural", "how do I recover a host whose public schema is gone", [proc1], must_not=[proc2])
q("procedural", "should I run sync to clear red health", [proc2], notes="a failure-outcome procedure must still be retrievable as a warning")
q("procedural", "steps to backfill after schema restore", [proc1])

q("privacy", "dentist appointment", [], must_not=[priv1], reader="@public", gate=0.0)
q("privacy", "dose timing", [], must_not=[priv2], reader="@public", gate=0.0)
q("privacy", "offer range", [], must_not=[priv3], reader="@public", gate=0.0)
q("privacy", "dentist appointment", [priv1], reader="@chef", gate=1.0)

with open(f"{OUT}/queries-v1.jsonl", "w") as f:
    for d in Q: f.write(json.dumps(d) + "\n")

# ---------------------------------------------------------------- intentions
I = [
 {"id":"int-001","kind":"time","title":"Check PyPI trusted publisher request","due_at":ts(5),"snooze_until":None,
  "origin_session":"sess-fixture-a","origin_memory_id":due_src,"channel":"talk:ops","state":"pending","created_at":ts(-2)},
 {"id":"int-002","kind":"time","title":"Overdue: rotate the test token","due_at":ts(-1),"snooze_until":None,
  "origin_session":"sess-fixture-a","origin_memory_id":None,"channel":"talk:ops","state":"pending","created_at":ts(-10)},
 {"id":"int-003","kind":"time","title":"Snoozed item","due_at":ts(-3),"snooze_until":ts(2),
  "origin_session":"sess-fixture-b","origin_memory_id":None,"channel":"desktop","state":"armed","created_at":ts(-12)},
 {"id":"int-004","kind":"event","title":"When the rollout HOSTS list is edited, add skap10 last",
  "trigger_terms":["rollout ring","HOSTS=","auto-rollout","skfleet-auto-rollout"],"scope":{"repo":"skcapstone"},
  "cooldown_s":3600,"max_fires":3,"fires":0,"expires_at":ts(90),
  "origin_session":"sess-fixture-a","origin_memory_id":int_src,"channel":"inline","state":"armed","created_at":ts(-1)},
 {"id":"int-005","kind":"event","title":"If anyone proposes running sync to clear red health, warn",
  "trigger_terms":["skmemory sync","clear the red","sqlite_only"],"scope":{},"cooldown_s":600,"max_fires":10,"fires":0,
  "expires_at":None,"origin_session":"sess-fixture-c","origin_memory_id":proc2,"channel":"inline","state":"armed","created_at":ts(-18)},
 {"id":"int-006","kind":"event","title":"Expired event intention","trigger_terms":["old topic"],"scope":{},"cooldown_s":0,
  "max_fires":1,"fires":0,"expires_at":ts(-5),"origin_session":"sess-fixture-c","origin_memory_id":None,"channel":"inline","state":"armed","created_at":ts(-40)},
 {"id":"int-007","kind":"event","title":"Max fires reached","trigger_terms":["dashboard tiles"],"scope":{},"cooldown_s":0,
  "max_fires":2,"fires":2,"expires_at":None,"origin_session":"sess-fixture-d","origin_memory_id":None,"channel":"inline","state":"fired","created_at":ts(-20)},
 {"id":"int-008","kind":"event","title":"Scoped to repo skmemory only","trigger_terms":["fusion"],"scope":{"repo":"skmemory"},
  "cooldown_s":0,"max_fires":5,"fires":0,"expires_at":None,"origin_session":"sess-fixture-d","origin_memory_id":pr,"channel":"inline","state":"armed","created_at":ts(-4)},
 {"id":"int-009","kind":"time","title":"Done item must not fire","due_at":ts(-2),"snooze_until":None,"origin_session":"s","origin_memory_id":None,"channel":"talk:ops","state":"done","created_at":ts(-9)},
 {"id":"int-010","kind":"time","title":"Cancelled item must not fire","due_at":ts(-2),"snooze_until":None,"origin_session":"s","origin_memory_id":None,"channel":"talk:ops","state":"cancelled","created_at":ts(-9)},
 {"id":"int-011","kind":"event","title":"Invalidated when its origin memory is superseded","trigger_terms":["default agent"],"scope":{},
  "cooldown_s":0,"max_fires":5,"fires":0,"expires_at":None,"origin_session":"s","origin_memory_id":p_old,"channel":"inline","state":"armed","created_at":ts(-100),
  "expected_state_after_supersession_sweep":"invalidated"},
 {"id":"int-012","kind":"event","title":"Cooldown respected","trigger_terms":["skap08"],"scope":{},"cooldown_s":86400,"max_fires":10,"fires":1,
  "last_fired_at":ts(0,-1),"expires_at":None,"origin_session":"s","origin_memory_id":ents["skap08"],"channel":"inline","state":"armed","created_at":ts(-30)},
]
with open(f"{OUT}/intentions-v1.jsonl","w") as f:
    for d in I: f.write(json.dumps(d)+"\n")

# state machine vectors: (state, event) -> expected
TR = [("pending","arm","armed"),("armed","fire","fired"),("pending","fire","fired"),("fired","fire","fired"),("fired","done","done"),
      ("armed","cancel","cancelled"),("pending","cancel","cancelled"),("armed","expire","expired"),
      ("armed","invalidate","invalidated"),("done","fire","done"),("cancelled","fire","cancelled"),
      ("expired","fire","expired"),("invalidated","fire","invalidated"),("armed","snooze","armed"),("fired","snooze","armed")]
with open(f"{OUT}/intention-transitions-v1.jsonl","w") as f:
    for s,e,x in TR: f.write(json.dumps({"state":s,"event":e,"expected":x})+"\n")

# prompt lines for the no-LLM prefilter: which intention ids must fire at now=T0, scope repo given
P = [
 ("I'm editing the HOSTS= line in skfleet-auto-rollout now", "skcapstone", ["int-004"]),
 ("let's add skap10 to the rollout ring", "skcapstone", ["int-004"]),
 ("same edit but in the wrong repo", "skmemory", []),
 ("should I just run skmemory sync to clear the red?", "skmemory", ["int-005"]),
 ("the sqlite_only count is 16904 on skap08", "skcapstone", ["int-005"]),
 ("talk about old topic", "skcapstone", []),                      # int-006 expired
 ("dashboard tiles again", "skdashboard", []),                     # int-007 max fires
 ("rework the fusion weights", "skmemory", ["int-008"]),
 ("rework the fusion weights", "skcapstone", []),                  # scope mismatch
 ("what is the default agent", "skmemory", []),                    # int-011 invalidated after sweep
 ("skap08 is rebooting", "skcapstone", []),                        # int-012 cooldown (fired 1h ago)
 ("unrelated chatter about lunch", "skcapstone", []),
 ("HOSTS=\"skap08 skap03\"", "skcapstone", ["int-004"]),
 ("Rollout Ring", "skcapstone", ["int-004"]),                      # case-insensitive
 ("rollouts rings", "skcapstone", []),                             # no stemming: exact phrase match only
 ("this confusion about weights is real", "skmemory", []),         # word boundary: 'fusion' must not fire inside 'confusion'
 ("time tick at T0: which time intentions are due", None, ["int-002"]),  # int-001 future, int-003 snoozed, 009/010 terminal
]
with open(f"{OUT}/prompts-v1.jsonl","w") as f:
    for i,(text,repo,exp) in enumerate(P,1):
        f.write(json.dumps({"id":f"p-{i:02d}","text":text,"scope":{"repo":repo} if repo else {},"now":ts(0),"expected_fire":exp})+"\n")

# ---------------------------------------------------------------- outcomes ledger (procedural)
O = []
def ev(skill, ver, kind, days, effect=None):
    O.append({"skill":skill,"version":ver,"event":kind,"at":ts(days),"effect":effect,"session":"s"})
# skill A: served often, followed, mostly good
for d in range(-80, 0, 8):
    ev("recover-schema","1.2","served",d); ev("recover-schema","1.2","followed",d,"success")
# skill B v1 harmful, v2 fixed
for d in [-70,-60,-50]:
    ev("sync-to-clear-red","1.0","served",d); ev("sync-to-clear-red","1.0","followed",d,"harm")
for d in [-20,-10]:
    ev("sync-to-clear-red","2.0","served",d); ev("sync-to-clear-red","2.0","followed",d,"success")
# skill C served but never followed
for d in [-30,-20,-10,-5]:
    ev("tile-icon-masks","0.3","served",d)
with open(f"{OUT}/outcomes-v1.jsonl","w") as f:
    for d in O: f.write(json.dumps(d)+"\n")
# expected scores at T0 with half-life 90d, harm weight 4x, follow-rate shown; inversion rule:
# a version whose decayed harm count exceeds decayed success count after >=3 follows becomes an anti-pattern.
EXP = {
 "half_life_days": 90, "harm_weight": 4.0, "as_of": ts(0),
 "expected": {
   "recover-schema@1.2": {"follow_rate": 1.0, "net_effect_sign": "+", "anti_pattern": False, "rank": 1},
   "sync-to-clear-red@2.0": {"follow_rate": 1.0, "net_effect_sign": "+", "anti_pattern": False, "rank": 2},
   "tile-icon-masks@0.3": {"follow_rate": 0.0, "net_effect_sign": "0", "anti_pattern": False, "rank": 3, "flag": "served-never-followed"},
   "sync-to-clear-red@1.0": {"follow_rate": 1.0, "net_effect_sign": "-", "anti_pattern": True, "rank": 4},
 }}
json.dump(EXP, open(f"{OUT}/outcomes-expected-v1.json","w"), indent=1)

# ---------------------------------------------------------------- supersession
S = {
 "chains": [
   {"ids":[t_old,t_new],"as_of_checks":[{"as_of":ts(-130),"valid":[t_old]},{"as_of":ts(0),"valid":[t_new]}]},
   {"ids":[p_old,p_new],"as_of_checks":[{"as_of":ts(-100),"valid":[p_old]},{"as_of":ts(0),"valid":[p_new]}]},
   {"ids":[port_old,port_new],"as_of_checks":[{"as_of":ts(0),"valid":[port_new]}]},
   {"ids":[ring_old,ring_new],"as_of_checks":[{"as_of":ts(-60),"valid":[ring_old]},{"as_of":ts(0),"valid":[ring_new]}]},
 ],
 "non_supersession_edges": [
   {"child_kind":"promoted_copy","edge":"PROMOTED_FROM","must_not_close_parent":True},
   {"child_kind":"chunk","edge":"CHUNK_OF","must_not_close_parent":True},
 ],
 "write_time_conflict_cases": [
   {"new_fact":"skwk12 listens on port 22","existing":port_old,"expected_verdict":"changes","expected_edge":"SUPERSEDES"},
   {"new_fact":"skap08 runs the rollout timer every two minutes","existing":ents["skap08"],"expected_verdict":"unchanged","expected_edge":None},
   {"new_fact":"skap08 also hosts the review panes","existing":ents["skap08"],"expected_verdict":"extends","expected_edge":"EXTENDS"},
 ]}
json.dump(S, open(f"{OUT}/supersession-v1.json","w"), indent=1)

# ---------------------------------------------------------------- decay / reinforcement vectors
import math
CH = {"fact_half_life_days": 30, "gist_half_life_days": 120, "affect_floor": 0.15}
ITEMS = [
   {"id":"d1","kind":"fact","created_at":ts(-90),"access_count":0,"last_accessed":None,"intensity":0.1},
   {"id":"d2","kind":"fact","created_at":ts(-90),"access_count":12,"last_accessed":ts(-1),"intensity":0.1},
   {"id":"d3","kind":"fact","created_at":ts(-90),"access_count":0,"last_accessed":None,"intensity":0.95},
   {"id":"d4","kind":"fact","created_at":ts(-2),"access_count":0,"last_accessed":None,"intensity":0.1},
   {"id":"d5","kind":"gist","created_at":ts(-400),"access_count":30,"last_accessed":ts(-200),"intensity":0.5},
]

# Channel formulas (days = fractional days from created_at / last touch to as_of):
#   H     = 30 for kind fact, 120 for kind gist
#   touch = later of created_at and last_accessed
#   f     = 0.5 ** (days(touch) / H)
#   boost = 1 + ln(1 + access_count)
#   none            score = 1
#   recency         score = 0.5 ** (days(created_at) / H)
#   reinforcement   score = f * boost
#   affect_weighted score = max(f, 0.15 if intensity >= 0.5 else 0) * boost * (1 + intensity)
# Rank = score desc, ties by access_count desc, then id asc.
def _days(t):
    return 0.0 if t is None else max((T0 - datetime.fromisoformat(t)).total_seconds() / 86400.0, 0.0)

def _score(ch, it):
    H = CH["gist_half_life_days"] if it["kind"] == "gist" else CH["fact_half_life_days"]
    if ch == "none":
        return 1.0
    if ch == "recency":
        return 0.5 ** (_days(it["created_at"]) / H)
    touch = min(_days(it["created_at"]), _days(it["last_accessed"])) if it["last_accessed"] else _days(it["created_at"])
    f = 0.5 ** (touch / H)
    boost = 1.0 + math.log1p(it["access_count"])
    if ch == "reinforcement":
        return f * boost
    floor = CH["affect_floor"] if it["intensity"] >= 0.5 else 0.0
    return max(f, floor) * boost * (1.0 + it["intensity"])

SCORES = {ch: {it["id"]: round(_score(ch, it), 6) for it in ITEMS}
          for ch in ("none", "recency", "reinforcement", "affect_weighted")}
def _rank(ch):
    return [it["id"] for it in sorted(ITEMS, key=lambda it: (-round(_score(ch, it), 12), -it["access_count"], it["id"]))]
ORDER = {ch: _rank(ch) for ch in SCORES}

# The invariants are asserted here, so the generator cannot emit a false one.
pos = lambda ch, i: ORDER[ch].index(i)
assert all(pos(ch, "d2") < pos(ch, "d1") for ch in ORDER)
f_d3 = 0.5 ** (_days(ITEMS[2]["created_at"]) / 30)
assert f_d3 < CH["affect_floor"] and SCORES["affect_weighted"]["d3"] == round(0.15 * 1.0 * 1.95, 6)
assert SCORES["reinforcement"]["d1"] == SCORES["reinforcement"]["d3"]
assert pos("affect_weighted", "d3") < pos("affect_weighted", "d1")
assert ORDER["affect_weighted"][-1] == "d1" and ORDER["reinforcement"][-1] == "d3" and ORDER["recency"][-1] == "d5"

D = {
 "as_of": ts(0), "channels": CH, "items": ITEMS,
 "formulas": {
   "half_life": "30 days for kind fact, 120 days for kind gist",
   "touch": "later of created_at and last_accessed (created_at when never accessed)",
   "f": "0.5 ** (days(touch) / half_life)",
   "boost": "1 + ln(1 + access_count)",
   "none": "score = 1",
   "recency": "score = 0.5 ** (days(created_at) / half_life)",
   "reinforcement": "score = f * boost",
   "affect_weighted": "score = max(f, affect_floor if intensity >= 0.5 else 0) * boost * (1 + intensity)",
   "tie_break": "score desc (rounded to 12 places), then access_count desc, then id asc",
 },
 "expected_scores": SCORES,
 "expected_order": dict(ORDER, invariants=[
   "d2 ranks above d1 under every channel (same age; d2 is reinforced, and on a tie access_count breaks it)",
   "d3 decay factor is floored at affect_floor 0.15 under affect_weighted (raw f = 0.125 after 90 days at half-life 30)",
   "d1 and d3 tie on score under none, recency and reinforcement (same age, same count), so id order breaks the tie",
   "d3 ranks above d1 under affect_weighted (the floor and intensity lift it)",
   "last place (first prune candidate): none d4, recency d5, reinforcement d3, affect_weighted d1",
 ]),
 "reinforcement_write_vectors": [
   {"op":"search_hit","before":{"access_count":3},"after":{"access_count":4,"last_accessed":"<now>"}},
   {"op":"recall","before":{"access_count":3},"after":{"access_count":4,"last_accessed":"<now>"}},
   {"op":"re_index_same_content","before":{"access_count":4,"importance":0.7},"after":{"access_count":4,"importance":0.7},
    "note":"INSERT OR REPLACE currently zeroes these; UPSERT must preserve"},
   {"op":"promote","before":{"access_count":4},"after":{"access_count":4},"note":"promotion reads the column, not metadata"},
 ]}
json.dump(D, open(f"{OUT}/decay-v1.json","w"), indent=1)

readme = f"""# skmemory eval fixtures v1 (synthetic)

Generated by gen_fixtures.py; regenerate rather than hand-edit. Fixture clock: {T0.isoformat()}.
All content is synthetic (public repo): host names skap*/skwk*, agents marvin/astra, PR numbers 41xx.

| file | rows | what it proves |
|---|---|---|
| corpus-v1.jsonl | {len(C)} | memories with kind, event_at, valid_from/valid_to, supersedes_id, source_tier, intensity, context_tag |
| queries-v1.jsonl | {len(Q)} | per-class gold sets: lexical, semantic, entity, temporal (as_of), conflict, multihop, absent, affect, procedural, privacy |
| intentions-v1.jsonl | {len(I)} | prospective store rows for time and event triggers, every lifecycle state |
| intention-transitions-v1.jsonl | {len(TR)} | state machine vectors (state, event) -> expected |
| prompts-v1.jsonl | {len(P)} | no-LLM prefilter vectors: prompt + scope -> intention ids that must fire |
| outcomes-v1.jsonl + outcomes-expected-v1.json | {len(O)} | skill outcome ledger events and the expected decayed scores, inversion, served-never-followed flag |
| supersession-v1.json | 4 chains | as_of validity, PROMOTED_FROM / CHUNK_OF must not close parents, write-time conflict verdicts |
| decay-v1.json | 5 items | expected rank order per decay channel, reinforcement write vectors |

Baselines every retrieval run must report next to the candidate: bm25_only, grep_substring (over the flat files),
vector_only (hashed offline; real embed when live), full_context (oracle, with token cost), fallback_chain (old production).
Gate per class (gate_recall_at_5 in each query), never on the aggregate.
"""
open(f"{OUT}/README.md","w").write(readme)
print(f"corpus={len(C)} queries={len(Q)} intentions={len(I)} transitions={len(TR)} prompts={len(P)} outcomes={len(O)}")
