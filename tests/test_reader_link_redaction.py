"""Hidden ids must not leak through link fields to a restricted reader (SKMEM-P0-21).

Tmp store only. Owner output must be byte-identical to the unredacted record.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skmemory import mcp_server
from skmemory.audience import READER_ENV
from skmemory.backends.file_backend import FileBackend
from skmemory.models import PARENT_RELATION_KEY, Memory, MemoryLayer
from skmemory.store import MemoryStore

HIDDEN = ("priv1", "priv2", "priv3")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    s = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    for mid in HIDDEN:
        s.primary.save(
            Memory(
                id=mid,
                title=mid,
                content="secret",
                layer=MemoryLayer.MID,
                context_tag="@chef-only",
            )
        )
    s.primary.save(Memory(id="pub2", title="pub2", content="open two", context_tag="@public"))
    s.primary.save(
        Memory(
            id="pub1",
            title="pub1",
            content="open linkage zebra",
            layer=MemoryLayer.MID,
            context_tag="@public",
            related_ids=["priv1", "pub2", "ghost"],
            parent_id="priv2",
            supersedes_id="priv3",
            metadata={
                "superseded_by": {"id": "priv1", "at": "2026-01-01"},
                "possible_conflict_with": ["priv2"],
                "write_triage": {"verdict": "changes", "target": "priv3"},
                "decomposition": {"parent_id": "priv2", "idx": 0},
                PARENT_RELATION_KEY: "supersedes",
                "promoted_id": "priv1",
                "chunk_memory_ids": ["priv2", "pub2", "ghost"],
                "observation": {"proof_count": 3, "evidence_ids": ["priv3", "pub2"], "key": "k"},
            },
        )
    )
    return s


def call(store, monkeypatch, name, **args):
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    return asyncio.run(mcp_server.call_tool(name, args))[0].text


def _no_hidden(text: str):
    for h in HIDDEN:
        assert h not in text
    assert "ghost" not in text


def test_redact_links_for_reader(store):
    m = store.redact_links(store.recall("pub1"), "@public")
    assert m.related_ids == ["pub2"]
    assert m.parent_id is None and m.supersedes_id is None
    for key in (
        "superseded_by",
        "possible_conflict_with",
        "write_triage",
        "promoted_id",
        PARENT_RELATION_KEY,
    ):
        assert key not in m.metadata, key
    assert m.metadata["decomposition"] == {"idx": 0}
    assert m.metadata["chunk_memory_ids"] == ["pub2"]
    assert m.metadata["observation"] == {"proof_count": 3, "evidence_ids": ["pub2"], "key": "k"}
    _no_hidden(m.model_dump_json())


@pytest.mark.parametrize(
    "key,value",
    [
        ("promoted_id", "priv1"),
        ("chunk_memory_ids", ["priv1", "ghost"]),
        ("observation", {"evidence_ids": ["priv1"], "key": "k"}),
        ("superseded_by", {"id": "priv1"}),
        ("possible_conflict_with", ["priv1"]),
        ("write_triage", {"target": "priv1"}),
        ("decomposition", {"parent_id": "priv1"}),
    ],
)
def test_each_metadata_field_redacted(store, key, value):
    store.primary.save(
        Memory(id="solo", title="solo", content="x", context_tag="@public", metadata={key: value})
    )
    out = store.redact_links(store.recall("solo"), "@public")
    _no_hidden(out.model_dump_json())


def test_storage_apis_return_real_links_under_ceiling(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    assert store.recall("pub1").parent_id == "priv2"
    assert "priv1" in store.search("linkage zebra")[0].related_ids
    assert "priv1" in {i for m in store.list_memories() if m.id == "pub1" for i in m.related_ids}


def test_ceiling_promote_and_refresh_leave_links_unchanged(store, monkeypatch):
    from skmemory.observations import refresh_observation
    from skmemory.promotion import PromotionEngine

    obs = Memory(
        id="obs1",
        title="o",
        content="o",
        context_tag="@public",
        related_ids=["priv1"],
        metadata={
            "observation": {"evidence_ids": ["priv1", "pub2"], "proof_count": 2, "key": "k"}
        },
    )
    store.primary.save(obs)
    before = {i: store.primary.load(i).model_dump_json() for i in ("pub1", "obs1")}
    monkeypatch.setenv(READER_ENV, "@public")
    with pytest.raises(PermissionError):
        store.promote("pub1", MemoryLayer.LONG)
    assert (
        PromotionEngine(store).promote_memory(store.primary.load("pub1"), MemoryLayer.LONG) is None
    )
    assert refresh_observation(store, store.primary.load("obs1"), "k") == "refused"
    for i, raw in before.items():
        assert store.primary.load(i).model_dump_json() == raw
    assert store.primary.load("pub1").parent_id == "priv2"
    assert "priv1" in store.primary.load("pub1").related_ids


def test_cli_recall_redacts(store, monkeypatch):
    from click.testing import CliRunner

    from skmemory.cli import cli

    monkeypatch.setenv(READER_ENV, "@public")
    res = CliRunner().invoke(cli, ["recall", "pub1"], obj={"store": store})
    assert res.exit_code == 0, res.output
    _no_hidden(res.output)


def test_visible_links_are_kept(store):
    store.primary.save(
        Memory(
            id="pub3",
            title="pub3",
            content="x",
            context_tag="@public",
            related_ids=["pub2"],
            parent_id="pub1",
            supersedes_id="pub2",
            metadata={"superseded_by": {"id": "pub2"}},
        )
    )
    m = store.redact_links(store.recall("pub3"), "@public")
    assert m.related_ids == ["pub2"] and m.parent_id == "pub1" and m.supersedes_id == "pub2"
    assert m.metadata["superseded_by"] == {"id": "pub2"}


def test_owner_recall_is_byte_identical(store, monkeypatch):
    raw = store.primary.load("pub1")
    owner = call(store, monkeypatch, "memory_recall", memory_id="pub1")
    assert json.loads(owner) == json.loads(json.dumps(mcp_server._memory_dict(raw), default=str))
    assert all(h in owner for h in ("priv1", "priv2", "priv3"))
    assert store.redact_links(raw) is raw
    assert store.search("linkage zebra")[0].model_dump_json() == raw.model_dump_json()
    listed = {m.id: m for m in store.list_memories()}
    assert listed["pub1"].model_dump_json() == raw.model_dump_json()


def test_mcp_recall_list_search_redact(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    _no_hidden(call(store, monkeypatch, "memory_recall", memory_id="pub1"))
    _no_hidden(call(store, monkeypatch, "memory_list", limit=10))
    _no_hidden(call(store, monkeypatch, "memory_search", query="linkage zebra"))
    _no_hidden(call(store, monkeypatch, "memory_search", query="linkage zebra", detail="index"))
