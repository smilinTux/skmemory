"""Residual hidden-id paths (SKMEM-P0-22). Tmp store only."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from skmemory import mcp_server
from skmemory.audience import READER_ENV
from skmemory.backends.file_backend import FileBackend
from skmemory.models import Memory, MemoryLayer
from skmemory.observations import (
    Observation,
    ObservationEvidence,
    refresh_observation,
    sweep_observations,
    write_observation,
)
from skmemory.openclaw import SKMemoryPlugin
from skmemory.sharing import MemorySharer, ShareFilter
from skmemory.store import MemoryStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    return MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))


def _put(store, mid, tag, content="plain text here", **kw):
    m = Memory(id=mid, title=mid, content=content, layer=MemoryLayer.MID, context_tag=tag, **kw)
    store.primary.save(m)
    return m


def _call(store, monkeypatch, name, **args):
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    return asyncio.run(mcp_server.call_tool(name, args))[0].text


# 1. sharing bundles ---------------------------------------------------------


def test_share_bundle_drops_hidden_and_redacts_links(store):
    _put(store, "hid", "@chef-only", "hidden body")
    _put(store, "pub2", "@public", "open", tags=["t"])
    _put(
        store, "pub1", "@public", "open", tags=["t"], related_ids=["hid", "pub2"], parent_id="hid"
    )
    bundle = MemorySharer(store).export_memories(ShareFilter(tags=["t"]), reader="@public")
    text = json.dumps(bundle.memories)
    assert "hid" not in text
    assert bundle.memory_count == 2
    by_id = {m["id"]: m for m in bundle.memories}
    assert by_id["pub1"]["related_ids"] == ["pub2"]


def test_share_bundle_hidden_by_id_is_dropped(store):
    _put(store, "hid", "@chef-only", "hidden body")
    bundle = MemorySharer(store).export_memories(ShareFilter(memory_ids=["hid"]), reader="@public")
    assert bundle.memories == [] and bundle.memory_count == 0


def test_share_bundle_env_ceiling_applies_without_reader(store, monkeypatch):
    _put(store, "hid", "@chef-only", "hidden body", tags=["t"])
    monkeypatch.setenv(READER_ENV, "@public")
    assert MemorySharer(store).export_memories(ShareFilter(tags=["t"])).memories == []


def test_share_bundle_owner_unchanged(store):
    _put(store, "hid", "@chef-only", "hidden body", tags=["t"])
    _put(store, "pub1", "@public", "x", tags=["t"], related_ids=["hid"])
    b = MemorySharer(store).export_memories(ShareFilter(tags=["t"]))
    assert b.memory_count == 2
    assert {"hid"} == set(next(m for m in b.memories if m["id"] == "pub1")["related_ids"])


# 2. observation text and counts ----------------------------------------------


def _observation(store, n=3):
    srcs = []
    for i in range(n):
        m = store.snapshot(
            title=f"s{i}", content=f"quote number {i} about zebra", layer=MemoryLayer.SHORT
        )
        m.context_tag = "@public"
        store.primary.save(m)
        srcs.append(m)
    ev = [
        ObservationEvidence(memory_id=m.id, quote=m.content, timestamp=datetime.now(timezone.utc))
        for m in srcs
    ]
    obs = write_observation(
        store,
        Observation(
            statement=f"A recurring pattern connects {n} memories around 'zebra'.", evidence=ev
        ),
        key="zebra",
    )
    assert obs is not None
    return obs, srcs


def test_observation_hides_reclassified_evidence(store):
    obs, srcs = _observation(store)
    hidden = srcs[0]
    hidden.context_tag = "@chef-only"
    store.primary.save(hidden)
    out = store.redact_links(store.recall(obs.id), "@public")
    blob = json.dumps(out.model_dump(mode="json"))
    assert hidden.id not in blob
    assert "quote number 0" not in blob
    assert out.metadata["observation"]["proof_count"] == 2
    assert len(out.metadata["observation"]["evidence_ids"]) == 2
    assert srcs[1].id in out.content and srcs[2].id in out.content
    assert "connects 2 memories" in out.content or "connects 2 memories" in out.title + out.content


def test_observation_all_hidden_leaves_no_ids(store):
    obs, srcs = _observation(store)
    for m in srcs:
        m.context_tag = "@chef-only"
        store.primary.save(m)
    out = store.redact_links(store.recall(obs.id), "@public")
    blob = json.dumps(out.model_dump(mode="json"))
    assert all(m.id not in blob for m in srcs)
    assert out.metadata["observation"]["proof_count"] == 0


def test_observation_owner_untouched(store):
    obs, srcs = _observation(store)
    srcs[0].context_tag = "@chef-only"
    store.primary.save(srcs[0])
    rec = store.recall(obs.id)
    assert store.redact_links(rec) is rec


def test_observation_mcp_recall_redacts(store, monkeypatch):
    obs, srcs = _observation(store)
    srcs[0].context_tag = "@chef-only"
    store.primary.save(srcs[0])
    monkeypatch.setenv(READER_ENV, "@public")
    text = _call(store, monkeypatch, "memory_recall", memory_id=obs.id)
    assert srcs[0].id not in text and "quote number 0" not in text


# 3. per-call reader writes ----------------------------------------------------


def test_mcp_promote_refuses_per_call_reader(store, monkeypatch):
    _put(store, "pub1", "@public")
    out = _call(
        store,
        monkeypatch,
        "memory_promote",
        memory_id="pub1",
        target="long-term",
        reader="@public",
    )
    assert "owner-only" in out
    assert store.primary.load("pub1").layer == MemoryLayer.MID
    assert len(store.list_memories(limit=50)) == 1


def test_mcp_promote_owner_still_works(store, monkeypatch):
    _put(store, "pub1", "@public")
    out = _call(store, monkeypatch, "memory_promote", memory_id="pub1", target="long-term")
    assert "promoted_id" in out


def test_store_promote_refuses_per_call_reader(store):
    _put(store, "pub1", "@public")
    with pytest.raises(PermissionError):
        store.promote("pub1", MemoryLayer.LONG, reader="@public")


def test_observation_writes_refuse_per_call_reader(store):
    obs, srcs = _observation(store)
    n = len(store.list_memories(limit=100))
    ev = [
        ObservationEvidence(memory_id=m.id, quote=m.content, timestamp=datetime.now(timezone.utc))
        for m in srcs
    ]
    assert (
        write_observation(
            store,
            Observation(statement="x " + srcs[0].content, evidence=ev),
            key="k2",
            reader="@public",
        )
        is None
    )
    assert refresh_observation(store, obs, "zebra", reader="@public") == "refused"
    assert sweep_observations(store, reader="@public")["written"] == 0
    assert len(store.list_memories(limit=100)) == n


def test_policy_table_marks_promote_owner_only():
    assert mcp_server.TOOL_READER_POLICY["memory_promote"][0] == "owner-only"


# 4. openclaw recall -------------------------------------------------------------


def test_openclaw_recall_redacts_under_env_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    plugin = SKMemoryPlugin(base_path=str(tmp_path / "m"))
    _put(plugin.store, "hid", "@chef-only", "hidden body")
    _put(plugin.store, "pub1", "@public", "open", related_ids=["hid"], parent_id="hid")
    monkeypatch.setenv(READER_ENV, "@public")
    got = plugin.recall("pub1")
    assert got["related_ids"] == [] and got["parent_id"] is None
    assert plugin.recall("hid") is None


def test_openclaw_recall_owner_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    plugin = SKMemoryPlugin(base_path=str(tmp_path / "m"))
    _put(plugin.store, "hid", "@chef-only", "hidden body")
    _put(plugin.store, "pub1", "@public", "open", related_ids=["hid"])
    assert plugin.recall("pub1")["related_ids"] == ["hid"]


# 5. delete paths ----------------------------------------------------------------


@pytest.mark.parametrize("mid", ["pubx", "hidx"])
@pytest.mark.parametrize("mode", ["env", "call"])
def test_mcp_forget_refused_for_declared_reader(store, monkeypatch, mid, mode):
    _put(store, "pubx", "@public")
    _put(store, "hidx", "@chef-only")
    args = {"memory_id": mid}
    if mode == "env":
        monkeypatch.setenv(READER_ENV, "@public")
    else:
        args["reader"] = "@public"
    out = _call(store, monkeypatch, "memory_forget", **args)
    assert "owner-only" in out and "deleted" not in out
    assert store.primary.load(mid) is not None


def test_mcp_forget_owner_still_works(store, monkeypatch):
    _put(store, "pubx", "@public")
    assert '"deleted": true' in _call(store, monkeypatch, "memory_forget", memory_id="pubx")
    assert store.primary.load("pubx") is None


@pytest.mark.parametrize("mode", ["env", "call"])
def test_store_forget_refused_for_declared_reader(store, monkeypatch, mode):
    _put(store, "pubx", "@public")
    kw = {}
    if mode == "env":
        monkeypatch.setenv(READER_ENV, "@public")
    else:
        kw["reader"] = "@public"
    with pytest.raises(PermissionError):
        store.forget_cascade("pubx", **kw)
    with pytest.raises(PermissionError):
        if mode == "env":
            store.forget("pubx")
        else:
            monkeypatch.setenv(READER_ENV, "@public")
            store.forget("pubx")
    assert store.primary.load("pubx") is not None


def test_observation_statement_hidden_id_is_stripped(store):
    obs, srcs = _observation(store)
    hid = srcs[0]
    obs.content = f"Pattern about {hid.id} here\n\n" + obs.content.partition("\n\n")[2]
    store.primary.save(obs)
    hid.context_tag = "@chef-only"
    store.primary.save(hid)
    out = store.redact_links(store.recall(obs.id), "@public")
    assert hid.id not in out.content
