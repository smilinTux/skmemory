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


def test_recall_redacts_for_reader(store):
    m = store.recall("pub1", reader="@public")
    assert m.related_ids == ["pub2"]
    assert m.parent_id is None and m.supersedes_id is None
    assert "superseded_by" not in m.metadata
    assert "possible_conflict_with" not in m.metadata
    assert "write_triage" not in m.metadata
    assert m.metadata["decomposition"] == {"idx": 0}
    assert PARENT_RELATION_KEY not in m.metadata
    _no_hidden(m.model_dump_json())


def test_recall_does_not_mutate_stored_record(store):
    store.recall("pub1", reader="@public")
    again = store.primary.load("pub1")
    assert again.parent_id == "priv2" and "priv1" in again.related_ids


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
    m = store.recall("pub3", reader="@public")
    assert m.related_ids == ["pub2"] and m.parent_id == "pub1" and m.supersedes_id == "pub2"
    assert m.metadata["superseded_by"] == {"id": "pub2"}


def test_owner_recall_is_byte_identical(store, monkeypatch):
    raw = store.primary.load("pub1")
    assert store.recall("pub1") is not None
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


def test_store_search_and_list_redact(store):
    hits = store.search("linkage zebra", reader="@public")
    assert [m.id for m in hits] == ["pub1"]
    _no_hidden(hits[0].model_dump_json())
    for m in store.list_memories(reader="@public"):
        _no_hidden(m.model_dump_json())
