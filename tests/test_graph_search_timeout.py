"""One retrieval arm was 94% of fused search latency.

Profiled on lumina's live corpus (14,203 Entity nodes, 259,010 MENTIONS
edges, 44,728 memories):

    embed query only              1.54s
    semantic arm                  0.26s
    lexical arm (BM25)            0.27s
    graph arm (AGE tokens)       10.72s   <-- 94% of the total
    FULL store.search            11.38s

The entity lookup is a case-insensitive REGEX over a node property, so no
index can serve it: pg_trgm is not installed and AGE's agtype blocks a plain
functional index. A deadline is the honest fix rather than an index guess, and
the fusion layer already treats an arm that returned nothing as having
abstained, so a capped arm degrades what it contributes instead of blocking
the query.

Measured after, on three real queries: mean 7.48s/query to 3.80s/query, and
much more consistent (6.1-9.9s to 3.4-4.0s). The entity A/B says the precision
cost is nil: `fused + graph (tokens)` scored 0.395 both capped and uncapped,
and the graph arm's own delta was +0.075 capped versus +0.070 uncapped, with
the harness's unaffected baselines differing by 0.015 in the same pair of runs
(its sampling noise floor).

THE SAFETY PROPERTY, which is the real reason this file exists: mutation and
guarded-reconcile reads must stay UNCAPPED. A truncated read there could be
misread as "the graph is nearly empty" and drive a mass prune.
"""

from __future__ import annotations

import pytest

from skmemory.backends.age_backend import AGEGraphBackend, _search_timeout_ms


class TestTimeoutKnob:
    def test_the_default_is_a_live_cap(self, monkeypatch):
        monkeypatch.delenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", raising=False)
        assert _search_timeout_ms() == 2500

    def test_the_env_overrides_it(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "900")
        assert _search_timeout_ms() == 900

    def test_zero_disables_the_cap(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "0")
        assert _search_timeout_ms() == 0

    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch):
        """A typo must not silently mean 'no cap'."""
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "soon")
        assert _search_timeout_ms() == 2500

    def test_blank_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "   ")
        assert _search_timeout_ms() == 2500


class _RecordingBackend(AGEGraphBackend):
    """Captures what timeout each _cypher call asked for, runs nothing."""

    def __init__(self):
        self.calls: list[int | None] = []
        self.graph = "test_knowledge"
        self.dsn = "postgresql://u:p@127.0.0.1:1/none"
        self.agent = "test"

    def _cypher(self, query, params=None, cols="v agtype", timeout_ms=None):
        self.calls.append(timeout_ms)
        return []


class TestSearchIsCapped:
    def test_search_by_entity_passes_a_timeout(self, monkeypatch):
        monkeypatch.delenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", raising=False)
        b = _RecordingBackend()
        b.search_by_entity("infrastructure", limit=10)
        assert b.calls == [2500]

    def test_the_configured_value_reaches_the_query(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "750")
        b = _RecordingBackend()
        b.search_by_entity("infrastructure", limit=10)
        assert b.calls == [750]


class TestMutationIsNotCapped:
    """The property that stops a cap from becoming data loss."""

    @pytest.mark.parametrize("method", ["index_memory", "remove_memory", "sync_all"])
    def test_write_paths_exist_and_do_not_reference_the_search_cap(self, method):
        import inspect

        src = inspect.getsource(getattr(AGEGraphBackend, method))
        assert "_search_timeout_ms" not in src, (
            f"{method} must not cap its own queries: a truncated write or "
            "backfill read can be misread as an empty graph and drive a prune"
        )

    def test_the_strict_reader_defaults_to_uncapped(self):
        """`_cypher_strict` backs the guarded reconcile path."""
        import inspect

        sig = inspect.signature(AGEGraphBackend._cypher_strict)
        assert sig.parameters["timeout_ms"].default is None

    def test_the_lenient_reader_defaults_to_uncapped(self):
        import inspect

        sig = inspect.signature(AGEGraphBackend._cypher)
        assert sig.parameters["timeout_ms"].default is None


class TestTokenFanoutBudget:
    def test_the_fanout_stops_once_the_budget_is_spent(self, monkeypatch):
        """Each sub-query being capped is NOT enough.

        This method issues one lookup per token, so N tokens could still total
        N times the per-query cap. One overall budget is what bounds it.
        """
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "50")
        calls: list[str] = []

        class _Slow(_RecordingBackend):
            def search_by_entity(self, query, limit=20):
                calls.append(query)
                import time

                time.sleep(0.08)  # each lookup overruns the 50ms budget
                return []

        b = _Slow()
        b.search_by_entity_tokens("alpha bravo charlie delta echo", limit=25)
        # The whole-query lookup always runs; the token fan-out must not.
        assert calls[0] == "alpha bravo charlie delta echo"
        assert len(calls) < 6, f"budget ignored, issued {len(calls)} lookups: {calls}"

    def test_with_budget_available_every_token_is_tried(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "60000")
        calls: list[str] = []

        class _Fast(_RecordingBackend):
            def search_by_entity(self, query, limit=20):
                calls.append(query)
                return []

        b = _Fast()
        b.search_by_entity_tokens("alpha bravo charlie", limit=25)
        assert calls[0] == "alpha bravo charlie"
        assert {"alpha", "bravo", "charlie"} <= set(calls)

    def test_a_disabled_cap_means_no_budget_cutoff(self, monkeypatch):
        monkeypatch.setenv("SKMEMORY_GRAPH_SEARCH_TIMEOUT_MS", "0")
        calls: list[str] = []

        class _Slow(_RecordingBackend):
            def search_by_entity(self, query, limit=20):
                calls.append(query)
                import time

                time.sleep(0.02)
                return []

        b = _Slow()
        b.search_by_entity_tokens("alpha bravo charlie", limit=25)
        assert {"alpha", "bravo", "charlie"} <= set(calls)
