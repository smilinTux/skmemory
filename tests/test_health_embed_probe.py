"""Health must fail the way the system fails.

On 2026-10-20 both configured embed endpoints were unusable (one host entirely
down, the other answering in 53s against a 15s timeout) while
`skmemory health` reported `"ok": true` and listed the dead URLs as though
naming one proved it worked. `search()` fell back to lexical matching and
returned five confident, wrong results for a query it could not serve.

A green that can be produced by absence is not a green. These tests lock in
that health actually CALLS the embedder and that a failed call is visible at
both the backend and the store level.
"""

from __future__ import annotations

import pytest

from skmemory.backends.pgvector_backend import PGVectorBackend, _probe_embed_enabled
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore


def _backend(**kw) -> PGVectorBackend:
    """A backend that never touches Postgres. probe_embed does not need it."""
    kw.setdefault("dsn", "postgresql://u:p@127.0.0.1:1/none")
    return PGVectorBackend(**kw)


# ---------------------------------------------------------------------------
# probe_embed
# ---------------------------------------------------------------------------


class TestProbeEmbedFn:
    def test_a_working_embed_fn_probes_ok(self):
        probe = _backend(embed_fn=lambda text: [0.1] * 1024).probe_embed()
        assert probe["ok"] is True
        assert probe["endpoint"] == "embed_fn"
        assert probe["latency_ms"] is not None

    def test_a_raising_embed_fn_is_reported_not_raised(self):
        def boom(text):
            raise RuntimeError("embedder exploded")

        probe = _backend(embed_fn=boom).probe_embed()
        assert probe["ok"] is False
        assert "embedder exploded" in probe["error"]

    def test_an_empty_vector_is_not_ok(self):
        probe = _backend(embed_fn=lambda text: []).probe_embed()
        assert probe["ok"] is False
        assert "empty or all-zero" in probe["error"]

    def test_an_all_zero_vector_is_not_ok(self):
        """The shape is right and the content is useless, which is the trap."""
        probe = _backend(embed_fn=lambda text: [0.0] * 1024).probe_embed()
        assert probe["ok"] is False
        assert "empty or all-zero" in probe["error"]


class TestProbeEmbedEndpoints:
    def test_no_endpoint_configured_is_not_ok(self):
        backend = _backend()
        backend.embed_urls = []
        probe = backend.probe_embed()
        assert probe["ok"] is False
        assert probe["error"] == "no embed endpoint configured"

    def test_it_walks_endpoints_in_order_and_names_the_one_that_answered(self, monkeypatch):
        backend = _backend()
        backend.embed_urls = ["http://dead.invalid/embed", "http://live.invalid/embed"]

        def fake_embed_one(url, text):
            return [0.2] * 8 if "live" in url else []

        monkeypatch.setattr(backend, "_embed_one", fake_embed_one)

        probe = backend.probe_embed()
        assert probe["ok"] is True
        assert probe["endpoint"] == "http://live.invalid/embed"
        # The dead one is recorded, so a silent failover is still visible.
        assert len(probe["tried"]) == 1
        assert probe["tried"][0]["endpoint"] == "http://dead.invalid/embed"

    def test_every_endpoint_failing_is_not_ok(self, monkeypatch):
        backend = _backend()
        backend.embed_urls = ["http://a.invalid/embed", "http://b.invalid/embed"]
        monkeypatch.setattr(backend, "_embed_one", lambda url, text: [])

        probe = backend.probe_embed()
        assert probe["ok"] is False
        assert probe["error"] == "every configured embed endpoint failed"
        assert len(probe["tried"]) == 2

    def test_a_raising_endpoint_is_recorded_and_the_walk_continues(self, monkeypatch):
        backend = _backend()
        backend.embed_urls = ["http://a.invalid/embed", "http://b.invalid/embed"]

        def fake_embed_one(url, text):
            if "a.invalid" in url:
                raise OSError("no route to host")
            return [0.3] * 8

        monkeypatch.setattr(backend, "_embed_one", fake_embed_one)

        probe = backend.probe_embed()
        assert probe["ok"] is True
        assert probe["endpoint"] == "http://b.invalid/embed"
        assert "no route to host" in probe["tried"][0]["error"]


class TestProbeToggle:
    def test_the_probe_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("SKMEMORY_HEALTH_PROBE_EMBED", raising=False)
        assert _probe_embed_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " off "])
    def test_it_can_be_turned_off(self, monkeypatch, raw):
        monkeypatch.setenv("SKMEMORY_HEALTH_PROBE_EMBED", raw)
        assert _probe_embed_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "anything-else"])
    def test_anything_else_leaves_it_on(self, monkeypatch, raw):
        monkeypatch.setenv("SKMEMORY_HEALTH_PROBE_EMBED", raw)
        assert _probe_embed_enabled() is True


# ---------------------------------------------------------------------------
# store.health()
# ---------------------------------------------------------------------------


class _FakeVector:
    """Only what MemoryStore.health() asks of a vector backend.

    `index_memory` has to succeed. Without it `snapshot()` records a real
    sync failure, which health() now (correctly) reports as `unindexed` and
    degrades on, so the fixture itself would make every status test fail for
    a reason that has nothing to do with the embedder.
    """

    def __init__(self, embed_ok, ok=None):
        self._embed_ok = embed_ok
        self._ok = (embed_ok is not False) if ok is None else ok

    def health_check(self) -> dict:
        return {"ok": self._ok, "embed_ok": self._embed_ok}

    def index_memory(self, *args, **kwargs):
        return True

    def save(self, *args, **kwargs):
        return True

    def remove_memory(self, *args, **kwargs):
        return True


def _store(tmp_path, embed_ok, ok=None):
    backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
    store = MemoryStore(primary=backend, vector=_FakeVector(embed_ok, ok))
    store.snapshot(title="hi", content="hello world, all good here")
    return store


class TestStoreStatusReflectsTheEmbedder:
    def test_a_dead_embedder_degrades_an_otherwise_clean_store(self, tmp_path):
        """Every count clean, semantic recall gone. That is not `ok`."""
        status = _store(tmp_path, False).health()
        assert status["embed_ok"] is False
        assert status["status"] == "degraded"
        assert status["sqlite_only"] == 0
        assert status["flat_only"] == 0

    def test_a_live_embedder_leaves_a_clean_store_ok(self, tmp_path):
        status = _store(tmp_path, True).health()
        assert status["embed_ok"] is True
        assert status["status"] == "ok"

    def test_an_unprobed_embedder_is_not_treated_as_broken(self, tmp_path):
        """None means `not probed`. Only an explicit False degrades."""
        status = _store(tmp_path, None).health()
        assert status["embed_ok"] is None
        assert status["status"] == "ok"

    def test_embed_ok_is_always_present_even_without_a_vector_backend(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        status = store.health()
        assert "embed_ok" in status
        assert status["embed_ok"] is None


class TestStoreStatusReflectsTheVectorBackend:
    """A wired backend reporting ok:false used to leave the store green."""

    def test_a_failed_vector_backend_degrades_the_store(self, tmp_path):
        """Observed on two agents whose derived store answered 'Not initialized'."""
        status = _store(tmp_path, None, ok=False).health()
        assert status["vector_ok"] is False
        assert status["status"] == "degraded"

    def test_a_healthy_vector_backend_leaves_the_store_ok(self, tmp_path):
        status = _store(tmp_path, True, ok=True).health()
        assert status["vector_ok"] is True
        assert status["status"] == "ok"

    def test_vector_ok_is_none_without_a_vector_backend(self, tmp_path):
        backend = SQLiteBackend(base_path=str(tmp_path / "memories"))
        store = MemoryStore(primary=backend)
        store.snapshot(title="hi", content="hello world, all good here")

        status = store.health()
        assert status["vector_ok"] is None
        assert status["status"] == "ok"
