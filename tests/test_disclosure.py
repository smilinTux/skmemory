"""Progressive disclosure: search detail=index, memory_expand (SKMEM-P3-07).

Tmp stores and files only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skmemory import disclosure, mcp_server
from skmemory.audience import READER_ENV
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


def call(store, monkeypatch, name, **args):
    monkeypatch.setattr(mcp_server, "_get_store", lambda: store)
    return asyncio.run(mcp_server.call_tool(name, args))[0].text


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(READER_ENV, raising=False)
    sess = tmp_path / "sessions"
    sess.mkdir()
    lines = [json.dumps({"n": i, "text": f"line {i}"}) for i in range(1, 41)]
    data = "\n".join(lines) + "\n"
    (sess / "s1.jsonl").write_text(data)
    monkeypatch.setattr(mcp_server, "sessions_dir", lambda: sess)
    store = MemoryStore(
        primary=__import__("skmemory.backends.sqlite_backend", fromlist=["x"]).SQLiteBackend(
            str(tmp_path / "mem")
        )
    )

    def add(mid, ref, tag="@public"):
        store.primary.save(
            Memory(
                id=mid,
                title=f"Digest {mid}",
                content="gateway port decision " + "x" * 400,
                layer=MemoryLayer.MID,
                context_tag=tag,
                source_ref=ref,
            )
        )

    add("a", "session:s1@L20")
    add("b", "session:s1@0-30")
    add("c", "session:../evil@L1")
    add("d", "session:s1")
    add("priv", "session:s1@L3", tag="@chef-only")
    return store, sess, data


def test_default_search_shape_unchanged(env, monkeypatch):
    store = env[0]
    rows = json.loads(call(store, monkeypatch, "memory_search", query="gateway port"))
    assert rows and "content" in rows[0] and "snippet" not in rows[0]


def test_index_mode_rows(env, monkeypatch):
    store = env[0]
    out = json.loads(
        call(store, monkeypatch, "memory_search", query="gateway port", detail="index")
    )
    assert out
    r = out[0]
    assert {"id", "title", "date", "score", "snippet", "source_ref", "expandable"} <= set(r)
    assert "content" not in r and len(r["snippet"]) <= disclosure.SNIPPET_CHARS
    assert r["est_tokens"] < r["est_tokens_full"]
    assert [x["score"] for x in out] == sorted((x["score"] for x in out), reverse=True)
    byid = {x["id"]: x for x in out}
    assert byid["a"]["expandable"] is True and byid["c"]["expandable"] is False


def test_expand_line_and_byte_and_cap(env, monkeypatch):
    store, _, data = env
    out = json.loads(call(store, monkeypatch, "memory_expand", memory_id="a"))
    assert out["range"]["from_line"] == 10 and '"n": 20' in out["text"]
    out = json.loads(call(store, monkeypatch, "memory_expand", memory_id="b"))
    assert out["text"] == data[:30]
    out = json.loads(call(store, monkeypatch, "memory_expand", memory_id="d", max_chars=50))
    assert len(out["text"]) == 50 and out["truncated"] is True
    out = json.loads(call(store, monkeypatch, "memory_expand", memory_id="d", max_chars=10**9))
    assert len(out["text"]) <= disclosure.EXPAND_MAX_CHARS


def test_expand_refuses_traversal_and_symlink_escape(env, monkeypatch, tmp_path):
    store, sess, _ = env
    assert "error" in json.loads(call(store, monkeypatch, "memory_expand", memory_id="c"))
    outside = tmp_path / "secret.jsonl"
    outside.write_text("TOPSECRET\n")
    (sess / "esc.jsonl").symlink_to(outside)
    store.primary.save(
        Memory(
            id="e",
            title="e",
            content="e",
            layer=MemoryLayer.MID,
            context_tag="@public",
            source_ref="session:esc",
        )
    )
    out = call(store, monkeypatch, "memory_expand", memory_id="e")
    assert "outside" in out and "TOPSECRET" not in out


def test_expand_reader_rules(env, monkeypatch):
    store = env[0]
    hidden = call(store, monkeypatch, "memory_expand", memory_id="priv", reader="@public")
    missing = call(store, monkeypatch, "memory_expand", memory_id="nope", reader="@public")
    assert "not found" in hidden.lower()
    assert hidden.replace("priv", "X") == missing.replace("nope", "X")
    visible = call(store, monkeypatch, "memory_expand", memory_id="a", reader="@public")
    assert "owner-only" in visible and "line 20" not in visible
    monkeypatch.setenv(READER_ENV, "@public")
    assert "line 20" not in call(store, monkeypatch, "memory_expand", memory_id="a")
    assert "not found" in call(store, monkeypatch, "memory_expand", memory_id="priv").lower()


def test_save_session_writes_offset_ref(env, monkeypatch):
    store = env[0]
    conv = "intro line\nWe decided to go with the new gateway because it is faster and safer.\n"
    out = json.loads(
        call(
            store,
            monkeypatch,
            "memory_save_session",
            conversation=conv,
            session_id="s9",
            min_length=20,
        )
    )
    assert out["saved"] >= 1
    m = store.recall(out["memories"][0]["id"])
    assert m.source_ref == "session:s9@L2"


def test_parse_source_ref():
    p = disclosure.parse_source_ref
    assert p("session:abc@5-9").end == 9 and p("session:abc@L4").line == 4
    assert p("session:abc").session_id == "abc"
    assert p("skwhisper-daemon") is None and p("session:../x") is None
