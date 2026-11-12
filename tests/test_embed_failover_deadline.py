"""A wedged primary embed endpoint must not eat the search deadline (card cd4ac109).

On the live log the semantic arm caused 118 of 119 search timeouts: the primary
embed endpoint accepted the connection and then never answered, the read timeout
(15 s) was far longer than the 4 s search deadline, so failover to the healthy
secondary never happened in time. These tests use fake endpoints only.
"""

from __future__ import annotations

import time

import httpx

from skmemory.backends.pgvector_backend import PGVectorBackend

WEDGE_MAX_S = 3.0  # a fake wedge never really sleeps longer than this


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"embeddings": [[0.1] * 1024], "model": "mxbai-embed-large"}


def _fake_post(calls):
    def post(url, json=None, timeout=None):
        calls.append(url)
        if "wedged" in url:
            # accepts the connection, never answers: only the read timeout ends it
            time.sleep(min(float(timeout.read), WEDGE_MAX_S))
            raise httpx.ReadTimeout("wedged")
        return _Resp()

    return post


def _backend():
    return PGVectorBackend(
        dsn="postgresql://u:p@127.0.0.1:1/none",
        embed_urls=["http://wedged.invalid/api/embed", "http://fast.invalid/api/embed"],
        embed_timeout=15.0,
    )


def test_wedged_primary_does_not_eat_the_search_deadline(monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "1.0")
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", _fake_post(calls))
    be = _backend()
    t0 = time.monotonic()
    vec = be._embed("a query")
    elapsed = time.monotonic() - t0
    assert vec and any(vec), "the healthy secondary must answer"
    assert elapsed < 1.0, f"embed took {elapsed:.2f}s against a 1.0s search deadline"
    assert be.last_embed_index() == 1


def test_failed_primary_is_demoted_for_the_next_query(monkeypatch):
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "1.0")
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", _fake_post(calls))
    be = _backend()
    be._embed("first")
    calls.clear()
    t0 = time.monotonic()
    assert be._embed("second")
    assert time.monotonic() - t0 < 0.2
    assert calls == ["http://fast.invalid/api/embed"], "wedged endpoint must be tried last"
    assert be.last_embed_index() == 1


def test_demoted_endpoint_is_still_tried_last_when_the_rest_fail(monkeypatch):
    """Demotion reorders, never removes: a lone endpoint is still used."""
    monkeypatch.setattr(httpx, "post", _fake_post([]))
    be = PGVectorBackend(
        dsn="postgresql://u:p@127.0.0.1:1/none",
        embed_urls=["http://fast.invalid/api/embed"],
    )
    be._mark_embed_bad(0)
    assert be._embed("q") and be.last_embed_index() == 0


def test_write_path_keeps_the_full_timeout(monkeypatch):
    """required=True (saves) is not bound by the search deadline."""
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "1.0")
    seen: list[float] = []

    def post(url, json=None, timeout=None):
        seen.append(float(timeout.read))
        return _Resp()

    monkeypatch.setattr(httpx, "post", post)
    _backend()._embed("memory text", required=True)
    assert seen == [15.0]
