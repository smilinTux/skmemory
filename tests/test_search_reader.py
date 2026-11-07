"""Reader enforcement on the search path (SKMEM-P0-16).

The gate is the corpus-v1 privacy class: a ``@public`` reader must never receive
a ``@chef-only`` memory through any retrieval arm, and the ``@chef`` owner must
still find it. Uses a tmp SQLite store and fakes only.
"""

from __future__ import annotations

import json

import pytest

from skmemory.audience import READER_ENV, AudienceResolver, resolve_reader
from skmemory.eval.fixtures import load_corpus, load_queries
from skmemory.models import Memory, MemoryLayer
from skmemory.store import MemoryStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(READER_ENV, raising=False)
    monkeypatch.delenv("SKMEMORY_SEARCH_FUSION", raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    s = MemoryStore()
    for row in load_corpus():
        s.primary.save(
            Memory(
                id=row.id,
                title=row.title,
                content=row.content,
                layer=MemoryLayer(row.layer),
                tags=row.tags,
                context_tag=row.context_tag,
            )
        )
    return s


def _privacy():
    return [q for q in load_queries() if q.query_class == "privacy"]


def _reader(fixture_reader: str) -> str | None:
    # corpus-v1 spells the owner "@chef"; the owner is undeclared in production.
    return None if fixture_reader == "@chef" else fixture_reader


@pytest.mark.parametrize("q", _privacy(), ids=lambda q: q.id)
@pytest.mark.parametrize("fusion", ["off", "on"])
def test_corpus_v1_privacy_class(store, monkeypatch, q, fusion):
    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", fusion)
    ids = {m.id for m in store.search(q.query, limit=10, reader=_reader(q.reader))}
    assert not ids & set(q.must_not_ids), f"{q.id} leaked {ids & set(q.must_not_ids)}"
    assert set(q.gold_ids) <= ids


def test_undeclared_reader_is_owner_and_unchanged(store):
    ids = {m.id for m in store.search("dentist appointment", limit=10)}
    assert "m-af440837ed" in ids


def test_unparseable_reader_fails_closed(store):
    assert store.search("dentist appointment", limit=10, reader="@bogus") == []
    assert store.search("dentist appointment", limit=10, reader="") == []


def test_env_declares_process_and_call_cannot_widen(store, monkeypatch):
    monkeypatch.setenv(READER_ENV, "@public")
    assert not store.search("dentist appointment", limit=10)
    assert not store.search("dentist appointment", limit=10, reader="@chef-only")
    assert not store.search("dentist appointment", limit=10, reader="@chef")


def test_explicit_context_tag_cannot_widen_reader(store):
    assert not store.search(
        "dentist appointment", limit=10, reader="@public", context_tag="@chef-only"
    )


def test_scoped_reader_isolated_from_other_scope():
    own = resolve_reader("@work:a").allowed_tags
    assert "@work:a" in own and "@work:b" not in own and "@chef-only" not in own
    assert "@work-circle" not in own


def test_channel_reader(tmp_path):
    cfg = tmp_path / "aud.json"
    cfg.write_text(
        json.dumps(
            {
                "channels": {
                    "c1": {"context_tag": "@work:x", "members": ["Pat"]},
                    "solo": {"context_tag": "@chef-only", "members": []},
                },
                "people": {"Pat": {"trust_level": 2, "never_share": ["intimate"]}},
            }
        )
    )
    res = AudienceResolver(cfg)
    s = resolve_reader("channel:c1", res)
    assert s.allows("@work:x") and s.allows("@public") and not s.allows("@chef-only")
    assert not s.allows("@public", ["intimate"])
    # unknown channel and a member-less channel are @public, not owner
    for cid in ("channel:nope", "channel:solo"):
        sc = resolve_reader(cid, res)
        assert sc.allows("@public") and not sc.allows("@community")


def test_exclusion_tags_filtered_through_store(store, monkeypatch, tmp_path):
    cfg = tmp_path / "aud.json"
    cfg.write_text(
        json.dumps(
            {
                "channels": {"c1": {"context_tag": "@public", "members": ["Pat"]}},
                "people": {"Pat": {"trust_level": 2, "never_share": ["intimate"]}},
            }
        )
    )
    monkeypatch.setattr("skmemory.audience._DEFAULT_CONFIG_PATH", cfg)
    store.primary.save(
        Memory(title="zebra a", content="zebra crossing", tags=["intimate"], context_tag="@public")
    )
    store.primary.save(
        Memory(title="zebra b", content="zebra crossing", tags=["ok"], context_tag="@public")
    )
    titles = {m.title for m in store.search("zebra", limit=5, reader="channel:c1")}
    assert titles == {"zebra b"}
    assert {m.title for m in store.search("zebra", limit=5)} == {"zebra a", "zebra b"}


@pytest.mark.parametrize("bad", [123, 1.5, ["@chef"], {"a": 1}, b"@chef", True])
def test_non_string_reader_is_public_not_raise(store, bad):
    scope = resolve_reader(bad)
    assert scope.allows("@public") and not scope.allows("@chef-only")
    assert not store.search("dentist appointment", limit=10, reader=bad)


@pytest.mark.parametrize("pad", ["@chef\n", " @chef ", "\t@chef-only\r\n"])
def test_whitespace_stripped_before_classifying(pad):
    assert resolve_reader(pad).unrestricted == resolve_reader("@chef").unrestricted is True
    assert resolve_reader("@public\n").allowed_tags == resolve_reader("@public").allowed_tags
    assert resolve_reader(" @work:a\n").allowed_tags == resolve_reader("@work:a").allowed_tags


def test_whitespace_env_reader(monkeypatch):
    monkeypatch.setenv(READER_ENV, "  @public\n")
    assert resolve_reader(None).allows("@public") and not resolve_reader(None).allows("@chef-only")
