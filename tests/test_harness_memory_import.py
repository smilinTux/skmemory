"""Hermetic tests for ``skmemory import harness-memory`` (card SKMEM-W-03)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.cli import cli
from skmemory.importers.harness_memory import build_plan, import_harness_memory
from skmemory.store import MemoryStore


def _fact(
    d: Path, slug: str, typ: str, body: str, *, name: str | None = None, mtime=None, nested=True
):
    d.mkdir(parents=True, exist_ok=True)
    kv = f"type: {typ}"
    meta = f"metadata:\n  {kv}" if nested else kv
    (d / f"{slug}.md").write_text(
        f"---\nname: {name or slug}\ndescription: about {slug}\n{meta}\noriginSessionId: abc\n---\n{body}\n"
    )
    if mtime:
        os.utime(d / f"{slug}.md", (mtime, mtime))
    return d / f"{slug}.md"


@pytest.fixture
def dirs(tmp_path):
    a, b = tmp_path / "a" / "memory", tmp_path / "b" / "memory"
    _fact(
        a,
        "fb-rule",
        "feedback",
        "Never do X. See [[proj-one]] and [[ref-two|alias]].",
        nested=False,
    )
    _fact(a, "proj-one", "project", "Project body dated nothing.", mtime=1_700_000_000)
    _fact(a, "ref-two", "reference", "Reference body 2026-11-06 is the date.")
    _fact(a, "user-pref", "user", "Likes tea.")
    _fact(a, "odd", "infrastructure", "Odd type body.")
    _fact(a, "same", "project", "Identical body.")
    _fact(b, "same", "project", "Identical body.")
    _fact(a, "moved", "project", "Old version.", mtime=1_700_000_000)
    _fact(b, "moved", "project", "New version.", mtime=1_800_000_000)
    (a / "nofm.md").write_text("Just a body, no frontmatter.\n")
    (a / "empty.md").write_text("---\nname: e\n---\n\n")
    (a / "check_index.py").write_text("print(1)\n")
    (a / "MEMORY.md.bak-1").write_text("old index\n")
    (a / "MEMORY.md").write_text(
        "# Index\n\n## Rules\n- [Fb](fb-rule.md): x\n- user-pref · odd\n\n"
        "## Projects, big\n- [P](proj-one.md) and [[ref-two]]\n- same\n"
    )
    return a, b


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(primary=SQLiteBackend(base_path=str(tmp_path / "store")))


def _all(store):
    return store.list_memories(tags=["harness-memory"], limit=1000)


def test_plan_counts_and_skips(dirs):
    plan = build_plan(list(dirs))
    r = plan.report()
    assert r["by_type"]["feedback"] == 1 and r["by_type"]["user"] == 1
    assert r["overlap_identical"] == 1 and r["overlap_diverged"] == 1
    reasons = {Path(p).name: why for p, why in r["skipped"]}
    assert reasons["MEMORY.md"] == "index file"
    assert reasons["check_index.py"] == "helper script"
    assert reasons["MEMORY.md.bak-1"] == "not markdown"
    assert reasons["empty.md"] == "empty body"
    assert any(p.endswith("nofm.md") for p in r["no_frontmatter"])
    assert any(p.endswith("odd.md") for p in r["unknown_type"])
    assert "Rules" in r["by_section"] and "Projects, big" in r["by_section"]


def test_import_mapping(dirs, tmp_path):
    store = _store(tmp_path)
    import_harness_memory(store, list(dirs))
    by_slug = {
        m.metadata["harness_slug"]: m for m in _all(store) if "superseded-variant" not in m.tags
    }
    fb = by_slug["fb-rule"]
    assert (fb.kind, fb.layer.value, fb.source_tier) == ("rule", "long-term", "user-stated")
    assert fb.context_tag == "@chef-only"
    assert fb.source == fb.source_ref == str((dirs[0] / "fb-rule.md").resolve())
    assert "[[proj-one]]" in fb.content and "originSessionId" not in fb.content
    assert fb.metadata["harness_links"] == ["proj-one", "ref-two"]
    assert {"harness-memory", "feedback", "index:Rules"} <= set(fb.tags)
    assert by_slug["proj-one"].kind == "fact" and by_slug["proj-one"].layer.value == "mid-term"
    assert by_slug["user-pref"].kind == "preference"
    assert by_slug["odd"].kind == "fact" and "infrastructure" in by_slug["odd"].tags
    assert "index:Projects, big" in by_slug["proj-one"].tags
    # mtime when the body has no date, the body date when it has one
    assert by_slug["proj-one"].event_at.startswith("2023-11-14")
    assert by_slug["ref-two"].event_at.startswith("2026-11-06")
    assert by_slug["nofm"].title == "nofm"


def test_long_body_stays_one_memory(tmp_path):
    d = tmp_path / "m"
    _fact(d, "long", "project", "word " * 5000)
    store = _store(tmp_path)
    import_harness_memory(store, [d])
    mems = _all(store)
    assert len(mems) == 1 and len(mems[0].content) >= 24000
    assert store.decompose_min_length < 10**9  # restored


def test_overlaps(dirs, tmp_path):
    store = _store(tmp_path)
    import_harness_memory(store, list(dirs))
    mems = _all(store)
    same = [m for m in mems if m.metadata["harness_slug"] == "same"]
    assert len(same) == 1 and len(same[0].metadata["harness_paths"]) == 2
    moved = [m for m in mems if m.metadata["harness_slug"] == "moved"]
    assert len(moved) == 2
    new = next(m for m in moved if "superseded-variant" not in m.tags)
    old = next(m for m in moved if "superseded-variant" in m.tags)
    assert new.content == "New version." and new.valid_to is None
    assert new.source_ref.startswith(str(dirs[1].resolve()))
    assert new.supersedes_id == old.id
    assert store.recall(old.id).valid_to is not None


def test_idempotent(dirs, tmp_path):
    store = _store(tmp_path)
    first = import_harness_memory(store, list(dirs))
    n = len(_all(store))
    second = import_harness_memory(store, list(dirs))
    assert first["imported"] == n and second["imported"] == 0
    assert len(_all(store)) == n
    # same content at a new path is deduped on the content hash
    other = dirs[0].parent / "c" / "memory"
    _fact(other, "user-pref", "user", "Likes tea.")
    assert import_harness_memory(store, [other])["imported"] == 0


def test_triage_env_restored(dirs, tmp_path, monkeypatch):
    monkeypatch.delenv("SKMEMORY_WRITE_TRIAGE", raising=False)
    import_harness_memory(_store(tmp_path), list(dirs))
    assert "SKMEMORY_WRITE_TRIAGE" not in os.environ


def test_cli_dry_run_never_opens_a_store(dirs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run opened a store")

    monkeypatch.setattr("skmemory.cli._get_store", boom)
    res = CliRunner().invoke(cli, ["import", "harness-memory", *map(str, dirs), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Memories to write" in res.output and "DRY RUN" in res.output


def test_cli_import_writes_to_injected_store(dirs, tmp_path):
    store = _store(tmp_path)
    res = CliRunner().invoke(
        cli, ["import", "harness-memory", *map(str, dirs)], obj={"store": store}
    )
    assert res.exit_code == 0, res.output
    assert _all(store)


def _moved_dirs(tmp_path):
    a, b = tmp_path / "a" / "memory", tmp_path / "b" / "memory"
    _fact(a, "moved", "project", "Old version.", mtime=1_700_000_000)
    _fact(b, "moved", "project", "New version.", mtime=1_800_000_000)
    return [a, b]


def _open_moved(store):
    return [m for m in _all(store) if m.metadata["harness_slug"] == "moved" and m.valid_to is None]


def test_resume_after_interrupt_between_variants(tmp_path):
    dirs = _moved_dirs(tmp_path)
    store = _store(tmp_path)
    real = store.snapshot
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    store.snapshot = flaky
    with pytest.raises(KeyboardInterrupt):
        import_harness_memory(store, dirs)
    store.snapshot = real
    assert len(_all(store)) == 1
    stats = import_harness_memory(store, dirs)
    assert stats["imported"] == 1
    open_ = _open_moved(store)
    assert len(open_) == 1 and open_[0].content == "New version."
    assert open_[0].supersedes_id is not None


def test_rerun_repairs_already_broken_store(tmp_path):
    dirs = _moved_dirs(tmp_path)
    store = _store(tmp_path)
    import_harness_memory(store, dirs)
    old = next(m for m in _all(store) if "superseded-variant" in m.tags)
    old.valid_to = None  # reopen: both variants open, as the interrupted run left it
    store.primary.save(old)
    assert len(_open_moved(store)) == 2
    stats = import_harness_memory(store, dirs)
    assert stats["imported"] == 0 and stats["repaired"] == 1
    assert len(_open_moved(store)) == 1
    assert import_harness_memory(store, dirs)["repaired"] == 0


def test_content_hash_dedupe_counts_as_deduped(tmp_path):
    d = tmp_path / "m"
    _fact(d, "dup", "project", "Same words.")
    store = _store(tmp_path)
    store.snapshot(title="pre", content="Same words.", tags=["x"])
    stats = import_harness_memory(store, [d])
    assert stats["imported"] == 0 and stats["deduped"] == 1


def test_unrelated_memory_with_same_body_is_never_closed(tmp_path):
    dirs = _moved_dirs(tmp_path)
    store = _store(tmp_path)
    mine = store.snapshot(title="mine", content="Old version.", tags=["mine"])
    for _ in range(3):  # across reruns too
        import_harness_memory(store, dirs)
        got = store.recall(mine.id)
        assert got.valid_to is None and "superseded_by" not in got.metadata
        assert got.tags == ["mine"]
    new = [m for m in _all(store) if m.content == "New version."]
    assert len(new) == 1 and new[0].supersedes_id is None


def test_import_honours_no_vector(dirs, monkeypatch, tmp_path):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return _store(tmp_path)

    monkeypatch.setattr("skmemory.cli._get_store", fake)
    res = CliRunner().invoke(cli, ["--no-vector", "import", "harness-memory", *map(str, dirs)])
    assert res.exit_code == 0, res.output
    assert seen["no_vector"] is True
