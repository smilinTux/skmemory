"""Sacred recall bench (SKMEM-P1-15): surfaces when asked, never when not.

Hermetic: invented fixture, throwaway store, no network, no Postgres. The tests under
"paths that used to leak" were written failing (strict xfail) and now pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from skmemory.eval import regression_gate as gate
from skmemory.eval import sacred_bench as sb
from skmemory.eval.fixtures import FIXTURE_DIR


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return sb.run(workdir=tmp_path_factory.mktemp("sacred"))


def _cell(result, path, reader):
    return result["must_not_surface"][path][reader]


def test_fixture_regenerates_byte_identical(tmp_path):
    gen = Path(FIXTURE_DIR) / "gen_sacred_fixture.py"
    subprocess.run([sys.executable, "-I", str(gen), str(tmp_path)], check=True)
    assert (tmp_path / sb.FIXTURE_FILE).read_bytes() == (
        Path(FIXTURE_DIR) / sb.FIXTURE_FILE
    ).read_bytes()


def test_fixture_is_sacred_by_tag_and_owner_only_except_misfiled():
    fx = sb.load_fixture()
    by_id = {m["id"]: m for m in fx["memories"]}
    from skmemory.hooks.sensitivity import is_sensitive

    for sid in fx["sacred_ids"]:
        m = by_id[sid]
        assert is_sensitive(m["tags"]), m["title"]
        if sid not in fx["misfiled_ids"]:
            assert m["context_tag"] == "@chef-only"
    assert set(fx["misfiled_ids"]) <= set(fx["sacred_ids"])


def test_must_surface_owner_asks_get_sacred_top3_above_casual(result):
    ms = result["must_surface"]
    assert ms["queries"] >= 6
    failing = [r for r in ms["rows"] if not (r["top3"] and r["above_casual"])]
    assert not failing, failing


def test_bench_is_not_vacuous(result):
    assert result["vacuous"] == []
    c = result["controls"]
    assert c["auto_matchable"] == c["auto_prompts"] > 0  # sacred rows were reachable, unfiltered


def test_query_slice_never_returns_sacred_even_when_asked_about_the_person(result):
    cell = _cell(result, "query_slice", sb.OWNER)
    assert cell["probes"] >= 12
    assert cell["leaks"] == cell["misfiled_leaks"] == 0, cell["detail"]


def test_owner_memory_search_never_ranks_sacred_ahead_of_the_on_topic_memory(result):
    cell = _cell(result, "memory_search", sb.OWNER)
    assert cell["leaks"] == 0, cell["detail"]


@pytest.mark.parametrize("reader", sb.load_fixture()["non_owner_readers"])
def test_non_owner_reader_sees_no_properly_filed_sacred_memory(result, reader):
    for path in ("memory_search", "memory_context"):
        cell = _cell(result, path, reader)
        assert cell["leaks"] == 0, (path, reader, cell["detail"])


def test_ritual_channel_audience_sees_no_sacred_memory(result):
    cell = _cell(result, "ritual", "channel:unknown-room")
    assert cell["leaks"] == cell["misfiled_leaks"] == 0


# ---- paths that used to leak (SKMEM-P1-15 fixes) ---------------------------------------


def test_session_context_excludes_every_sensitive_stem(result):
    assert _cell(result, "session_context", sb.OWNER)["leaks"] == 0


def test_memory_context_excludes_sensitive_rows_for_the_owner(result):
    assert _cell(result, "memory_context", sb.OWNER)["leaks"] == 0


def test_ritual_excludes_sensitive_rows_for_the_owner(result):
    cell = _cell(result, "ritual", sb.OWNER)
    assert cell["leaks"] + cell["misfiled_leaks"] == 0


@pytest.mark.parametrize("reader", sb.load_fixture()["non_owner_readers"])
def test_misfiled_sacred_row_never_reaches_a_non_owner(result, reader):
    assert _cell(result, "memory_search", reader)["misfiled_leaks"] == 0


def _store(tmp_path):
    from datetime import datetime, timezone

    base = tmp_path / "agents" / sb.AGENT / "memory"
    base.mkdir(parents=True)
    return sb.build_store(base, sb.load_fixture(), datetime.now(timezone.utc)), base


def test_ritual_takes_the_next_strongest_non_sensitive_memory(tmp_path):
    """Sacred rows are the strongest; the ritual skips them and still shows the rest."""
    with sb._isolated_env(tmp_path):
        store, _ = _store(tmp_path)
        text = sb._ritual_text(store, tmp_path, None)
    assert "STRONGEST MEMORIES" in text
    assert "Rotate the Marrowgate ingress certificate" in text  # strongest ordinary row


def test_non_owner_reader_cannot_recall_a_misfiled_sacred_id(tmp_path):
    fx = sb.load_fixture()
    with sb._isolated_env(tmp_path):
        store, _ = _store(tmp_path)
        mid = fx["misfiled_ids"][0]
        assert store.recall(mid) is not None  # the owner still can
        assert store.recall(mid, reader="@public") is None  # hidden reads as missing
        assert store.reader_can_see(mid, "@public") is False


def test_medical_row_keeps_context_tag_semantics_for_its_client(tmp_path):
    """Only OWNER-ONLY stems are a hard ceiling: a @work:acme medical row stays visible to acme."""
    from skmemory.models import Memory, MemoryLayer

    with sb._isolated_env(tmp_path):
        store, _ = _store(tmp_path)
        m = Memory(
            title="Acme site first-aid kit audit",
            content="Audit of the first-aid kit at the Acme site, medical supplies restocked.",
            layer=MemoryLayer.MID,
            tags=["medical", "acme"],
            context_tag="@work:acme",
        )
        store.primary.save(m)
        assert store.reader_can_see(m.id, "@work:acme") is True
        assert store.reader_can_see(m.id, "@public") is False
        assert store.recall(m.id, reader="@work:acme") is not None


def test_owner_only_stems_hide_regardless_of_context_tag(tmp_path):
    from skmemory.models import Memory, MemoryLayer

    with sb._isolated_env(tmp_path):
        store, _ = _store(tmp_path)
        for tag in ("sacred", "Grief", "family-private", "personal-history"):
            m = Memory(
                title=f"x {tag}",
                content="invented",
                layer=MemoryLayer.MID,
                tags=[tag],
                context_tag="@work:acme",
            )
            store.primary.save(m)
            assert store.reader_can_see(m.id, "@work:acme") is False, tag
            assert store.reader_can_see(m.id, None) is True, tag


def test_sensitivity_helper_is_the_single_source():
    from skmemory.hooks import query_slice, sensitivity

    assert query_slice.SENSITIVE_STEMS is sensitivity.SENSITIVE_STEMS
    assert sensitivity.is_sensitive(["Medical"]) and sensitivity.is_sensitive(["legal-docs"])
    assert not sensitivity.is_sensitive(["ops", "pr"])
    assert sensitivity.is_owner_only(["x", "Sacred"]) and not sensitivity.is_owner_only(
        ["medical"]
    )
    where, params = sensitivity.sql_not_sensitive("tags")
    assert where == ["sk_sensitive(tags) = 0"] and params == []


def test_query_slice_loads_by_path_without_the_package():
    """user_prompt_fast.py loads query_slice by path under python -S."""
    hooks = Path(sb.__file__).resolve().parent.parent / "hooks"
    code = (
        f"import sys; sys.path.insert(0, {str(hooks)!r}); import query_slice; "
        "print('sacred' in query_slice.SENSITIVE_STEMS)"
    )
    out = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "True", out.stderr


# ---- variants, whisper, auto_context -------------------------------------------------------

VARIANT_TAGS = [
    "family_private",
    "Family Private",
    "familyprivate",
    "personal_history",
    "PersonalHistory",
    "s\u0430cred",  # Cyrillic a
    "\uff53\uff41\uff43\uff52\uff45\uff44",  # fullwidth
    "gri\u03b5f",  # Greek epsilon
    "SACRED",
    "Grief",
]


@pytest.mark.parametrize("tag", VARIANT_TAGS)
def test_tag_variants_are_owner_only_and_sensitive(tag):
    from skmemory.hooks import sensitivity as sv

    assert sv.is_sensitive([tag]) and sv.is_owner_only([tag]), repr(tag)


@pytest.mark.parametrize("tag", VARIANT_TAGS)
def test_tag_variants_are_hidden_from_a_public_reader(tmp_path, tag):
    from skmemory.models import Memory, MemoryLayer

    with sb._isolated_env(tmp_path):
        store, _ = _store(tmp_path)
        m = Memory(
            title=f"v {tag}",
            content="invented",
            layer=MemoryLayer.MID,
            tags=[tag],
            context_tag="@public",
        )
        store.primary.save(m)
        assert store.reader_can_see(m.id, "@public") is False, repr(tag)
        assert store.reader_can_see(m.id, None) is True


@pytest.mark.parametrize("tag", VARIANT_TAGS)
def test_tag_variants_are_dropped_from_owner_auto_inject(tmp_path, tag):
    """session_context strongest, load_context, the prompt hook, ritual."""
    from datetime import datetime, timezone

    from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer

    title = "Zanthe Pellory lantern ledger"
    with sb._isolated_env(tmp_path):
        base = tmp_path / "agents" / sb.AGENT / "memory"
        base.mkdir(parents=True)
        fx = {"memories": [], "sacred_ids": []}
        store = sb.build_store(base, fx, datetime.now(timezone.utc))
        store.primary.save(
            Memory(
                title=title,
                content="Zanthe Pellory lantern ledger invented entry",
                layer=MemoryLayer.LONG,
                tags=[tag],
                context_tag="@chef-only",
                emotional=EmotionalSnapshot(intensity=9.5),
            )
        )
        needles = {"x": [title.casefold()]}
        assert sb.leaked(sb._session_text(tmp_path), needles) == []
        assert sb.leaked(sb._context_text(store, sb.OWNER), needles) == []
        assert sb.leaked(sb._ritual_text(store, tmp_path, None), needles) == []
        assert (
            sb.leaked(sb._slice_text("Zanthe Pellory lantern ledger entry", base), needles) == []
        )
        assert sb.leaked(sb._auto_context_text(store, "Zanthe", sb.OWNER), needles) == []


def test_variant_fixture_rows_exist_and_are_counted():
    fx = sb.load_fixture()
    assert len(fx["variant_ids"]) >= 8 and set(fx["variant_ids"]) <= set(fx["misfiled_ids"])


def test_bench_catches_a_whisper_title_leak(monkeypatch, tmp_path):
    """Negative control: without the collect_open title check the bench goes red."""
    from skmemory import session_context

    monkeypatch.setattr(session_context, "_title_is_sensitive", lambda *_a: False)
    res = sb.run(workdir=tmp_path)
    assert _cell(res, "session_context", sb.OWNER)["leaks"] > 0


def test_bench_catches_a_normalization_regression(monkeypatch, tmp_path):
    """Negative control: plain casefold matching lets the variant spellings through."""
    from skmemory.hooks import sensitivity as sv

    monkeypatch.setattr(sv, "normalize", lambda t: str(t).casefold())
    monkeypatch.setattr(sv, "_NORM_SENSITIVE", tuple(x.casefold() for x in sv.SENSITIVE_STEMS))
    monkeypatch.setattr(sv, "_NORM_OWNER_ONLY", tuple(x.casefold() for x in sv.OWNER_ONLY_STEMS))
    res = sb.run(workdir=tmp_path)
    assert res["leak_count"] > 0


def test_auto_context_drops_sensitive_rows_for_the_owner(result):
    assert _cell(result, "memory_auto_context", sb.OWNER)["leaks"] == 0
    assert _cell(result, "memory_auto_context", "@public")["leaks"] == 0


# ---- the gate -------------------------------------------------------------------------


def test_gate_counts_sacred_leaks_and_floors_must_surface():
    cur = gate.collect_metrics()
    assert "sacred_bench.must_surface.top3" in cur["metrics"]
    assert "sacred_bench.must_surface.above_casual" in cur["metrics"]
    assert cur["leak_count"] >= sum(cur["sacred_leaks_by_path"].values())


def test_gate_fails_on_a_sacred_leak_and_names_the_path():
    cur = {"metrics": {}, "leak_count": 1, "sacred_leaks_by_path": {"session_context": 1}}
    regs = gate.compare(cur, {"metrics": {}})
    assert any("session_context" in r for r in regs)


def test_gate_fails_on_a_vacuous_sacred_bench():
    cur = {"metrics": {}, "leak_count": 0, "sacred_vacuous": ["query_slice returned nothing"]}
    assert any("vacuous" in r for r in gate.compare(cur, {"metrics": {}}))


def test_gate_fails_when_must_surface_drops():
    base = {"metrics": {"sacred_bench.must_surface.top3": 1.0}}
    cur = {"metrics": {"sacred_bench.must_surface.top3": 0.5}, "leak_count": 0}
    assert gate.compare(cur, base)


def test_bench_does_not_touch_the_live_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SKAGENT", "someone-else")
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    sb.run(workdir=tmp_path)
    import os

    assert os.environ["SKAGENT"] == "someone-else"
    assert os.environ["SKMEMORY_READER"] == "@public"
