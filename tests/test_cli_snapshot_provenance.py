"""CLI ``snapshot`` provenance options and ``recall --exact/--reader``.

Callers that cannot import skmemory (Hermes runs in its own venv) store and verify
memories through the CLI, so it must carry what the store API takes: source tier,
source ref, kind, outcome and metadata, with a machine-readable result; and recall
must be exact and reader-aware, a hidden id reading as not found.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skmemory import cli as cli_mod
from skmemory.audience import READER_ENV
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)
    s = MemoryStore(primary=SQLiteBackend(str(tmp_path / "mem")), use_sqlite=True)
    monkeypatch.setattr(cli_mod, "_get_store", lambda **_kw: s)
    s.primary.save(
        Memory(
            id="pub-mem-0001",
            title="Public",
            content="visible to anyone",
            layer=MemoryLayer.MID,
            context_tag="@public",
        )
    )
    s.primary.save(
        Memory(
            id="priv-mem-0001",
            title="Private",
            content="owner only text",
            layer=MemoryLayer.MID,
            context_tag="@chef-only",
        )
    )
    return s


def run(*args, env=None):
    return CliRunner().invoke(cli_mod.cli, ["--no-vector", *args], env=env)


def test_snapshot_json_carries_provenance(store):
    r = run(
        "snapshot",
        "Skill lesson: probe",
        "Check the lock first.",
        "--layer",
        "mid-term",
        "--tags",
        "lesson,hermes-curator",
        "--source",
        "hermes-curator",
        "--source-tier",
        "self-generated",
        "--source-ref",
        "hermes-curator:run-7",
        "--kind",
        "procedure",
        "--outcome",
        "lesson",
        "--metadata-json",
        '{"cited_ids": ["pub-mem-0001"], "skill": "probe"}',
        "--json",
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    mem = store.recall(out["id"])
    assert mem.source_tier == "self-generated"
    assert mem.source_ref == "hermes-curator:run-7"
    assert (mem.kind, mem.outcome) == ("procedure", "lesson")
    assert mem.metadata["cited_ids"] == ["pub-mem-0001"]
    assert out == {
        "id": mem.id,
        "layer": "mid-term",
        "kind": "procedure",
        "outcome": "lesson",
        "source_tier": "self-generated",
    }


def test_snapshot_defaults_unchanged(store):
    r = run("snapshot", "T", "C")
    assert r.exit_code == 0 and r.output.startswith("Snapshot saved: ")


def test_snapshot_rejects_bad_metadata_json(store):
    r = run("snapshot", "T", "C", "--metadata-json", "[1, 2]")
    assert r.exit_code != 0 and "JSON object" in r.output


def test_snapshot_rejects_unknown_tier(store):
    assert run("snapshot", "T", "C", "--source-tier", "gossip").exit_code != 0


def test_recall_exact_refuses_a_prefix(store):
    assert run("recall", "--exact", "pub-mem").exit_code == 1
    r = run("recall", "--exact", "pub-mem-0001")
    assert r.exit_code == 0 and json.loads(r.output)["id"] == "pub-mem-0001"


def test_recall_reader_hides_what_it_may_not_see(store):
    assert run("recall", "--reader", "@public", "--exact", "priv-mem-0001").exit_code == 1
    assert run("recall", "--reader", "@public", "--exact", "pub-mem-0001").exit_code == 0
    assert run("recall", "--exact", "priv-mem-0001").exit_code == 0  # the owner sees it


def test_recall_env_ceiling_applies(store):
    r = run("recall", "--exact", "priv-mem-0001", env={READER_ENV: "@public"})
    assert r.exit_code == 1
