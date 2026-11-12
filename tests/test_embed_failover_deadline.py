"""A wedged primary embed endpoint must not eat the search deadline (card cd4ac109).

On the live log the semantic arm caused 118 of 119 search timeouts: the primary
embed endpoint accepted the connection and then never answered, the read timeout
(15 s) was far longer than the 4 s search deadline, so failover to the healthy
secondary never happened in time. These tests use fake endpoints only.
"""

from __future__ import annotations

import contextlib
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


def test_slices_are_computed_from_the_remaining_deadline(monkeypatch):
    """3 endpoints: each non-last gets 60 percent of what REMAINS when it is tried."""
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "4.0")
    seen: list[float] = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def post(url, json=None, timeout=None):
        seen.append(round(float(timeout.read), 3))
        if "last" in url:
            return _Resp()
        clock["t"] += float(timeout.read)  # the endpoint burns its whole slice
        raise httpx.ReadTimeout("wedged")

    monkeypatch.setattr(httpx, "post", post)
    be = PGVectorBackend(
        dsn="postgresql://u:p@127.0.0.1:1/none",
        embed_urls=["http://a.invalid/x", "http://b.invalid/x", "http://last.invalid/x"],
        embed_timeout=15.0,
    )
    assert be._embed("q")
    # a: 0.6*4.0 = 2.4 ; b: 0.6*(4.0-2.4) = 0.96 ; last: all that remains, 0.64
    assert seen == [2.4, 0.96, 0.64]


def test_primary_slice_clears_the_measured_p99(monkeypatch):
    """Primary slice at the default 4 s deadline is 2.4 s, above the 2.151 s p99."""
    monkeypatch.delenv("SKMEMORY_SEARCH_DEADLINE_S", raising=False)
    seen: list[float] = []

    def post(url, json=None, timeout=None):
        seen.append(float(timeout.read))
        return _Resp()

    monkeypatch.setattr(httpx, "post", post)
    _backend()._embed("q")
    assert 2.151 < seen[0] <= 2.4 + 1e-6


def test_over_context_retries_share_one_slice(monkeypatch):
    """Repeated 400s on a sliced endpoint cannot stretch past the slice."""
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "1.0")
    reads: list[float] = []

    class _R400:
        status_code = 400

    def post(url, json=None, timeout=None):
        if "wedged" in url:
            reads.append(float(timeout.read))
            time.sleep(0.2)
            raise httpx.HTTPStatusError("400", request=None, response=_R400())
        return _Resp()

    monkeypatch.setattr(httpx, "post", post)
    be = _backend()
    t0 = time.monotonic()
    assert be._embed("x" * 1400)
    assert time.monotonic() - t0 < 1.0
    assert reads and reads[0] <= 0.6 + 1e-6
    assert len(reads) >= 2 and reads[-1] < reads[0], "retries must not get fresh time"


def test_in_flight_index_visible_from_another_thread_while_wedged(monkeypatch):
    import threading

    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "4.0")
    started = threading.Event()
    release = threading.Event()

    def post(url, json=None, timeout=None):
        started.set()
        release.wait(5)
        raise httpx.ReadTimeout("wedged")

    monkeypatch.setattr(httpx, "post", post)
    be = PGVectorBackend(
        dsn="postgresql://u:p@127.0.0.1:1/none",
        embed_urls=["http://wedged.invalid/x"],
    )
    tid = {}

    def work():
        tid["t"] = threading.get_ident()
        with contextlib.suppress(Exception):
            be._embed("q")

    th = threading.Thread(target=work)
    th.start()
    assert started.wait(5)
    assert be.embed_inflight_index(tid["t"]) == 0
    release.set()
    th.join(5)
    assert be.embed_inflight_index(tid["t"]) is None


def test_timeout_row_carries_embed_ep_inflight(tmp_path, monkeypatch):
    """A semantic arm that times out on a wedged endpoint logs which endpoint."""
    import json as _json

    from skmemory import retrieval_log as rl
    from skmemory.backends.sqlite_backend import SQLiteBackend
    from skmemory.models import MemoryLayer
    from skmemory.store import MemoryStore

    monkeypatch.setenv("SKMEMORY_SEARCH_FUSION", "on")
    monkeypatch.setenv("SKMEMORY_SEARCH_DEADLINE_S", "0.5")
    release = __import__("threading").Event()

    def post(url, json=None, timeout=None):
        release.wait(3)  # wedged past the deadline, never answers in time
        raise httpx.ReadTimeout("wedged")

    monkeypatch.setattr(httpx, "post", post)
    be = PGVectorBackend(
        dsn="postgresql://u:p@127.0.0.1:1/none",
        embed_urls=["http://wedged.invalid/x"],
    )

    class _Vec:
        search_semantic = None

        def __init__(self, inner):
            self._inner = inner
            self.last_embed_index = inner.last_embed_index
            self.embed_inflight_index = inner.embed_inflight_index

        def search_semantic(self, query, **kw):  # noqa: F811
            self._inner._embed(query)
            return []

    st = MemoryStore(primary=SQLiteBackend(str(tmp_path / "mem")))
    st.vector = _Vec(be)
    st.snapshot(title="alpha", content="zebra giraffe", layer=MemoryLayer.SHORT)
    st.search_fused("zebra", limit=5)
    release.set()
    rl.flush()
    p = tmp_path / "logs" / rl.LOG_NAME
    rows = [_json.loads(x) for x in p.read_text().splitlines()]
    row = rows[-1]
    assert row["arms"]["semantic"]["status"] == "timeout"
    assert row["embed_ep_inflight"] == 0
    assert "embed_ep" not in row
