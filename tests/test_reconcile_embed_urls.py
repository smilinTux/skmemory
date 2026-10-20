"""The reconcile path must obey the same embed knobs as the read path.

`reconcile.py` read a bare `EMBED_URL` and defaulted to a hardcoded
`http://192.168.0.100:11434/api/embed`. That is both a DIFFERENT knob from the
one the rest of the package documents (`SKMEMORY_EMBED_URL` /
`SKMEMORY_EMBED_URLS`) and, since that host went down, an address that cannot
answer. A node could set the documented variables, see them reflected in
`skmemory health`, and still send every reconcile embed call into a black hole.

It also had no failover: one URL, four retries against that same URL, then
give up. The read path has walked a list for a while; the write path did not.
"""

from __future__ import annotations

import importlib

import pytest

import skmemory.reconcile as reconcile

ALL_VARS = ("SKMEMORY_EMBED_URLS", "SKMEMORY_EMBED_URL", "EMBED_URL")


def _resolve(monkeypatch, **env) -> list[str]:
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return reconcile._resolve_embed_urls()


class TestPrecedence:
    def test_urls_list_wins_and_keeps_order(self, monkeypatch):
        urls = _resolve(
            monkeypatch,
            SKMEMORY_EMBED_URLS="http://a/embed,http://b/embed",
            SKMEMORY_EMBED_URL="http://ignored/embed",
            EMBED_URL="http://also-ignored/embed",
        )
        assert urls == ["http://a/embed", "http://b/embed"]

    def test_namespaced_single_url_is_used_when_the_list_is_unset(self, monkeypatch):
        urls = _resolve(
            monkeypatch,
            SKMEMORY_EMBED_URL="http://only/embed",
            EMBED_URL="http://legacy/embed",
        )
        assert urls == ["http://only/embed"]

    def test_legacy_embed_url_still_works(self, monkeypatch):
        """Existing unit files and cron entries set this; do not break them."""
        assert _resolve(monkeypatch, EMBED_URL="http://legacy/embed") == ["http://legacy/embed"]

    def test_the_default_is_node_local_not_a_remote_host(self, monkeypatch):
        """The old default pointed at a specific LAN box that is now down."""
        urls = _resolve(monkeypatch)
        assert urls == ["http://localhost:11434/api/embed"]
        assert not any("192.168." in u for u in urls)


class TestParsing:
    def test_blank_entries_and_whitespace_are_dropped(self, monkeypatch):
        urls = _resolve(monkeypatch, SKMEMORY_EMBED_URLS=" http://a/embed , , http://b/embed ,")
        assert urls == ["http://a/embed", "http://b/embed"]

    def test_duplicates_collapse_but_order_survives(self, monkeypatch):
        urls = _resolve(
            monkeypatch,
            SKMEMORY_EMBED_URLS="http://a/embed,http://b/embed,http://a/embed",
        )
        assert urls == ["http://a/embed", "http://b/embed"]

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , "])
    def test_an_empty_list_falls_through_to_the_next_source(self, monkeypatch, raw):
        urls = _resolve(
            monkeypatch, SKMEMORY_EMBED_URLS=raw, SKMEMORY_EMBED_URL="http://next/embed"
        )
        assert urls == ["http://next/embed"]


class TestModuleDefaults:
    def test_default_embed_url_is_the_first_of_the_list(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_EMBED_URLS", "http://first/embed,http://second/embed")
        for var in ("SKMEMORY_EMBED_URL", "EMBED_URL"):
            monkeypatch.delenv(var, raising=False)
        reloaded = importlib.reload(reconcile)
        try:
            assert reloaded.DEFAULT_EMBED_URLS == ["http://first/embed", "http://second/embed"]
            assert reloaded.DEFAULT_EMBED_URL == "http://first/embed"
        finally:
            # Leave the module as the rest of the suite expects to find it.
            for var in ALL_VARS:
                monkeypatch.delenv(var, raising=False)
            importlib.reload(reconcile)
