"""Per-node pg DSN guard (card: prb-6f069c5e / skmemory sqlite-only-orphans).

skmem-pg is per-node by design: each node runs its own writable Postgres
and agents point only at localhost. prb-6f069c5e's own remediation plan
found that every agent's SYNCED skmemory.yaml had hardcoded
`pgvector_dsn=192.168.0.158:5432`, making `.158` a single point of failure
the per-node architecture was supposed to retire -- and the laptop had no
local pg at all for most of the 2026-10-16 incident's window. This guard
makes a non-loopback DSN loud by default, and refusable on request, so a
synced config or env file can never silently re-point a node at another
node's pg again without anyone noticing.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("psycopg")


def _reload(monkeypatch):
    import importlib

    import skmemory.backends.pgvector_backend as mod

    return importlib.reload(mod)


def _construct(monkeypatch, dsn, **env):
    for key in (
        "SKMEMORY_PG_ALLOW_REMOTE",
        "SKMEMORY_PG_REMOTE_STRICT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mod = _reload(monkeypatch)
    return mod, mod.PGVectorBackend(dsn=dsn, agent="lumina")


def test_loopback_dsn_is_silent(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="skmemory.backends.pgvector_backend"):
        _construct(monkeypatch, "postgresql://postgres:x@localhost:5432/skmemory")
    assert not any("per-node" in r.message for r in caplog.records)


def test_127_0_0_1_is_silent(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="skmemory.backends.pgvector_backend"):
        _construct(monkeypatch, "postgresql://postgres:x@127.0.0.1:5432/skmemory")
    assert not any("per-node" in r.message for r in caplog.records)


def test_remote_host_warns_loudly_by_default(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="skmemory.backends.pgvector_backend"):
        _construct(monkeypatch, "postgresql://postgres:x@192.168.0.158:5432/skmemory")
    assert any("per-node" in r.message and "192.168.0.158" in r.message for r in caplog.records)


def test_remote_host_does_not_raise_by_default(monkeypatch):
    # Fail-open by default: a loud warning, never a crash, unless strict
    # mode is explicitly requested.
    _construct(monkeypatch, "postgresql://postgres:x@192.168.0.158:5432/skmemory")


def test_remote_host_raises_in_strict_mode(monkeypatch):
    # _construct() reloads the module itself (to re-evaluate DEFAULT_DSN
    # against freshly-set env), which gives each call its own fresh class
    # objects -- compare by name, not by capturing the exception class from
    # a separate reload before the one that actually raises.
    with pytest.raises(Exception) as exc_info:
        _construct(
            monkeypatch,
            "postgresql://postgres:x@192.168.0.158:5432/skmemory",
            SKMEMORY_PG_REMOTE_STRICT="1",
        )
    assert exc_info.type.__name__ == "RemotePgDsnRefused"


def test_remote_host_allowed_silences_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="skmemory.backends.pgvector_backend"):
        _construct(
            monkeypatch,
            "postgresql://postgres:x@192.168.0.158:5432/skmemory",
            SKMEMORY_PG_ALLOW_REMOTE="1",
        )
    assert not any("per-node" in r.message for r in caplog.records)


def test_remote_host_allowed_also_bypasses_strict_mode(monkeypatch):
    """The explicit allow flag is a stronger override than strict mode --
    an operator who audited and allowed the exception should never be
    blocked even if strict mode is also set."""
    _construct(
        monkeypatch,
        "postgresql://postgres:x@192.168.0.158:5432/skmemory",
        SKMEMORY_PG_ALLOW_REMOTE="1",
        SKMEMORY_PG_REMOTE_STRICT="1",
    )
