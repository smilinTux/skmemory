"""Write-time conflict verdict on snapshot (card SKMEM-P3-02). No network, no live state."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from skmemory.backends.file_backend import FileBackend
from skmemory.models import PARENT_RELATION_KEY, MemoryLayer
from skmemory.store import MemoryStore
from skmemory.triage import judge_relation

OLD = "The alias on skap01 points skwk12 at port 2222."
NEW = "skwk12 listens on port 22"
HOST_OLD = "skap08 runs the auto-rollout timer every two minutes and is the Syncthing authority."


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.delenv("SKMEMORY_TRIAGE_USE_LLM", raising=False)
    monkeypatch.delenv("SKMEMORY_WRITE_TRIAGE", raising=False)
    monkeypatch.delenv("SKMEMORY_READER", raising=False)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(primary=FileBackend(base_path=str(tmp_path / "m")))


def _llm(*words):
    it = iter(words)
    return lambda prompt: next(it)


def _patch_llm(monkeypatch, call):
    from skmemory import write_triage

    real = write_triage.judge_relation
    monkeypatch.setattr(
        write_triage,
        "judge_relation",
        lambda new, old, **kw: real(new, old, use_llm=True, llm_call=call),
    )


def test_changes_closes_old_and_links(store, monkeypatch):
    old = store.snapshot("port alias", OLD, kind="fact", layer=MemoryLayer.MID)
    _patch_llm(monkeypatch, _llm("CHANGES", "CHANGES"))
    new = store.snapshot("skwk12 port", NEW, kind="fact", layer=MemoryLayer.MID)
    assert new.supersedes_id == old.id and new.parent_id == old.id
    assert new.metadata[PARENT_RELATION_KEY] == "supersedes"
    closed = store.primary.load(old.id)
    assert closed.valid_to == new.valid_from
    assert closed.metadata["superseded_by"]["id"] == new.id
    # the edit does not break the hash, and the stored bytes round-trip
    assert closed.verify_integrity()
    raw = json.loads((Path(store.primary.base_path) / "mid-term" / f"{old.id}.json").read_text())
    assert raw["valid_to"] == new.valid_from


def test_seal_sidecar_is_resigned_consistently(tmp_path):
    from skmemory import sealing

    s = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "m")))
    old = s.snapshot("a", OLD, kind="fact", layer=MemoryLayer.MID)
    from skmemory.write_triage import close_superseded

    new = s.snapshot("b", "unrelated zebra", kind="episode")
    assert close_superseded(s, old.id, new)
    path = Path(s.primary.base_path) / "mid-term" / f"{old.id}.json"
    verdict = sealing.verify_seal(path.read_bytes(), path, config=None)
    assert verdict is None or verdict.ok
    assert not close_superseded(s, old.id, new)  # idempotent: already closed


def test_llm_disagreement_extends_never_closes(store, monkeypatch):
    old = store.snapshot("port alias", OLD, kind="fact", layer=MemoryLayer.MID)
    _patch_llm(monkeypatch, _llm("CHANGES", "EXTENDS"))
    new = store.snapshot("skwk12 port", NEW, kind="fact", layer=MemoryLayer.MID)
    assert new.supersedes_id is None
    assert new.metadata[PARENT_RELATION_KEY] == "extends"
    assert store.primary.load(old.id).valid_to is None


@pytest.mark.parametrize("failure", ["raise", "garbage"])
def test_llm_failure_never_loses_the_write(store, monkeypatch, failure):
    old = store.snapshot("port alias", OLD, kind="fact", layer=MemoryLayer.MID)

    def call(prompt):
        if failure == "raise":
            raise OSError("gateway down")
        return "banana"

    _patch_llm(monkeypatch, call)
    new = store.snapshot("skwk12 port", NEW, kind="fact", layer=MemoryLayer.MID)
    assert store.primary.load(new.id) is not None
    assert new.supersedes_id is None
    assert store.primary.load(old.id).valid_to is None


def test_triage_crash_never_loses_the_write(store, monkeypatch):
    from skmemory import write_triage

    store.snapshot("port alias", OLD, kind="fact")
    monkeypatch.setattr(store, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    new = store.snapshot("skwk12 port", NEW, kind="fact")
    assert store.primary.load(new.id) is not None
    assert write_triage.decide(store, new) is None


def test_unchanged_dedupes_only_when_no_new_word(store, monkeypatch):
    old = store.snapshot("rollout", HOST_OLD, kind="fact", layer=MemoryLayer.MID)
    _patch_llm(monkeypatch, _llm("UNCHANGED", "UNCHANGED"))
    got = store.snapshot(
        "rollout again", "skap08 runs the rollout timer every two minutes", kind="fact"
    )
    assert got.id == old.id
    # a negation adds a word the old fact lacks: never dropped
    _patch_llm(monkeypatch, _llm("UNCHANGED", "UNCHANGED"))
    neg = store.snapshot(
        "rollout neg", "skap08 stopped the rollout timer every two minutes", kind="fact"
    )
    assert neg.id != old.id and neg.supersedes_id is None


def test_extends_edge_fixture(store, monkeypatch):
    old = store.snapshot("rollout", HOST_OLD, kind="fact")
    new = store.snapshot("panes", "skap08 also hosts the review panes", kind="fact")
    assert new.parent_id == old.id and new.metadata[PARENT_RELATION_KEY] == "extends"
    assert store.primary.load(old.id).valid_to is None


def test_rules_changes_needs_agreed_cue():
    r = judge_relation(
        "skap08 runs the auto-rollout timer, correction: every five minutes",
        HOST_OLD,
        use_llm=False,
    )
    assert r.verdict in ("changes", "extends")
    # no cue, no LLM: an ambiguous pair must not become changes
    assert judge_relation(NEW, OLD, use_llm=False).verdict == "extends"


def test_episode_and_unkinded_never_triaged(store, monkeypatch):
    from skmemory import write_triage

    calls = []
    monkeypatch.setattr(write_triage, "judge_relation", lambda *a, **k: calls.append(1))
    store.snapshot("a", OLD, kind="fact")
    store.snapshot("b", NEW, kind="episode")
    store.snapshot("c", NEW + " again")
    store.snapshot("d", NEW + " proc", kind="procedure")
    assert calls == []


def test_reader_cannot_compare_or_close_hidden(store, monkeypatch):
    hidden = store.snapshot("secret port", OLD, kind="fact", layer=MemoryLayer.MID)
    h = store.primary.load(hidden.id)
    h.context_tag = "@chef-only"
    store.primary.save(h)
    seen = []
    _patch_llm(monkeypatch, lambda p: seen.append(p) or "CHANGES")
    new = store.snapshot("skwk12 port", NEW, kind="fact", reader="@public")
    assert seen == []  # the hidden memory was never shown to anything
    assert new.parent_id is None and new.supersedes_id is None
    assert store.primary.load(hidden.id).valid_to is None
    # and a dedupe can never hand back a hidden memory
    again = store.snapshot("secret again", OLD, kind="fact", reader="@public")
    assert again.id != hidden.id


def test_kill_switch(store, monkeypatch):
    store.snapshot("port alias", OLD, kind="fact")
    monkeypatch.setenv("SKMEMORY_WRITE_TRIAGE", "off")
    new = store.snapshot("skwk12 port", NEW, kind="fact")
    assert new.parent_id is None


def test_no_kind_callers_unaffected_and_latency(tmp_path, monkeypatch, capsys):
    from skmemory import write_triage

    monkeypatch.setattr(write_triage, "decide", lambda *a, **k: pytest.fail("triage ran"))
    s = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "m")))
    m = s.snapshot("t", "plain content", tags=["x"])
    assert m.kind == "episode" and m.parent_id is None and m.supersedes_id is None
    assert "write_triage" not in m.metadata
    monkeypatch.undo()
    # latency: no kind vs fact with 5 same-kind neighbours (rules only)
    s2 = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "m2")))
    for i in range(5):
        s2.snapshot(f"n{i}", f"skap0{i} runs the timer service {i}", kind="fact")

    def timed(**kw):
        t = time.perf_counter()
        for i in range(10):
            s2.snapshot(f"x{kw}{i}", f"skap01 fact number {i} {kw}", **kw)
        return (time.perf_counter() - t) / 10 * 1000

    base, fact = timed(), timed(kind="fact")
    print(f"snapshot latency ms: no-kind={base:.1f} fact={fact:.1f}")
    assert fact < base + 500
