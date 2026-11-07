"""SKMEM-P1-04: harvest and citation-gated labelling. Synthetic data only, fully offline."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from skmemory.eval import harvest as hv
from skmemory.eval import label as lab
from skmemory.eval import live_bench as lb

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=3)).isoformat()
OLD = (NOW - timedelta(days=200)).isoformat()


def _cc_line(name, inp, ts=RECENT, sid="s1"):
    return json.dumps(
        {
            "sessionId": sid,
            "timestamp": ts,
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
        }
    )


def test_claude_harvest_tools_shell_window_dedupe(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    lines = [
        _cc_line("mcp__skmemory__memory_search", {"query": "gateway pool ceilings"}),
        _cc_line("mcp__skcapstone__memory_search", {"query": "Gateway  pool ceilings"}, sid="s2"),
        _cc_line("mcp__skmemory__memory_recall", {"query": "ritual order"}),
        _cc_line("mcp__skmemory__memory_search", {"query": "too old query"}, ts=OLD),
        _cc_line("mcp__skmemory__memory_store", {"query": "not a search"}),
        _cc_line(
            "Bash", {"command": 'cd /x && skmemory search "fusion weights" --limit 5 | head'}
        ),
        _cc_line("Bash", {"command": "skmemory recall 023ff44f"}),
    ]
    (d / "a.jsonl").write_text("\n".join(lines) + "\nnot json memory\n")
    cands, stats = hv.harvest(hv.iter_claude(d), now=NOW)
    qs = sorted(c.query for c in cands)
    assert qs == ["fusion weights", "gateway pool ceilings", "ritual order"]
    assert stats["duplicate"] == 1 and stats["outside_window"] == 1
    assert next(c for c in cands if c.query.startswith("gateway")).seen == 2
    assert all(len(c.session) == 12 and c.session not in ("s1", "s2") for c in cands)


def test_hermes_only_owner_platforms(tmp_path):
    def sess(name, platform):
        call = {"function": {"name": "mcp_skcapstone_memory_search",
                             "arguments": json.dumps({"query": f"q from {platform}"})}}  # fmt: skip
        (tmp_path / name).write_text(json.dumps(
            {"session_id": name, "platform": platform, "last_updated": RECENT,
             "messages": [{"role": "assistant", "tool_calls": [call]}]}))  # fmt: skip

    sess("session_1.json", "cli")
    sess("session_2.json", "cron")
    sess("session_3.json", "telegram")
    cands, _ = hv.harvest(hv.iter_hermes(tmp_path), now=NOW)
    assert sorted(c.query for c in cands) == ["q from cli", "q from cron"]


def test_sensitive_queries_excluded_and_counted():
    raw = [
        ("how is my liver transplant going", "s", NOW, "claude_code"),
        ("bacopa dose timeline", "s", NOW, "claude_code"),
        ("sacred memory about Sarah", "s", NOW, "claude_code"),
        ("fusion weights", "s", NOW, "claude_code"),
    ]
    cands, stats = hv.harvest(raw, now=NOW)
    assert [c.query for c in cands] == ["fusion weights"]
    assert stats["excluded_sensitive"] == 3


def test_sensitive_memory_by_tag_and_text():
    assert hv.is_sensitive_memory("t", "c", ["Sacred"])
    assert hv.is_sensitive_memory("Medical notes", "c", [])
    assert not hv.is_sensitive_memory("gateway", "pool sizes", ["infra"])
    assert not hv.is_sensitive_memory("memory_health", "embed_ok vector_ok health check", [])
    assert hv.is_sensitive_memory("t", "his health history is private", [])


def test_private_dir_refuses_repo_and_git_tree(tmp_path):
    with pytest.raises(ValueError):
        hv.check_private_dir(lab.Path(lab.__file__).resolve().parents[2] / "x")
    (tmp_path / "w" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError):
        hv.check_private_dir(tmp_path / "w" / "private")
    p = hv.ensure_private_dir(tmp_path / "ok" / "private")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o700


def test_quote_validation():
    text = "The gateway pool ceiling for codex is 32, not 4, on both gateways."
    assert lab.quote_is_valid("pool ceiling for codex is 32", text)
    assert lab.quote_is_valid("The Gateway  pool ceiling for codex is 32,", text)  # ws and case
    assert not lab.quote_is_valid("pool ceiling for codex is 64", text)  # altered
    assert not lab.quote_is_valid("short", text)
    assert not lab.quote_is_valid(None, text)
    assert lab.quote_is_valid("tiny memory", "tiny memory")  # whole short text allowed


def test_route_guard():
    lab.assert_local({"x-sk-rail": "local", "x-sk-provider": "local"})
    with pytest.raises(lab.RouteRefused):
        lab.assert_local({"x-sk-rail": "remote", "x-sk-provider": "local"})
    with pytest.raises(lab.RouteRefused):
        lab.assert_local({})


MEM = {
    "m1": (
        "Pool ceilings",
        "The gateway pool ceiling for codex is 32, not 4, on both gateways.",
        ["infra"],
    ),
    "m2": ("Fusion", "Reciprocal rank fusion weights are tuned per arm in the bench.", ["infra"]),
    "m3": ("Private", "Something about a medical diagnosis in the family.", ["infra"]),
    "m4": ("Tagged", "A perfectly boring note about nothing in particular here.", ["sacred"]),
}
LOCAL = {"x-sk-rail": "local", "x-sk-provider": "local", "x-sk-model-served": "m"}


class FakeGateway:
    """Reads the prompt it is sent and answers per scripted behaviour."""

    def __init__(self, a_quote=None, b_quote=None, headers=LOCAL, mode="agree"):
        self.sent = []
        self.headers, self.mode = headers, mode
        self.a_quote, self.b_quote = a_quote, b_quote

    def __call__(self, url, payload, timeout):
        text = payload["messages"][-1]["content"]
        self.sent.append(text)
        if "single word" in text:
            return 200, self.headers, json.dumps({"choices": [{"message": {"content": "ok"}}]})
        pool = {}
        for blk in text.split("[M")[1:]:
            idx, rest = blk.split("]", 1)
            pool[f"M{idx}"] = rest.strip().split("\n", 1)[1].split("\n\n")[0]
        h = next((k for k, v in pool.items() if "pool ceiling" in v), None)
        if "Grade EACH" in text:
            quote = self.b_quote or "pool ceiling for codex is 32"
            v = [{"m": k, "relevant": k == h and self.mode != "b_none", "quote": quote if k == h else ""}
                 for k in pool]  # fmt: skip
            out = {"verdicts": v}
        else:
            quote = self.a_quote or "pool ceiling for codex is 32"
            out = {"gold": [{"m": h, "quote": quote}] if h and self.mode != "a_none" else []}
        return (
            200,
            self.headers,
            json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}),
        )


def _arms():
    return {"a": lambda q: ["m1", "m2", "m3", "m4", "gone"], "b": lambda q: ["m2", "m1"]}


def _load(mid):
    return MEM.get(mid)


def _cand(q="codex pool ceiling"):
    return hv.Candidate(hv.qid_of(q), q, "claude_code", "abc", RECENT, 1)


def _run(tmp_path, gw, cands=None):
    return lab.label_run(
        cands or [_cand()],
        _arms(),
        _load,
        lab.Judge(transport=gw),
        tmp_path / "priv",
        interval_s=0,
    )


def test_pool_drops_sensitive_and_missing_and_hides_them_from_judge(tmp_path):
    gw = FakeGateway()
    meta = _run(tmp_path, gw)
    sent = "\n".join(gw.sent)
    assert "medical diagnosis" not in sent and "boring note" not in sent
    assert meta["pool_sensitive_dropped"] == 2
    assert meta["labelled"] == 1 and meta["agreement_rate"] == 1.0


def test_agreeing_valid_passes_label_and_files_are_private(tmp_path):
    _run(tmp_path, FakeGateway())
    d = tmp_path / "priv"
    rec = json.loads((d / "labelled.jsonl").read_text())
    assert rec["gold"] == ["m1"] and "pool ceiling" in rec["quotes"]["m1"]
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for f in d.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    meta = json.loads((d / "label-meta.json").read_text())
    assert "correlated" in meta["limits"] and "—" not in meta["limits"]


def test_fabricated_quote_is_rejected_then_unjudged(tmp_path):
    meta = _run(tmp_path, FakeGateway(a_quote="a sentence that is not in the memory at all"))
    assert meta["labelled"] == 0 and meta["unjudged"] == 1
    assert meta["rejected_quotes"] >= 1 and meta["unjudged_reasons"] == {"disagree": 1}


def test_disagreement_goes_to_unjudged(tmp_path):
    meta = _run(tmp_path, FakeGateway(mode="b_none"))
    assert meta["unjudged_reasons"] == {"disagree": 1} and meta["agreement_rate"] == 0.0


def test_both_empty_is_no_gold_not_labelled(tmp_path):
    gw = FakeGateway(mode="a_none")
    gw.b_quote = None

    class Both(FakeGateway):
        def __call__(self, url, payload, timeout):
            self.mode = (
                "b_none" if "Grade EACH" in payload["messages"][-1]["content"] else "a_none"
            )
            return super().__call__(url, payload, timeout)

    meta = _run(tmp_path, Both())
    assert meta["unjudged_reasons"] == {"no_gold": 1} and meta["agreement_rate"] == 1.0


def test_non_local_route_aborts_before_any_query(tmp_path):
    gw = FakeGateway(headers={"x-sk-rail": "remote", "x-sk-provider": "anthropic"})
    with pytest.raises(lab.RouteRefused):
        _run(tmp_path, gw)
    assert len(gw.sent) == 1 and "codex" not in gw.sent[0]  # only the canary left the box


def test_sensitive_candidate_never_sent(tmp_path):
    gw = FakeGateway()
    c = hv.Candidate("x", "liver transplant notes", "hermes", "abc", RECENT, 1)
    meta = _run(tmp_path, gw, [c])
    assert meta["excluded_sensitive"] == 1 and len(gw.sent) == 1  # canary only


def test_parse_error_unjudged(tmp_path):
    def bad(url, payload, timeout):
        body = "ok" if "single word" in payload["messages"][-1]["content"] else "no json here"
        return 200, LOCAL, json.dumps({"choices": [{"message": {"content": body}}]})

    meta = _run(tmp_path, bad)
    assert meta["unjudged_reasons"] == {"parse_error:a_nojson_None": 1}


# ---- live_bench --labelled ------------------------------------------------


def test_live_bench_labelled_multi_gold_and_evidence_class(tmp_path):
    d = tmp_path / "priv"
    hv.ensure_private_dir(d)
    (d / "labelled.jsonl").write_text(
        json.dumps(
            {
                "qid": "q1",
                "query": "secret query text",
                "source": "hermes",
                "gold": ["id1", "id2"],
                "quotes": {},
            }
        )
        + "\n"  # fmt: skip
    )
    (d / "label-meta.json").write_text(json.dumps(
        {"candidates": 10, "attempted": 5, "labelled": 1, "unjudged": 4,
         "unjudged_reasons": {"disagree": 4}, "agreement_rate": 0.2}))  # fmt: skip
    qs, meta = lb.load_labelled(d)
    assert qs[0].gold == ("id1", "id2") and qs[0].kind == "hermes"
    arms = {n: (lambda q: ["x", "id2"]) for n in lb.ARMS if n != "fallback_chain"}
    alive = {n: (True, "ok") for n in lb.ARMS}
    res = lb.run_live(arms, qs, alive=alive, embed_interval_s=0)
    a = res["arms"]["bm25_only"]["by_kind"]["all"]
    assert a["recall@5"] == 0.5 and a["mrr"] == 0.5
    res["evidence_class"] = lb.EVIDENCE_LABELLED
    res["missingness"] = lb.missingness_line(meta)
    res["judge_limits"] = lab.JUDGE_LIMITS
    table = lb.format_table(res)
    assert "live_labelled" in table and "unjudged 4" in table and "not accuracy" in table
    assert "SELF-RETRIEVAL." not in table.split("evidence_class")[1][:200]
    assert "secret query text" not in table and "secret query text" not in json.dumps(res)
    assert "—" not in table and "–" not in table


def test_self_retrieval_table_still_tagged():
    qs = lb.build_queries([("a", "alpha beta gamma", "body words here")])
    res = lb.run_live({n: (lambda q: ["a"]) for n in lb.ARMS}, qs,
                      alive={n: (True, "ok") for n in lb.ARMS}, embed_interval_s=0)  # fmt: skip
    res["evidence_class"] = lb.EVIDENCE_SELF
    assert "live_self_retrieval" in lb.format_table(res)


def test_labelled_dir_must_be_private(tmp_path):
    with pytest.raises(ValueError):
        lb.load_labelled(lab.Path(lab.__file__).resolve().parents[2] / "x")


def test_parse_json_repairs_missing_closers_only():
    assert lab.parse_json('{"gold":[{"m":"M1","quote":"abc"}')["gold"][0]["m"] == "M1"
    assert lab.parse_json('<think>x</think>{"gold":[]}') == {"gold": []}
    assert lab.parse_json("no braces") is None
    assert lab.parse_json('{"gold": [oops]}') is None


# ---- paraphrase (SKMEM-P1-04 stronger instrument) --------------------------

from skmemory.eval import paraphrase as para  # noqa: E402

PM = [
    (
        "p1",
        "Codex pool ceiling",
        "The gateway pool ceiling for codex is 32 on both gateways. zebra9-x",
    ),
    (
        "p2",
        "Fusion weights",
        "Reciprocal rank fusion weights are tuned per arm in the benchmark harness.",
    ),
]


def _df():
    return para.CorpusDF.from_texts([f"{t} {c}" for _, t, c in PM])


def test_overlap_rejects_identifier_stem_number_and_trigram():
    t, c = PM[0][1], PM[0][2]
    df = _df()
    assert para.overlap_reason("what is zebra9-x", t, c, df) == "number"  # shares the 9
    assert para.overlap_reason("what is abcd", t, "see ref ab-cd here", df) == "identifier"
    assert para.overlap_reason("what is ab-cd", t, "see ref abcd here", df) == "identifier"
    assert para.overlap_reason("how many concurrent codexes", t, c, df) == "rare_stem"  # plural
    assert para.overlap_reason("which gateways are busy", t, c, df) == "rare_stem"
    assert para.overlap_reason("how many agents run, 32 or more", t, c, df) == "number"
    assert para.overlap_reason("a limit of 7", t, "limit 7 here", df) == "number"
    assert para.overlap_reason("is the gateway pool ceiling ok", t, c, df) is not None
    assert para.overlap_reason("how many agents can run at once", t, c, df) is None


def test_rarity_comes_from_corpus_not_sample():
    t, c = "Pool", "The ceiling applies"
    common = para.CorpusDF.from_texts(["the ceiling alpha"] * 50 + ["the other beta"] * 50)
    assert not common.is_rare("ceil") and common.is_rare("zzz")
    assert para.overlap_reason("what is the limit", t, c, common) is None  # shares only "the"
    assert para.overlap_reason("what ceilings exist", t, c, common) is None  # common stem
    rare = para.CorpusDF.from_texts(["x y z"] * 100)
    assert para.overlap_reason("what ceilings exist", t, c, rare) == "rare_stem"


def test_corpus_df_cache_roundtrip(tmp_path):
    df = para.CorpusDF.from_texts(["the ceiling alpha"] * 50 + ["the other beta"] * 50)
    pdir = hv.ensure_private_dir(tmp_path / "pp")
    df.dump(pdir / "df.json")
    back = para.CorpusDF.load(pdir / "df.json")
    assert back.n == 100 and back.common == df.common


def test_question_type_and_cleaning():
    assert para.question_type("How many agents can run?") == "how"
    assert para.question_type("Does it scale?") == "yes_no"
    assert para.question_type("Tell me more") == "other"
    assert para.clean_question('<think>x</think>\n"How many agents run at once?"') == (
        "How many agents run at once?"
    )


def test_sensitive_widened_plurals_terms_tags_and_paths():
    for t in ["physicians advised", "anxiety", "diabetes", "death", "medications", "surgeries",
              "diagnoses", "transplants", "his late wife"]:  # fmt: skip
        assert hv.is_sensitive_text(t), t
    assert not hv.is_sensitive_text("stdout of the hive deliver job")
    assert hv.is_sensitive_memory("t", "boring text", ["Personal-History"])
    assert hv.is_sensitive_memory("t", "boring text", ["chef-medical-notes"])
    assert hv.is_sensitive_memory("t", "boring text", [], source_ref="notes/sacred/x.md")
    assert not hv.is_sensitive_memory("t", "boring text", ["infra"], source_ref="docs/gw.md")


def test_select_memories_skips_sensitive_and_short():
    recs = {
        "a": ("T", "x" * 100, []),
        "b": ("T", "medical diagnosis " + "y" * 100, []),
        "c": ("T", "short", []),
        "d": ("T", "z" * 100, ["sacred"]),
    }
    out, sk = para.select_memories(list(recs), recs.get, 10, 1)
    assert [m[0] for m in out] == ["a"] and sk == {"sensitive": 2, "too_short": 1}


class QGateway:
    """First answer overlaps (when ``leak_first``), the retry is clean."""

    def __init__(self, leak_first=True, always_leak=False, headers=LOCAL):
        self.n, self.leak_first, self.always_leak, self.headers = (
            0,
            leak_first,
            always_leak,
            headers,
        )
        self.sent = []

    def __call__(self, url, payload, timeout):
        text = payload["messages"][-1]["content"]
        self.sent.append(text)
        if "single word" in text:
            out = "ok"
        else:
            retry = "Rephrase completely" in text
            leak = self.always_leak or (self.leak_first and not retry)
            out = "What is the zebra9-x thing?" if leak else "How many helpers can run together?"
        return 200, self.headers, json.dumps({"choices": [{"message": {"content": out}}]})


def test_generate_retry_then_accept_and_private_files(tmp_path):
    gw = QGateway()
    meta = para.generate(
        PM[:1], lab.Judge(transport=gw), tmp_path / "pp", df=_df(), interval_s=0,
        near=lambda mid: ["n1", mid],
    )  # fmt: skip
    assert meta["requested"] == 1 and meta["accepted"] == 1 and meta["retried"] == 1
    rec = json.loads((tmp_path / "pp" / "paraphrase.jsonl").read_text())
    assert rec["gold"] == ["p1"] and rec["qtype"] == "how"
    assert rec["near"] == ["n1"]  # near-duplicates are separate from gold
    assert meta["with_near_duplicates"] == 1
    assert stat.S_IMODE(os.stat(tmp_path / "pp").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "pp" / "paraphrase.jsonl").st_mode) == 0o600


def test_generate_rejected_stays_missing(tmp_path):
    meta = para.generate(
        PM[:1], lab.Judge(transport=QGateway(always_leak=True)), tmp_path / "pp", df=_df(),
        interval_s=0,
    )  # fmt: skip
    assert meta["accepted"] == 0 and meta["rejected_overlap"] == 1
    assert (tmp_path / "pp" / "paraphrase.jsonl").read_text() == ""


def test_generate_non_local_route_sends_only_canary(tmp_path):
    gw = QGateway(headers={"x-sk-rail": "remote", "x-sk-provider": "x"})
    with pytest.raises(lab.RouteRefused):
        para.generate(PM, lab.Judge(transport=gw), tmp_path / "pp", df=_df(), interval_s=0)
    assert len(gw.sent) == 1


def test_live_bench_paraphrase_header_and_breakdown(tmp_path):
    d = tmp_path / "pp"
    para.generate(PM[:1], lab.Judge(transport=QGateway()), d, df=_df(), interval_s=0,
                  near=lambda m: ["n1"])  # fmt: skip
    qs, meta = lb.load_paraphrase(d)
    assert qs[0].kind == "how" and qs[0].gold == ("p1",)
    arms = {n: (lambda q: ["n1"]) for n in lb.ARMS if n != "fallback_chain"}  # near-dup only
    res = lb.run_live(arms, qs, alive={n: (True, "ok") for n in lb.ARMS}, embed_interval_s=0)
    a = res["arms"]["bm25_only"]["by_kind"]["all"]
    assert a["recall@5"] == 0.0 and a["recall@5_lenient"] == 1.0  # lenient never replaces strict
    res.update(evidence_class=lb.EVIDENCE_SELF, query_source="paraphrase",
               missingness=lb.paraphrase_line(meta), judge_limits=para.LIMITS)  # fmt: skip
    table = lb.format_table(res)
    assert "query_source: paraphrase" in table and "live_self_retrieval" in table
    assert "not real user queries" in table and "QUERY-TYPE" in table
    assert "how " in table and "How many helpers" not in table
    assert "lenient r@5" in table and "never replaces strict" in table
    assert "—" not in table and "–" not in table


def test_yes_no_capped_and_stratified_prompt(tmp_path):
    class YN(QGateway):
        def __call__(self, url, payload, timeout):
            text = payload["messages"][-1]["content"]
            self.sent.append(text)
            out = "ok" if "single word" in text else "Does a helper pool exist for agents?"
            return 200, self.headers, json.dumps({"choices": [{"message": {"content": out}}]})

    mems = [
        (f"p{i}", "T", "unrelated filler words about nothing " * 3, "long-term") for i in range(5)
    ]
    gw = YN()
    meta = para.generate(mems, lab.Judge(transport=gw), tmp_path / "pp", df=_df(), interval_s=0)
    assert meta["query_types"] == {"yes_no": 1}  # cap = ceil(0.2 * 5) = 1
    assert meta["rejected_yes_no_cap"] == 4 and meta["accepted"] == 1
    assert "Start with What, Why or How" in gw.sent[1]
    assert "MUST begin with What, Why or How" in "\n".join(gw.sent)
    assert meta["missing_profile"]["rejected"]["layer"] == {"long-term": 4}
    assert meta["missing_profile"]["rejected"]["length"]


def test_pg_near_sql_runs_on_throwaway_db():
    dsn = os.environ.get("SKMEMORY_PG_DSN")
    if not dsn:
        pytest.skip("needs the throwaway skmem-pg")
    from skmemory.backends.pgvector_backend import PGVectorBackend

    pg = PGVectorBackend(dsn=dsn, agent="lumina")
    with pg._connection().cursor() as cur:  # read-only SQL: must parse and execute
        cur.execute(para.NEAR_SQL, ("none", "lumina", "lumina", "none", 0.05))
        assert cur.fetchall() == []


@pytest.mark.parametrize(
    "a,b",
    [
        ("certificate", "certificates"),
        ("service", "services"),
        ("cache", "caches"),
        ("database", "databases"),
        ("update", "updated"),
        ("update", "updating"),
        ("updated", "updating"),
        ("deploy", "deployed"),
        ("deploy", "deploying"),
    ],
)
def test_stem_pairs_meet(a, b):
    assert para.stem(a) == para.stem(b)


def test_e_word_no_longer_leaks_past_overlap_check():
    df = para.CorpusDF.from_texts(["x y z"] * 100)
    assert (
        para.overlap_reason("what certificate expired", "Renewals", "certificates renewed", df)
        == "rare_stem"
    )
