"""Stub/shrink guard on PGVectorBackend.save() (card: skmemory
sqlite-only-orphans, 2026-10-16).

Before this guard, a save() of a rescued SQLite-preview stub (or any content
shorter than what's already in skmem-pg) would silently overwrite the pg
row via ``ON CONFLICT (id) DO UPDATE``. A flat->pg reconcile run did exactly
that to 373 rows during the 2026-10-16 recovery. These tests fake the
connection/cursor so no live Postgres is needed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from skmemory.models import Memory  # noqa: E402


class _FakeCursor:
    """Records executed SQL; returns a scripted fetchone() value per SELECT."""

    def __init__(self, fetchone_value=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self._fetchone_value = fetchone_value
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_value


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor


def _backend(monkeypatch, *, existing_len: int | None):
    """A PGVectorBackend whose embed never fails and whose fake connection
    reports `existing_len` chars already stored for any id (None = no row)."""
    import skmemory.backends.pgvector_backend as mod

    be = mod.PGVectorBackend(agent="lumina", embed_fn=lambda _t: [0.1] * 1024)
    row = (existing_len,) if existing_len is not None else None
    cur = _FakeCursor(fetchone_value=row)
    be._conn = _FakeConn(cur)
    return mod, be, cur


def _stub_memory(content_len: int = 10) -> Memory:
    return Memory(
        title="t",
        content=("x" * content_len) + "\n\n[recovered from SQLite preview — full content lost]",
        metadata={"recovered_from_sqlite_preview": True},
    )


def _real_memory(content: str) -> Memory:
    return Memory(title="t", content=content)


def test_stub_marker_in_metadata_refused_against_existing_row(monkeypatch):
    """A stub-flagged memory must never overwrite an existing pg row."""
    mod, be, cur = _backend(monkeypatch, existing_len=900)
    with pytest.raises(mod.StubOverwriteRefused):
        be.save(_stub_memory())
    assert not any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_stub_marker_refused_even_with_no_existing_row(monkeypatch):
    """Fresh insert of stub content must ALSO be refused -- this is exactly
    what laundered 373 rows into skmem-pg: ids absent from pg got a stub
    INSERTed as if it were real content."""
    mod, be, cur = _backend(monkeypatch, existing_len=None)
    with pytest.raises(mod.StubOverwriteRefused):
        be.save(_stub_memory())
    assert not any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_stub_marker_in_content_text_alone_is_detected(monkeypatch):
    """Even without the metadata flag, the literal recovery-marker text in
    content is enough to refuse (defense in depth: metadata can be lost or
    stripped by an intermediate hop, the text marker cannot)."""
    mod, be, _cur = _backend(monkeypatch, existing_len=None)
    mem = Memory(title="t", content="short\n\n[recovered from SQLite preview — full content lost]")
    with pytest.raises(mod.StubOverwriteRefused):
        be.save(mem)


def test_shrinking_non_stub_write_refused(monkeypatch):
    """A normal (non-stub) write that is SHORTER than the existing pg content
    must also be refused -- the general guard Chef asked for, not just the
    stub-specific one."""
    mod, be, cur = _backend(monkeypatch, existing_len=500)
    with pytest.raises(mod.StubOverwriteRefused):
        be.save(_real_memory("short new content"))
    assert not any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_growing_non_stub_write_allowed(monkeypatch):
    """A normal write that is LONGER than (or equal to) existing content is
    exactly the legitimate case and must proceed."""
    mod, be, cur = _backend(monkeypatch, existing_len=5)
    be.save(_real_memory("this is much longer than the old content"))
    assert any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_fresh_insert_non_stub_allowed(monkeypatch):
    """A brand new id with no existing row and ordinary content is the
    common case and must proceed normally."""
    mod, be, cur = _backend(monkeypatch, existing_len=None)
    be.save(_real_memory("brand new memory content"))
    assert any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_force_overrides_stub_refusal(monkeypatch):
    """force=True is the documented, explicit escape hatch."""
    mod, be, cur = _backend(monkeypatch, existing_len=900)
    be.save(_stub_memory(), force=True)
    assert any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_force_overrides_shrink_refusal(monkeypatch):
    mod, be, cur = _backend(monkeypatch, existing_len=500)
    be.save(_real_memory("short"), force=True)
    assert any("INSERT INTO memories" in sql for sql, _ in cur.executed)


def test_refusal_logs_loudly(monkeypatch, caplog):
    """A refusal must be visible in logs, not just raised and caught silently
    somewhere upstream -- this is exactly the failure mode that let 373 rows
    through unnoticed."""
    import logging

    mod, be, _cur = _backend(monkeypatch, existing_len=900)
    with (
        caplog.at_level(logging.WARNING, logger="skmemory.backends.pgvector_backend"),
        pytest.raises(mod.StubOverwriteRefused),
    ):
        be.save(_stub_memory())
    assert any("REFUS" in r.message.upper() for r in caplog.records)


def test_dimension_mismatch_still_raises_before_any_connection_use(monkeypatch):
    """Pre-existing invariant (card 41ec2201) must survive this change: an
    embedding-model mismatch must raise before the guard (or the insert)
    ever touches the connection."""
    import skmemory.backends.pgvector_backend as mod

    be = mod.PGVectorBackend(agent="lumina", embed_fn=lambda _t: [0.1] * 384)
    be._connection = lambda: (_ for _ in ()).throw(AssertionError("connection touched!"))
    mem = Memory(title="t", content="c")
    with pytest.raises(mod.EmbeddingModelMismatch):
        be.save(mem)
