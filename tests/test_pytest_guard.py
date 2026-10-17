"""TDD for skmemory/pytest_guard.py.

Card: skmemory pytest-writes-to-real-fleet (coordinator finding during the
2026-10-16 sqlite-only-orphans investigation). ~536 of Lumina's REAL
memories turned out to be pytest/CI test fixtures written in because some
consuming test suite imported skmemory with an ambient SKAGENT and no
SKCAPSTONE_HOME override.

These tests manipulate HOME and the relevant env vars directly rather than
going through the package's normal conftest isolation, since the whole
point is to exercise the guard's own view of "the real default tree."
"""

from __future__ import annotations

import pytest

from skmemory.pytest_guard import PytestRealMemoryWriteRefused, assert_write_allowed


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A fake $HOME with a real-looking (but throwaway) .skcapstone/agents
    tree, so "the real default" in these tests is this safe fixture, never
    the operator's actual home."""
    home = tmp_path / "home"
    (home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SKCAPSTONE_HOME", raising=False)
    monkeypatch.delenv("SKMEMORY_HOME", raising=False)
    monkeypatch.delenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_pytest_guard.py::fake")
    return home


def test_refuses_write_under_real_default_tree_during_pytest(fake_home):
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    with pytest.raises(PytestRealMemoryWriteRefused) as exc:
        assert_write_allowed(target)
    assert "lumina" in str(exc.value) or str(fake_home) in str(exc.value)


def test_noop_when_not_running_under_pytest(fake_home, monkeypatch):
    """Outside an active pytest test (PYTEST_CURRENT_TEST unset), the guard
    must never change behavior -- production safety net."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    assert_write_allowed(target)  # must not raise


def test_allowed_with_explicit_override_env(fake_home, monkeypatch):
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", "1")
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    assert_write_allowed(target)  # must not raise


@pytest.mark.parametrize("value", ["true", "YES", "On", "1"])
def test_override_env_accepts_common_truthy_spellings(fake_home, monkeypatch, value):
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", value)
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    assert_write_allowed(target)


def test_override_env_rejects_falsy_or_empty(fake_home, monkeypatch):
    monkeypatch.setenv("SKMEMORY_ALLOW_PYTEST_REAL_WRITES", "0")
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    with pytest.raises(PytestRealMemoryWriteRefused):
        assert_write_allowed(target)


def test_allowed_when_skcapstone_home_points_elsewhere(fake_home, monkeypatch, tmp_path):
    """The exact pattern tests/conftest.py already uses: SKCAPSTONE_HOME set
    to a tmp dir BEFORE anything imports skmemory. This must be trusted."""
    isolated = tmp_path / "isolated-test-home"
    (isolated / "agents" / "lumina" / "memory" / "long-term").mkdir(parents=True)
    monkeypatch.setenv("SKCAPSTONE_HOME", str(isolated))
    target = isolated / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    assert_write_allowed(target)  # must not raise -- this IS the isolated home


def test_allowed_when_skmemory_home_points_elsewhere(fake_home, monkeypatch, tmp_path):
    isolated = tmp_path / "isolated-skmemory-home"
    isolated.mkdir(parents=True)
    monkeypatch.setenv("SKMEMORY_HOME", str(isolated))
    target = isolated / "x.json"
    assert_write_allowed(target)  # must not raise


def test_write_outside_real_tree_entirely_is_fine(fake_home, tmp_path):
    """A write to some unrelated tmp path (not under the real default tree
    at all) is never the risk this guard protects against."""
    target = tmp_path / "somewhere-unrelated" / "x.json"
    assert_write_allowed(target)  # must not raise


def test_refusal_message_points_at_the_fix(fake_home):
    target = fake_home / ".skcapstone" / "agents" / "lumina" / "memory" / "long-term" / "x.json"
    with pytest.raises(PytestRealMemoryWriteRefused) as exc:
        assert_write_allowed(target)
    msg = str(exc.value)
    assert "SKCAPSTONE_HOME" in msg
    assert "SKMEMORY_ALLOW_PYTEST_REAL_WRITES" in msg
