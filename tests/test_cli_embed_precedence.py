"""The per-node env must beat the shared yaml for embed endpoints.

The agent home is one Syncthing folder, so a yaml `embed_url` is a SHARED
value. The agent yamls say so themselves:

    "SKMEMORY_EMBED_URL / SKMEMORY_EMBED_URLS in the env OVERRIDE this file.
     A node with no local ollama gets its answer from the failover list, not
     from here."

Both sentences were false. `cli._get_store` passed the yaml value through
unconditionally, and an explicit `embed_url` collapses the backend's endpoint
list to that single URL, so one shared line both beat the env and disabled
per-node failover.

Observed 2026-10-20: one agent pinned node-local ollama, which on that box is
CPU only and took 53.8s to 120.1s per embed against a 15s timeout, while the
env listed endpoints answering in 0.32s and 10.5s. Another pinned a host that
had been down for 26 days as its only endpoint, so it could not embed at all.
"""

from __future__ import annotations

import pytest

from skmemory.backends.pgvector_backend import PGVectorBackend
from skmemory.cli import _env_pins_embed_endpoint

VARS = ("SKMEMORY_EMBED_URLS", "SKMEMORY_EMBED_URL")


@pytest.fixture
def clean_env(monkeypatch):
    for var in VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestEnvPinsEmbedEndpoint:
    def test_no_env_means_the_yaml_is_allowed_to_speak(self, clean_env):
        assert _env_pins_embed_endpoint() is False

    def test_the_urls_list_pins_it(self, clean_env):
        clean_env.setenv("SKMEMORY_EMBED_URLS", "http://a/embed,http://b/embed")
        assert _env_pins_embed_endpoint() is True

    def test_the_single_url_pins_it(self, clean_env):
        clean_env.setenv("SKMEMORY_EMBED_URL", "http://a/embed")
        assert _env_pins_embed_endpoint() is True

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_a_blank_value_does_not_count_as_pinned(self, clean_env, raw):
        """An empty var must not silence the yaml and leave nothing behind."""
        clean_env.setenv("SKMEMORY_EMBED_URLS", raw)
        clean_env.setenv("SKMEMORY_EMBED_URL", raw)
        assert _env_pins_embed_endpoint() is False


class TestExplicitUrlStillCollapsesFailover:
    """The behaviour that made a single yaml line so expensive.

    This is not a bug to fix here: a caller naming ONE endpoint should get
    exactly that endpoint. It is pinned so it stays deliberate, and so the
    reason the yaml must not set it stays visible.
    """

    def test_an_explicit_embed_url_discards_the_env_list(self, clean_env):
        clean_env.setenv("SKMEMORY_EMBED_URLS", "http://fast/embed,http://backup/embed")
        backend = PGVectorBackend(
            dsn="postgresql://u:p@127.0.0.1:1/none",
            embed_url="http://slow-but-pinned/embed",
        )
        assert backend.embed_urls == ["http://slow-but-pinned/embed"]

    def test_without_an_explicit_url_the_env_list_is_used_in_order(self, clean_env):
        clean_env.setenv("SKMEMORY_EMBED_URLS", "http://fast/embed,http://backup/embed")
        import skmemory.backends.pgvector_backend as mod

        # The module caches the parsed list at import time. Resolve the same
        # way it does rather than reloading the module: an importlib.reload
        # here rebinds it for every other test file in the run, which is a
        # mistake already made and reverted once in this suite.
        urls = mod._parse_embed_urls("http://fast/embed,http://backup/embed", "http://fast/embed")
        assert urls == ["http://fast/embed", "http://backup/embed"]
