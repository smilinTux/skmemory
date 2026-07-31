"""Guards for the skmem-pg write-path DSN invariant (prb-6f069c5e).

skmem-pg is a LOCAL, per-node writable Postgres. The default DSN must resolve to
a local writable port, never the retired :5433 standby and never a hardcoded
remote host. A node that constructs the backend without an explicit override must
NOT end up writing to a read-only replica (where every save()/delete() raises).
"""

import importlib

import pytest

# The pgvector backend needs psycopg (optional pg extra). A bare CI runner that
# installs only the base deps lacks it, so skip the whole module there; the
# backend itself lazy-imports psycopg so production is unaffected.
pytest.importorskip("psycopg")


def _reload_backend(monkeypatch, env_value):
    """Reimport the backend module with SKMEMORY_PG_DSN set/unset so the
    import-time DEFAULT_DSN is recomputed."""
    if env_value is None:
        monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)
    else:
        monkeypatch.setenv("SKMEMORY_PG_DSN", env_value)
    import skmemory.backends.pgvector_backend as mod

    return importlib.reload(mod)


def test_default_dsn_is_local_writable_port(monkeypatch):
    """With no env override the default targets the local writable port :5432."""
    mod = _reload_backend(monkeypatch, None)
    assert "localhost:5432" in mod.DEFAULT_DSN


def test_default_dsn_never_standby_or_remote(monkeypatch):
    """The write-path default must never be the retired standby port or a
    hardcoded remote host (that reintroduces the SPOF / read-only-replica bug)."""
    mod = _reload_backend(monkeypatch, None)
    assert ":5433" not in mod.DEFAULT_DSN, "retired standby port leaked into default"
    assert "192.168." not in mod.DEFAULT_DSN, "hardcoded remote host in write-path default"


def test_env_overrides_default_dsn(monkeypatch):
    """SKMEMORY_PG_DSN (the node-local lever) drives the default when set."""
    mod = _reload_backend(monkeypatch, "postgresql://postgres:x@localhost:5433/skmemory")
    assert mod.DEFAULT_DSN.endswith("localhost:5433/skmemory")


def test_backend_uses_explicit_dsn_over_default(monkeypatch):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(dsn="postgresql://postgres:x@localhost:5433/skmemory", agent="jarvis")
    assert be.dsn.endswith("localhost:5433/skmemory")
    assert be.agent == "jarvis"


def test_backend_default_dsn_is_local(monkeypatch):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina")
    assert "localhost:5432" in be.dsn


# --- Gap A: forget() must delete the pgvector row immediately (card 23a722ca) ---
#
# MemoryStore.forget() calls self.vector.remove(memory_id). PGVectorBackend used
# to expose only delete() (no remove()), so on the default (pgvector) deployment
# forget() raised AttributeError, which store.py swallowed — the pg row survived
# until the daily reconcile prune. remove() now cascades the delete like the
# other vector backends. These run without a live DB by faking the connection.


class _FakeCursor:
    """Records executed SQL and returns a scripted rowcount per execute()."""

    def __init__(self, rowcounts):
        self._rowcounts = list(rowcounts)
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor


def _backend_with_cursor(monkeypatch, rowcounts):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina")
    cur = _FakeCursor(rowcounts)
    be._conn = _FakeConn(cur)  # _connection() returns this (not closed) as-is
    return be, cur


def test_pgvector_backend_exposes_remove(monkeypatch):
    """forget() calls vector.remove(); the pgvector backend must have it.

    Fail-before: PGVectorBackend had no remove() (only delete()), so this
    attribute was missing and forget() raised AttributeError.
    """
    mod = _reload_backend(monkeypatch, None)
    assert callable(getattr(mod.PGVectorBackend, "remove", None))


def test_remove_deletes_row_and_cascades_chunks(monkeypatch):
    """remove() deletes the memory's own row (scoped id+agent) AND cascades to
    child/chunk rows via memory_json.parent_id — matching the other backends."""
    be, cur = _backend_with_cursor(monkeypatch, rowcounts=[1, 2])
    result = be.remove("mem-123")

    assert result is True
    # main-row delete: scoped to id + agent
    assert any(
        "DELETE FROM memories" in sql and "id=%s" in sql and params == ("mem-123", "lumina")
        for sql, params in cur.executed
    ), f"no scoped main-row delete in {cur.executed}"
    # chunk/child cascade: delete rows whose memory_json.parent_id == id
    assert any(
        "parent_id" in sql and params == ("mem-123", "lumina") for sql, params in cur.executed
    ), f"no parent_id cascade delete in {cur.executed}"


def test_remove_returns_false_when_nothing_deleted(monkeypatch):
    """No matching row and no children -> nothing removed -> False."""
    be, _cur = _backend_with_cursor(monkeypatch, rowcounts=[0, 0])
    assert be.remove("ghost") is False


def test_remove_true_when_only_children_deleted(monkeypatch):
    """A child/chunk cascade alone still counts as a removal."""
    be, _cur = _backend_with_cursor(monkeypatch, rowcounts=[0, 3])
    assert be.remove("parent-only") is True


# --- Card 41ec2201: pin + verify embedding model identity across endpoints ------
#
# Fleet standard is mxbai-embed-large (1024-dim) everywhere. If an endpoint silently
# serves a DIFFERENT model, or a redeploy swaps it, the returned vectors become
# incompatible with the stored ones and recall corrupts with NO error. The backend
# now verifies the embedding's dimension (and the served model name when reported)
# on every embed, raising EmbeddingModelMismatch instead of poisoning the store.


def _backend_with_embed_fn(monkeypatch, embed_fn, **kwargs):
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina", embed_fn=embed_fn, **kwargs)
    return mod, be


def test_matching_dimension_passes(monkeypatch):
    """A vector whose length equals the pinned vector_dim is returned unchanged."""
    vec = [0.1] * 1024
    _mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: list(vec))
    assert be._embed("hello") == vec


def test_mismatched_dimension_raises(monkeypatch):
    """A wrong-dimension vector (e.g. a swapped 384-dim model) fails loudly."""
    mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [0.1] * 384)
    with pytest.raises(mod.EmbeddingModelMismatch) as exc:
        be._embed("hello")
    msg = str(exc.value)
    assert "1024" in msg and "384" in msg  # names expected-vs-actual dimension


def test_save_fails_loudly_on_dimension_mismatch(monkeypatch):
    """The mismatch surfaces at write time (save) BEFORE any row is stored,
    so a swapped model can never silently poison the store."""
    from skmemory.models import Memory

    mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [0.1] * 512)
    mem = Memory(title="t", content="c")
    with pytest.raises(mod.EmbeddingModelMismatch):
        be.save(mem)  # raises in _embed, before _connection() is touched


def test_empty_vector_passes_through(monkeypatch):
    """An empty vector means the embed call failed upstream; that stays a graceful
    [] (handled elsewhere), it must NOT be turned into a mismatch error."""
    _mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [])
    assert be._embed("hello") == []


def test_verify_can_be_disabled(monkeypatch):
    """SKMEMORY_EMBED_VERIFY=0 / verify_embedding=False is an ops escape hatch:
    a mismatched vector is returned unchecked."""
    _mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [0.1] * 384, verify_embedding=False)
    assert be._embed("hello") == [0.1] * 384


def test_model_name_mismatch_raises(monkeypatch):
    """When the endpoint reports serving a genuinely different model, raise even if
    the dimension happens to match."""
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina", embed_model="mxbai-embed-large")
    with pytest.raises(mod.EmbeddingModelMismatch) as exc:
        be._verify_embedding([0.1] * 1024, served_model="nomic-embed-text")
    assert "nomic-embed-text" in str(exc.value)


def test_model_name_tolerant_match(monkeypatch):
    """Ollama tags, org prefixes, and version suffixes for the SAME model must not
    trip the guard (mxbai-embed-large ~ mxbai-embed-large:latest ~
    mixedbread-ai/mxbai-embed-large-v1)."""
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina", embed_model="mxbai-embed-large")
    for served in (
        "mxbai-embed-large",
        "mxbai-embed-large:latest",
        "mixedbread-ai/mxbai-embed-large-v1",
    ):
        assert be._verify_embedding([0.1] * 1024, served_model=served) == [0.1] * 1024


def test_env_disables_verification(monkeypatch):
    """The module-level default honors SKMEMORY_EMBED_VERIFY at import time."""
    monkeypatch.setenv("SKMEMORY_EMBED_VERIFY", "0")
    mod = _reload_backend(monkeypatch, None)
    assert mod.DEFAULT_EMBED_VERIFY is False
    be = mod.PGVectorBackend(agent="lumina", embed_fn=lambda _t: [0.1] * 384)
    assert be._embed("hello") == [0.1] * 384


# --- Card d3498d86: embed-endpoint failover + fail-loud NULL-embedding handling -
#
# The embed server sits on a GPU box with documented VRAM flapping. Before this,
# PGVectorBackend took a SINGLE embed_url and, when it was down / returned empty,
# _embed() returned [] and save() stored that as a NULL vector (an unsearchable
# row that silently poisons recall). Now: (1) SKMEMORY_EMBED_URLS lets a node fail
# over to a secondary endpoint, and (2) the WRITE path raises EmbeddingUnavailable
# rather than store a NULL/empty/zero vector. Query paths still degrade to text.


class _FakeResp:
    """Minimal httpx.Response stand-in for the embed HTTP call."""

    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


def _fake_post(behaviors):
    """Build a fake ``httpx.post`` that dispatches on URL.

    ``behaviors`` maps url -> either an Exception (raised, simulating a down
    endpoint) or a dict (returned as the JSON body of a 200). An unlisted url
    raises ConnectError. Records the URLs called, in order.
    """
    import httpx

    calls = []

    def post(url, json=None, timeout=None):  # noqa: A002 - mirror httpx signature
        calls.append(url)
        b = behaviors.get(url)
        if b is None:
            raise httpx.ConnectError(f"no route to {url}")
        if isinstance(b, Exception):
            raise b
        return _FakeResp(b)

    return post, calls


def _ok_body(dim=1024, model="mxbai-embed-large"):
    return {"embeddings": [[0.1] * dim], "model": model}


PRIMARY = "http://primary:11434/api/embed"
SECONDARY = "http://secondary:11434/api/embed"


def _http_backend(monkeypatch, embed_urls, behaviors):
    """A backend wired to real _embed HTTP path with httpx.post faked."""
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina", embed_urls=embed_urls)
    post, calls = _fake_post(behaviors)
    monkeypatch.setattr("httpx.post", post)
    return mod, be, calls


def test_failover_to_secondary_when_primary_down(monkeypatch):
    """Primary endpoint is down -> _embed fails over to the secondary and returns
    its vector. Fail-before: only one embed_url existed, so a down primary meant
    an empty vector with no second chance."""
    import httpx

    _mod, be, calls = _http_backend(
        monkeypatch,
        [PRIMARY, SECONDARY],
        {PRIMARY: httpx.ConnectError("down"), SECONDARY: _ok_body()},
    )
    vec = be._embed("hello", required=True)
    assert vec == [0.1] * 1024
    assert calls == [PRIMARY, SECONDARY]  # tried primary, then failed over


def test_all_endpoints_down_raises_on_write(monkeypatch):
    """Every endpoint down on the WRITE path -> EmbeddingUnavailable, nothing
    stored. Fail-before: _embed returned [] and save stored a NULL vector."""
    import httpx

    from skmemory.models import Memory

    mod, be, _calls = _http_backend(
        monkeypatch,
        [PRIMARY, SECONDARY],
        {PRIMARY: httpx.ConnectError("down"), SECONDARY: httpx.ConnectError("down")},
    )
    # If _embed did not raise first, save() would touch _connection(); make that
    # explode so a regression (storing NULL) can never pass silently.
    monkeypatch.setattr(
        be, "_connection", lambda: (_ for _ in ()).throw(AssertionError("row was written!"))
    )
    with pytest.raises(mod.EmbeddingUnavailable):
        be.save(Memory(title="t", content="c"))


def test_all_endpoints_down_query_stays_graceful(monkeypatch):
    """The READ path tolerates a total embed outage: _embed(required=False)
    returns [] so search can degrade to BM25/text instead of raising."""
    import httpx

    _mod, be, calls = _http_backend(
        monkeypatch,
        [PRIMARY, SECONDARY],
        {PRIMARY: httpx.ConnectError("down"), SECONDARY: httpx.ConnectError("down")},
    )
    assert be._embed("query text") == []  # required defaults False
    assert calls == [PRIMARY, SECONDARY]  # both endpoints were attempted


def test_save_raises_on_empty_embedding(monkeypatch):
    """An empty embedding on the write path raises and stores nothing (never a
    NULL vector). Fail-before: save() did `self._embed(...) or None` -> NULL."""
    from skmemory.models import Memory

    mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [])
    monkeypatch.setattr(
        be, "_connection", lambda: (_ for _ in ()).throw(AssertionError("row was written!"))
    )
    with pytest.raises(mod.EmbeddingUnavailable):
        be.save(Memory(title="t", content="c"))


def test_save_raises_on_all_zero_embedding(monkeypatch):
    """An all-zero vector is degenerate (matches nothing under cosine); it must
    fail loudly on write rather than be stored as a dead row."""
    from skmemory.models import Memory

    mod, be = _backend_with_embed_fn(monkeypatch, lambda _t: [0.0] * 1024)
    monkeypatch.setattr(
        be, "_connection", lambda: (_ for _ in ()).throw(AssertionError("row was written!"))
    )
    with pytest.raises(mod.EmbeddingUnavailable):
        be.save(Memory(title="t", content="c"))


def test_happy_path_save_stores_the_vector(monkeypatch):
    """A good embedding is stored as the embedding column value (not NULL), and
    only the primary endpoint is contacted when it succeeds."""
    from skmemory.models import Memory

    _mod, be, calls = _http_backend(monkeypatch, [PRIMARY, SECONDARY], {PRIMARY: _ok_body()})
    cur = _FakeCursor([1])
    be._conn = _FakeConn(cur)
    be.save(Memory(title="t", content="c"))
    assert calls == [PRIMARY]  # secondary never needed
    # the INSERT carried the real 1024-dim vector as its last param, not None
    insert = next((p for sql, p in cur.executed if "INSERT INTO memories" in sql), None)
    assert insert is not None
    assert insert[-1] == [0.1] * 1024


def test_model_mismatch_does_not_failover(monkeypatch):
    """A pinned-model mismatch is a config error identical on every endpoint, so
    it raises immediately without trying the secondary (failover cannot help)."""
    mod, be, calls = _http_backend(
        monkeypatch,
        [PRIMARY, SECONDARY],
        {PRIMARY: _ok_body(dim=384), SECONDARY: _ok_body()},
    )
    with pytest.raises(mod.EmbeddingModelMismatch):
        be._embed("hello", required=True)
    assert calls == [PRIMARY]  # did NOT fall through to secondary


def test_embed_urls_from_env(monkeypatch):
    """SKMEMORY_EMBED_URLS (comma-separated) drives the ordered endpoint list at
    import time; the primary is embed_url, blanks/dupes are collapsed in order."""
    monkeypatch.setenv("SKMEMORY_EMBED_URLS", f"{PRIMARY}, {SECONDARY} ,,{PRIMARY}")
    mod = _reload_backend(monkeypatch, None)
    assert mod.DEFAULT_EMBED_URLS == [PRIMARY, SECONDARY]
    be = mod.PGVectorBackend(agent="lumina")
    assert be.embed_urls == [PRIMARY, SECONDARY]
    assert be.embed_url == PRIMARY  # primary stays the reported/back-compat url


def test_single_url_default_unchanged(monkeypatch):
    """With nothing configured a node still has exactly one endpoint (today's
    behavior): default embed_urls == [default embed_url]."""
    monkeypatch.delenv("SKMEMORY_EMBED_URLS", raising=False)
    monkeypatch.delenv("SKMEMORY_EMBED_URL", raising=False)
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina")
    assert be.embed_urls == [mod.DEFAULT_EMBED_URL]
    assert be.embed_url == mod.DEFAULT_EMBED_URL


# --- Embed-endpoint timeout hardening (GPU-outage resilience) -------------------
#
# The embed server sits on a GPU box whose driver/VRAM can flap ("Driver/library
# version mismatch" wedges the process). The per-endpoint call was hardcoded to a
# 60s timeout, so during an outage a save could hang 60s * N-endpoints -> minutes.
# The timeout is now short and configurable (SKMEMORY_EMBED_TIMEOUT /
# SKMEMORY_EMBED_CONNECT_TIMEOUT) so a wedged backend is abandoned fast and
# failover proceeds. These tests pin the default, the env overrides, garbage-value
# fallback, and that the configured timeout is actually applied to the HTTP call.


def _fake_post_recording_timeout(behaviors):
    """Like ``_fake_post`` but also records the ``timeout`` passed to each call.

    Returns (post, timeouts) where ``timeouts`` is the list of timeout objects
    handed to httpx.post, in call order — so a test can assert the wedged-backend
    cap is actually applied instead of the old hardcoded 60s.
    """
    import httpx

    timeouts = []

    def post(url, json=None, timeout=None):  # noqa: A002 - mirror httpx signature
        timeouts.append(timeout)
        b = behaviors.get(url)
        if b is None:
            raise httpx.ConnectError(f"no route to {url}")
        if isinstance(b, Exception):
            raise b
        return _FakeResp(b)

    return post, timeouts


def test_default_embed_timeout_is_short(monkeypatch):
    """With nothing configured the per-endpoint timeout is a short bound (<= 30s),
    not the old 60s that let a wedged backend hang for minutes across failover."""
    monkeypatch.delenv("SKMEMORY_EMBED_TIMEOUT", raising=False)
    monkeypatch.delenv("SKMEMORY_EMBED_CONNECT_TIMEOUT", raising=False)
    mod = _reload_backend(monkeypatch, None)
    assert 0 < mod.DEFAULT_EMBED_TIMEOUT <= 30
    be = mod.PGVectorBackend(agent="lumina")
    assert 0 < be.embed_timeout <= 30
    # connect cap is no larger than the overall timeout and stays > 0
    assert 0 < be.embed_connect_timeout <= be.embed_timeout


def test_embed_timeout_from_env(monkeypatch):
    """SKMEMORY_EMBED_TIMEOUT / SKMEMORY_EMBED_CONNECT_TIMEOUT drive the values."""
    monkeypatch.setenv("SKMEMORY_EMBED_TIMEOUT", "8")
    monkeypatch.setenv("SKMEMORY_EMBED_CONNECT_TIMEOUT", "2")
    mod = _reload_backend(monkeypatch, None)
    assert mod.DEFAULT_EMBED_TIMEOUT == 8.0
    assert mod.DEFAULT_EMBED_CONNECT_TIMEOUT == 2.0
    be = mod.PGVectorBackend(agent="lumina")
    assert be.embed_timeout == 8.0
    assert be.embed_connect_timeout == 2.0


def test_garbage_timeout_env_falls_back_to_default(monkeypatch):
    """A non-numeric or non-positive env value must NOT become 0 (httpx "fail
    now") or crash import; it falls back to the safe default."""
    for bad in ("", "  ", "abc", "0", "-5"):
        monkeypatch.setenv("SKMEMORY_EMBED_TIMEOUT", bad)
        mod = _reload_backend(monkeypatch, None)
        assert mod.DEFAULT_EMBED_TIMEOUT == 15.0, f"{bad!r} should fall back"


def test_explicit_timeout_arg_overrides(monkeypatch):
    """The constructor arg wins over the env-derived default, and a bad value is
    coerced to a safe positive timeout."""
    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(agent="lumina", embed_timeout=3.0, embed_connect_timeout=1.0)
    assert be.embed_timeout == 3.0
    assert be.embed_connect_timeout == 1.0
    # 0 / negative is rejected in favor of a safe positive value (never httpx "fail now")
    be2 = mod.PGVectorBackend(agent="lumina", embed_timeout=0, embed_connect_timeout=-1)
    assert be2.embed_timeout > 0
    assert be2.embed_connect_timeout > 0


def test_configured_timeout_is_applied_to_http_call(monkeypatch):
    """The configured (short) timeout is actually passed to httpx.post, so a
    wedged backend is dropped at that bound rather than the old hardcoded 60s."""
    import httpx

    mod = _reload_backend(monkeypatch, None)
    be = mod.PGVectorBackend(
        agent="lumina", embed_urls=[PRIMARY], embed_timeout=7.0, embed_connect_timeout=2.0
    )
    post, timeouts = _fake_post_recording_timeout({PRIMARY: _ok_body()})
    monkeypatch.setattr("httpx.post", post)

    be._embed("hello", required=True)

    assert len(timeouts) == 1
    t = timeouts[0]
    assert isinstance(t, httpx.Timeout)
    # httpx.Timeout exposes per-phase attrs; connect is the tight cap, read the overall
    assert t.connect == 2.0
    assert t.read == 7.0


def test_wedged_primary_times_out_then_fails_over(monkeypatch):
    """A wedged primary (raises ReadTimeout at its bound) is abandoned and _embed
    fails over to the secondary — the whole point of the short timeout."""
    import httpx

    _mod, be, calls = _http_backend(
        monkeypatch,
        [PRIMARY, SECONDARY],
        {PRIMARY: httpx.ReadTimeout("wedged GPU box"), SECONDARY: _ok_body()},
    )
    vec = be._embed("hello", required=True)
    assert vec == [0.1] * 1024
    assert calls == [PRIMARY, SECONDARY]


# Restore a clean module state for any later test in the session.
@pytest.fixture(autouse=True, scope="module")
def _restore_module():
    yield
    import skmemory.backends.pgvector_backend as mod

    importlib.reload(mod)
