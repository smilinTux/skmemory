"""SKMEM-P1-03: live-corpus runner. Offline: fake arms, tmp homes, no network."""

import json
import os
import stat
from collections import Counter

import pytest

from skmemory.eval import live_bench as lb

MEMS = [
    (f"id{i:03d}", f"Decision about gateway pool {i}", f"body text zebra{i}-x9 shared words here")
    for i in range(30)
]


def test_sampler_deterministic_and_order_independent():
    ids = [m[0] for m in MEMS]
    a = lb.sample_ids(ids, 10, seed=7)
    assert a == lb.sample_ids(list(reversed(ids)), 10, seed=7)
    assert a != lb.sample_ids(ids, 10, seed=8)
    assert len(set(a)) == 10 and set(a) <= set(ids)
    assert lb.sample_ids(ids, 999) == sorted(ids)


def test_rare_tokens_prefer_identifiers_and_low_df():
    df = lb.document_frequency(["common words common words", "common words zebra9-x"])
    got = lb.rare_tokens("common words zebra9-x", df)
    assert got[0] == "zebra9-x"
    assert got.index("zebra9-x") < got.index("common")
    assert lb.rare_tokens("common words zebra9-x", df) == got  # deterministic


def test_build_queries_title_and_rare():
    qs = lb.build_queries(MEMS)
    kinds = Counter(q.kind for q in qs)
    assert kinds["title"] == 30 and kinds["rare_token"] == 30
    rare = next(q for q in qs if q.id == "id004" and q.kind == "rare_token")
    assert "zebra4-x9" in rare.text
    assert lb.build_queries(MEMS) == qs


def _arms(vec_empty=False):
    def hit(q):  # the gold id is the number embedded in the query text
        digits = "".join(c for c in q.split("zebra")[-1].split("-")[0] if c.isdigit())
        if not digits:
            digits = q.rsplit(" ", 1)[-1]
        return [f"id{int(digits):03d}"]

    return {
        "bm25_only": hit,
        "vector_only": (lambda q: []) if vec_empty else hit,
        "sqlite_like": hit,
        "graph_only": lambda q: [],
        "candidate": hit,
    }


ALIVE = {n: (True, "ok") for n in lb.ARMS}


def test_run_live_scores_and_latency():
    qs = lb.build_queries(MEMS)
    res = lb.run_live(_arms(), qs, alive=ALIVE, embed_interval_s=0)
    a = res["arms"]
    assert a["bm25_only"]["by_kind"]["all"]["recall@5"] == 1.0
    assert a["bm25_only"]["p95_ms"] is not None
    assert a["fallback_chain"]["status"] == "alive"


def test_dead_arm_is_dead_not_zero():
    qs = lb.build_queries(MEMS)
    alive = dict(ALIVE, vector_only=(False, "embed_ok=False"))
    res = lb.run_live(_arms(), qs, alive=alive, embed_interval_s=0)
    v = res["arms"]["vector_only"]
    assert v["status"] == "dead" and v["calls"] == 0
    assert "dead" in lb.format_table(res)
    assert res["arms"]["fallback_chain"]["status"] == "alive"  # lexical still up


def test_silent_and_raising_arms_marked_dead():
    qs = lb.build_queries(MEMS)
    arms = _arms(vec_empty=True)

    def boom(q):
        raise RuntimeError("secret detail")

    arms["sqlite_like"] = boom
    res = lb.run_live(arms, qs, alive=ALIVE, embed_interval_s=0)
    assert res["arms"]["vector_only"]["status"] == "dead"
    assert "nothing" in res["arms"]["vector_only"]["reason"]
    assert res["arms"]["sqlite_like"]["status"] == "dead"
    assert "RuntimeError" in json.dumps(res["rows"])
    assert "secret detail" not in json.dumps(res)
    # fallback chain falls through to lexical when vector is empty
    assert res["arms"]["fallback_chain"]["by_kind"]["all"]["recall@5"] == 1.0


def test_embed_rate_limit_applies_only_to_embedding_arms():
    qs = lb.build_queries(MEMS)[:4]
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lb.run_live(_arms(), qs, alive=ALIVE, embed_interval_s=0.5, sleep=sleep, clock=lambda: now[0])
    # two embedding arms per query, so 2*4 calls, each at least 0.5s after the last
    assert len(slept) == 7 and all(s > 0 for s in slept)


def test_table_has_bias_label_and_no_memory_text():
    qs = lb.build_queries(MEMS)
    table = lb.format_table(lb.run_live(_arms(), qs, alive=ALIVE, embed_interval_s=0))
    assert "SELF-RETRIEVAL" in table and "p95" in table
    assert "Decision about gateway" not in table and "zebra" not in table
    assert "\u2014" not in table and "\u2013" not in table


def test_outputs_mode_600_and_no_text(tmp_path):
    qs = lb.build_queries(MEMS)
    res = lb.run_live(_arms(), qs, alive=ALIVE, embed_interval_s=0)
    paths = lb.write_outputs(tmp_path / "out", res, lb.format_table(res), "t")
    for p in paths:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "out").st_mode) == 0o700
    assert "Decision about gateway" not in paths[1].read_text()


def test_out_dir_refuses_agent_home_and_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(lb.Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(lb.ReadOnlyViolation):
        lb.check_out_dir(tmp_path / ".skcapstone" / "agents" / "x")
    with pytest.raises(lb.ReadOnlyViolation):
        lb.check_out_dir(lb.Path(lb.__file__).resolve().parents[2] / "out")


def test_requires_opt_in():
    with pytest.raises(lb.LiveNotEnabled):
        lb.require_live({})
    lb.require_live({lb.LIVE_ENV: "1"})


def test_readonly_sqlite_reads_and_refuses_writes(tmp_path):
    from skmemory.backends.sqlite_backend import SQLiteBackend
    from skmemory.models import Memory, MemoryLayer

    src = SQLiteBackend(str(tmp_path / "mem"))
    src.save(Memory(id="m1", title="alpha beta", content="gamma delta", layer=MemoryLayer.SHORT))
    src._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = sorted(p.name for p in (tmp_path / "mem").iterdir())
    pg, age, ro = lb.make_backends(None, "lumina", str(tmp_path / "mem"))
    assert [m.id for m in ro.search_text("alpha")] == ["m1"]
    assert ro.load("m1").id == "m1"
    with pytest.raises(Exception, match="readonly|read-only|ReadOnly"):
        ro._get_conn().execute("DELETE FROM memories")
    with pytest.raises(lb.ReadOnlyViolation):
        ro.save(Memory(id="m2", title="t", content="c", layer=MemoryLayer.SHORT))
    with pytest.raises(lb.ReadOnlyViolation):
        pg.save(None)
    with pytest.raises(lb.ReadOnlyViolation):
        age.save(None)
    assert sorted(p.name for p in (tmp_path / "mem").iterdir()) == before


def test_build_store_creates_no_wal_dir(tmp_path):
    from skmemory.backends.sqlite_backend import SQLiteBackend

    SQLiteBackend(str(tmp_path / "mem"))
    pg, age, ro = lb.make_backends(None, "lumina", str(tmp_path / "mem"))
    assert not (tmp_path / "mem" / "wal").exists()
    lb.build_store(pg, age, ro)
    assert not (tmp_path / "mem" / "wal").exists()


@pytest.mark.skipif(not os.environ.get("SKMEMORY_PG_DSN"), reason="needs the throwaway skmem-pg")
def test_pg_session_is_read_only_against_throwaway_db(tmp_path):
    from skmemory.backends.sqlite_backend import SQLiteBackend

    SQLiteBackend(str(tmp_path / "mem"))
    pg, age, ro = lb.make_backends(os.environ["SKMEMORY_PG_DSN"], "lumina", str(tmp_path / "mem"))
    report = lb.verify_read_only(pg, age, ro)
    assert report["sqlite"] == "write refused" and report["pg"] == "write refused"
    assert report["age"] in ("write refused", "no connection")
