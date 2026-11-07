"""Skill index: read-only, ledger-ranked, tolerant of malformed files (SKMEM-P4-03)."""

from __future__ import annotations

from click.testing import CliRunner

from skmemory.cli import cli
from skmemory.ledger import Ledger
from skmemory.skills_index import build_index, find_collisions, parse_frontmatter, rank


def _skill(root, name, body, d=None):
    p = root / (d or name)
    p.mkdir(parents=True, exist_ok=True)
    (p / "SKILL.md").write_text(body)
    return p / "SKILL.md"


def _fm(name, desc):
    return f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n"


def _stores(tmp_path):
    return {
        "hermes": str(tmp_path / "h"),
        "clawd": str(tmp_path / "c"),
        "claude": str(tmp_path / "cl"),
        "plugins": str(tmp_path / "pl"),
        "skcapstone": str(tmp_path / "none"),
    }


def test_malformed_files_survive(tmp_path):
    root = tmp_path / "c"
    _skill(root, "a", "---\nname: [unclosed\n: : :\n\x00\x01")
    _skill(root, "b", "no frontmatter at all\nsecond line")
    (root / "c").mkdir()
    (root / "c" / "SKILL.md").write_bytes(b"---\nname: bin\xff\xfe\ndescription: x\n---\n")
    _skill(root, "d", "")
    entries, stats = build_index({**_stores(tmp_path)})
    assert len(entries) == 4
    assert {e.name for e in entries} >= {"b", "d"}
    assert parse_frontmatter("---\nname: 'q'\ndescription: >\n  folded\n  text\n---\n") == {
        "name": "q",
        "description": "folded text",
    }
    assert parse_frontmatter(None) == {}  # type: ignore[arg-type]


def test_missing_dirs_skipped_and_readonly(tmp_path):
    p = _skill(tmp_path / "c", "x", _fm("x", "does things"))
    before = p.read_bytes()
    entries, stats = build_index(_stores(tmp_path))
    assert stats["stores"] == {"clawd": 1}
    assert "hermes" in stats["missing"]
    assert p.read_bytes() == before
    assert sorted(x.name for x in (tmp_path / "c").rglob("*")) == ["SKILL.md", "x"]


def test_collisions_need_cross_store_and_different_hash(tmp_path):
    _skill(tmp_path / "c", "dup", _fm("dup", "one"))
    _skill(tmp_path / "h", "dup", _fm("dup", "two"))
    _skill(tmp_path / "c", "same", _fm("same", "s"))
    _skill(tmp_path / "h", "same", _fm("same", "s"))
    _skill(tmp_path / "c", "solo", _fm("solo", "s"))
    _skill(tmp_path / "c", "in-store", _fm("in-store", "a"), d="p1")
    _skill(tmp_path / "c", "in-store", _fm("in-store", "b"), d="p2")
    entries, _ = build_index(_stores(tmp_path))
    col = find_collisions(entries)
    assert col["names"] == 1 and col["entries"] == 2 and col["examples"] == ["dup"]


def _ledger_with(tmp_path, entry):
    led = Ledger(tmp_path / "mem", host="a")
    for i in range(3):
        led.record(entry.name, entry.version, "served", session=f"s{i}", at="2026-10-01T00:00:00Z")
    return led


def test_ranking_anti_pattern_last_and_flags(tmp_path):
    _skill(tmp_path / "c", "good", _fm("deploy-good", "deploy the service"))
    _skill(tmp_path / "c", "bad", _fm("deploy-bad", "deploy the service"))
    _skill(tmp_path / "c", "idle", _fm("deploy-idle", "deploy the service"))
    entries, _ = build_index(_stores(tmp_path))
    by = {e.name: e for e in entries}
    led = Ledger(tmp_path / "mem", host="a")
    for i in range(3):
        for n, eff in (("deploy-good", "success"), ("deploy-bad", "harm")):
            e = by[n]
            led.record(n, e.version, "served", session=f"s{i}", at="2026-11-01T00:00:00Z")
            led.record(
                n, e.version, "followed", effect=eff, session=f"s{i}", at="2026-11-01T00:00:00Z"
            )
        e = by["deploy-idle"]
        led.record("deploy-idle", e.version, "served", session=f"s{i}", at="2026-11-01T00:00:00Z")
    hits = rank("deploy service", entries, led)
    names = [h.entry.name for h in hits]
    assert names[-1] == "deploy-bad" and hits[-1].anti_pattern
    assert names[0] == "deploy-good"
    idle = next(h for h in hits if h.entry.name == "deploy-idle")
    assert idle.flag == "served-never-followed"


def test_imported_counters_separate_and_not_scored(tmp_path):
    _skill(tmp_path / "h", "imp", _fm("imp", "import thing"))
    _skill(tmp_path / "h", "plain", _fm("plain", "import thing"))
    entries, _ = build_index(_stores(tmp_path))
    led = Ledger(tmp_path / "mem", host="a")
    for i in range(5):
        led.record(
            "imp",
            "patch-0",
            "served",
            session="hermes-usage",
            source="hermes-usage-counter",
            at="2026-11-01T00:00:00Z",
            event_id=f"id{i}",
        )
    hits = {h.entry.name: h for h in rank("import thing", entries, led)}
    assert hits["imp"].imported_served == 5
    assert hits["imp"].served == 0 and hits["imp"].flag is None and hits["imp"].ledger_net is None
    assert hits["imp"].score == hits["plain"].score


def test_cli_index_and_search(tmp_path):
    _skill(tmp_path / "c", "x", _fm("x", "backup the database"))
    _skill(tmp_path / "h", "x", _fm("x", "different"))
    args = []
    for k, v in _stores(tmp_path).items():
        args += ["--store", f"{k}={v}"]
    r = CliRunner().invoke(cli, ["skills", "index", "--base", str(tmp_path / "mem"), *args])
    assert r.exit_code == 0, r.output
    assert "clawd: 1" in r.output and "collisions: 1 names" in r.output
    r = CliRunner().invoke(
        cli, ["skills", "search", "backup database", "--base", str(tmp_path / "mem"), *args]
    )
    assert r.exit_code == 0 and "x" in r.output
