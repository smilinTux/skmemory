"""Tests for MCP server store wiring (skmemory.mcp_server._get_store).

Gap B (card dc8280a7): the MCP _get_store built a graph-less MemoryStore, so
forget() never cascaded a DETACH DELETE to the live skmem-pg AGE
<agent>_knowledge graph — a forgotten memory's AGE node was orphaned. These
tests verify AGE is wired into the graph role when pgvector is the healthy
vector backend, and that the wiring degrades gracefully.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import skmemory.mcp_server as mcp_server
from skmemory.backends.age_backend import AGEGraphBackend


def _reset_store():
    mcp_server._store = None


def test_mcp_get_store_wires_age_graph_when_pgvector_healthy(monkeypatch):
    """When pgvector is the healthy vector backend, the MCP store wires an AGE
    graph so forget() cascades a DETACH DELETE to the AGE graph.

    Fail-before: MemoryStore(vector=vector) was built with no graph, leaving
    store.graph is None and AGE nodes orphaned on forget.
    """
    _reset_store()
    monkeypatch.setenv("SKMEMORY_VECTOR_BACKEND", "pgvector")
    monkeypatch.setenv("SKMEMORY_PG_DSN", "postgresql://u:p@node/skmemory")

    healthy_pg = MagicMock()
    healthy_pg.health_check.return_value = {"ok": True}

    with patch(
        "skmemory.backends.pgvector_backend.PGVectorBackend", return_value=healthy_pg
    ):
        store = mcp_server._get_store()

    assert store.vector is healthy_pg
    assert isinstance(store.graph, AGEGraphBackend)
    assert store.graph.dsn == "postgresql://u:p@node/skmemory"
    _reset_store()


def test_mcp_get_store_age_wiring_degrades_when_construction_fails(monkeypatch):
    """If AGE construction raises, the MCP store still builds (graph=None) and
    forget() must not hard-fail."""
    _reset_store()
    monkeypatch.setenv("SKMEMORY_VECTOR_BACKEND", "pgvector")

    healthy_pg = MagicMock()
    healthy_pg.health_check.return_value = {"ok": True}

    with (
        patch(
            "skmemory.backends.pgvector_backend.PGVectorBackend", return_value=healthy_pg
        ),
        patch(
            "skmemory.backends.age_backend.AGEGraphBackend",
            side_effect=RuntimeError("skmem-pg unreachable"),
        ),
    ):
        store = mcp_server._get_store()

    assert store.vector is healthy_pg
    assert store.graph is None
    _reset_store()
