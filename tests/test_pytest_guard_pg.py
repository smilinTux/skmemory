"""Isolating the agent home does not isolate the DATABASE.

``tests/conftest.py`` points ``SKCAPSTONE_HOME`` at a temp directory, which
makes every flat write safe and makes the path-based guard's
``_is_isolated_elsewhere()`` return True. ``SKMEMORY_PG_DSN`` is untouched by
that, so the suite was isolated for files and completely unisolated for
Postgres. Measured 2026-10-17: the production ``skmemory`` database held 157
rows under agent ``test-agent`` with source ``openclaw``, titles like "Test
moment" and "Penguin Kingdom launch", accumulated by ordinary test runs. Same
class as postmortem §3.9, one layer down.
"""

from __future__ import annotations

import pytest

from skmemory.pytest_guard import (
    PRODUCTION_DB_NAMES,
    PytestRealMemoryWriteRefused,
    assert_pg_write_allowed,
)

PROD = "postgresql://postgres:pw@localhost:5432/skmemory"
THROWAWAY = "postgresql://postgres:pw@localhost:5432/skmemory_fusionbench"


def test_a_production_write_is_refused_during_pytest():
    """PYTEST_CURRENT_TEST is set while this runs, so the guard is live."""
    with pytest.raises(PytestRealMemoryWriteRefused) as exc:
        assert_pg_write_allowed(PROD, agent="test-agent")
    # The message has to tell the reader what to do about it.
    assert "throwaway" in str(exc.value).lower()
    assert "SKMEMORY_ALLOW_PYTEST_REAL_WRITES" in str(exc.value)


def test_a_throwaway_database_is_allowed():
    """Exactly what a test should be using: no exception."""
    assert_pg_write_allowed(THROWAWAY, agent="fusionbench")


def test_the_explicit_opt_in_is_honoured(monkeypatch):
    """An audited escape hatch, visible in the diff that uses it."""
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", "1")
    assert_pg_write_allowed(PROD, agent="lumina")


def test_the_guard_is_inert_outside_pytest(monkeypatch):
    """It must be incapable of changing production behaviour."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert_pg_write_allowed(PROD, agent="lumina")


def test_every_production_database_name_is_covered():
    for name in PRODUCTION_DB_NAMES:
        with pytest.raises(PytestRealMemoryWriteRefused):
            assert_pg_write_allowed(f"postgresql://u:p@localhost:5432/{name}")


def test_a_dsn_with_query_params_still_resolves_the_database_name():
    with pytest.raises(PytestRealMemoryWriteRefused):
        assert_pg_write_allowed(PROD + "?sslmode=disable")


@pytest.mark.parametrize("dsn", ["", "not-a-dsn", "postgresql://localhost"])
def test_an_unreadable_dsn_is_permitted_rather_than_guessed(dsn):
    """Refusing on an unparseable DSN would break legitimate callers for no
    safety gain: the guard only claims to recognise KNOWN production names."""
    assert_pg_write_allowed(dsn)
