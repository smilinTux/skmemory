"""Tests for repairing flat memory files the model cannot load.

The properties that matter: a repair never invents content, never touches a
file it can already load, defaults to a preview, and reports the producer so
the upstream cause can be chased rather than endlessly re-repaired.
"""

from __future__ import annotations

import json

import pytest

from skmemory.repair import (
    MAX_DERIVED_TITLE,
    derive_title,
    normalise_legacy_id,
    repair_missing_titles,
    scan_unloadable,
)

GOOD = {
    "id": "aaaa",
    "title": "already fine",
    "content": "this file can be loaded",
    "layer": "short-term",
    "source": "test",
}

#: The real shape found in the live store: no title, and `memory_id` rather
#: than `id`, which is how a foreign producer wrote it.
LEGACY_NO_TITLE = {
    "memory_id": "50df5eabce3b",
    "content": "Daily infra security scan (cron) on 2026-09-14: 11 MED findings.\nSecond line.",
    "layer": "mid-term",
    "source": "telegram-bridge",
    "tags": ["cron"],
    "importance": 0.5,
}


def _write(tmp_path, tier, name, payload):
    d = tmp_path / tier
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# derive_title
# ---------------------------------------------------------------------------


def test_derive_title_takes_the_first_nonblank_line():
    assert derive_title("\n\n  first line  \nsecond line") == "first line"


def test_derive_title_truncates_on_a_word_boundary():
    content = "word " * 60
    title = derive_title(content)
    assert len(title) <= MAX_DERIVED_TITLE
    assert not title.endswith("wor")


def test_derive_title_returns_empty_for_empty_content():
    """Empty means NOT repairable. Inventing a placeholder would make the
    memory assert something it does not say."""
    assert derive_title("") == ""
    assert derive_title("   \n  \n") == ""


def test_derive_title_is_a_prefix_of_the_content():
    """A derived title can only ever copy text the memory already contains."""
    content = "The quick brown fox jumped over the lazy dog and kept going"
    assert content.startswith(derive_title(content))


def test_derive_title_is_deterministic():
    content = "some content here\nmore"
    assert derive_title(content) == derive_title(content)


# ---------------------------------------------------------------------------
# scan_unloadable
# ---------------------------------------------------------------------------


def test_scan_ignores_loadable_files(tmp_path):
    _write(tmp_path, "short-term", "ok", GOOD)
    report = scan_unloadable(tmp_path)
    assert report.scanned == 1
    assert report.unloadable == []


def test_scan_finds_a_title_less_file_and_marks_it_repairable(tmp_path):
    _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    report = scan_unloadable(tmp_path)
    assert len(report.unloadable) == 1
    item = report.unloadable[0]
    assert item.missing == ("title",)
    assert item.repairable


def test_scan_reports_the_producer(tmp_path):
    """Repairing without naming the producer just lets the files come back."""
    _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    assert scan_unloadable(tmp_path).unloadable[0].source == "telegram-bridge"


def test_scan_picks_up_the_legacy_memory_id_key(tmp_path):
    _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    assert scan_unloadable(tmp_path).unloadable[0].memory_id == "50df5eabce3b"


def test_a_file_missing_content_is_found_but_not_repairable(tmp_path):
    _write(tmp_path, "short-term", "nocontent", {"id": "x", "title": "t"})
    item = scan_unloadable(tmp_path).unloadable[0]
    assert item.missing == ("content",)
    assert not item.repairable


def test_unparseable_json_is_reported_not_raised(tmp_path):
    d = tmp_path / "short-term"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    item = scan_unloadable(tmp_path).unloadable[0]
    assert item.missing == ("<unparseable>",)
    assert not item.repairable


def test_scan_skips_the_archive_tier(tmp_path):
    """Archive is cold storage, not something a read path loads."""
    _write(tmp_path, "archive", "legacy", LEGACY_NO_TITLE)
    assert scan_unloadable(tmp_path).scanned == 0


# ---------------------------------------------------------------------------
# repair_missing_titles
# ---------------------------------------------------------------------------


def test_repair_defaults_to_a_dry_run_and_changes_nothing(tmp_path):
    p = _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    before = p.read_text(encoding="utf-8")

    report = repair_missing_titles(tmp_path)

    assert report.dry_run is True
    assert report.repaired == [p]
    assert p.read_text(encoding="utf-8") == before, "a dry run wrote to disk"


def test_repair_applies_only_the_title_and_preserves_every_other_field(tmp_path):
    p = _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)

    report = repair_missing_titles(tmp_path, dry_run=False)

    assert report.dry_run is False
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["title"] == "Daily infra security scan (cron) on 2026-09-14: 11 MED findings."
    for key, value in LEGACY_NO_TITLE.items():
        assert after[key] == value, f"{key} was altered by the repair"


def test_repair_is_idempotent(tmp_path):
    p = _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    repair_missing_titles(tmp_path, dry_run=False)
    first = p.read_text(encoding="utf-8")

    second_report = repair_missing_titles(tmp_path, dry_run=False)

    assert second_report.unloadable == [], "a repaired file is still reported as unloadable"
    assert second_report.repaired == []
    assert p.read_text(encoding="utf-8") == first


def test_repair_leaves_a_loadable_file_untouched(tmp_path):
    p = _write(tmp_path, "short-term", "ok", GOOD)
    before = p.read_text(encoding="utf-8")
    repair_missing_titles(tmp_path, dry_run=False)
    assert p.read_text(encoding="utf-8") == before


def test_repair_skips_what_it_cannot_derive(tmp_path):
    _write(tmp_path, "short-term", "nocontent", {"id": "x", "title": "t"})
    _write(tmp_path, "short-term", "empty", {"id": "y", "content": "   "})
    report = repair_missing_titles(tmp_path, dry_run=False)
    assert report.repaired == []
    assert len(report.skipped) == 2


def test_repaired_file_actually_loads_as_a_memory(tmp_path):
    """The whole point: after repair the model can read it."""
    from skmemory.models import Memory

    p = _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    repair_missing_titles(tmp_path, dry_run=False)
    mem = Memory.model_validate_json(p.read_text(encoding="utf-8"))
    assert mem.title
    assert mem.content == LEGACY_NO_TITLE["content"]


def test_summary_states_whether_it_was_applied(tmp_path):
    _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    assert "DRY RUN" in repair_missing_titles(tmp_path).summary()
    assert "APPLIED" in repair_missing_titles(tmp_path, dry_run=False).summary()


# ---------------------------------------------------------------------------
# legacy memory_id normalisation
# ---------------------------------------------------------------------------


def test_normalise_legacy_id_copies_memory_id_into_id():
    obj = {"memory_id": "50df5eabce3b", "content": "x", "title": "t"}
    assert normalise_legacy_id(obj) is True
    assert obj["id"] == "50df5eabce3b"


def test_normalise_legacy_id_leaves_the_legacy_key_in_place():
    """Removing a field another reader may want is not this function's job."""
    obj = {"memory_id": "abc123", "content": "x"}
    normalise_legacy_id(obj)
    assert obj["memory_id"] == "abc123"


def test_normalise_legacy_id_does_not_overwrite_a_real_id():
    obj = {"id": "canonical", "memory_id": "legacy", "content": "x"}
    assert normalise_legacy_id(obj) is False
    assert obj["id"] == "canonical"


@pytest.mark.parametrize("legacy", [None, "", "   ", 123, []])
def test_normalise_legacy_id_ignores_an_unusable_legacy_value(legacy):
    obj = {"content": "x"}
    if legacy is not None:
        obj["memory_id"] = legacy
    assert normalise_legacy_id(obj) is False
    assert "id" not in obj


def test_repair_normalises_the_id_so_the_memory_keeps_its_identity(tmp_path):
    """The subtler half of the repair, and the one that caused real damage.

    ``Memory`` reads ``id``. A file carrying only ``memory_id`` gets a brand new
    uuid minted on every load, so saving it to the derived Postgres cache
    INSERTS A DUPLICATE instead of updating the row it came from. Measured:
    refreshing 17 such files took lumina from 20,299 rows to 20,316 with all 17
    originals untouched.
    """
    from skmemory.models import Memory

    p = _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    repair_missing_titles(tmp_path, dry_run=False)

    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["id"] == LEGACY_NO_TITLE["memory_id"]

    # The decisive assertion: loading it twice yields the SAME id, so a save
    # updates its own row instead of creating a new one.
    first = Memory.model_validate_json(p.read_text(encoding="utf-8"))
    second = Memory.model_validate_json(p.read_text(encoding="utf-8"))
    assert first.id == second.id == LEGACY_NO_TITLE["memory_id"]


def test_the_repaired_id_matches_the_filename(tmp_path):
    """The flat store names files by id, so the canonical id was always there,
    just under the wrong key and in the filename."""
    p = _write(tmp_path, "mid-term", LEGACY_NO_TITLE["memory_id"], LEGACY_NO_TITLE)
    repair_missing_titles(tmp_path, dry_run=False)
    assert json.loads(p.read_text(encoding="utf-8"))["id"] == p.stem


def test_scan_flags_a_legacy_id_only_file(tmp_path):
    _write(tmp_path, "mid-term", "legacy", LEGACY_NO_TITLE)
    assert scan_unloadable(tmp_path).unloadable[0].legacy_id_only is True
