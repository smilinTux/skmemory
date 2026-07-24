"""Dual-mode tests for the skmemory ⇄ skcapstone integration adapter.

Verifies the contract from
skcapstone/docs/ADR-optional-integration-backbone.md:
  * standalone (SK_STANDALONE=1 or skcapstone absent) → native fallback, no crash
  * integrated (skcapstone present)                   → routes to sk-alert /
                                                         skscheduler / registry

The test suite's conftest points SKCAPSTONE_HOME at a temp dir, so integrated
mode writes there rather than the real ~/.skcapstone tree.
"""

from __future__ import annotations

import json

import pytest

from skmemory import integration


def _home():
    """The temp skcapstone shared home that conftest configured."""
    from skcapstone import shared_home

    return shared_home()


# --------------------------------------------------------------------------
# Standalone mode (operator forced)
# --------------------------------------------------------------------------

def test_standalone_env_disables_integration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SK_STANDALONE", "1")
    assert integration.is_present() is False
    assert integration.alert("x", {"m": 1}, level="error") is False
    assert integration.ensure_schedule() is False
    assert integration.register_self() is False


# --------------------------------------------------------------------------
# Absent mode (package not importable)
# --------------------------------------------------------------------------

def test_absent_skcapstone_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    monkeypatch.setattr(integration, "_sdk", None)
    assert integration.is_present() is False
    # alert still "works" (logs) and reports it did not publish
    assert integration.alert("sweep_failed", {"message": "boom"}, level="error") is False
    assert integration.ensure_schedule() is False


# --------------------------------------------------------------------------
# Integrated mode (skcapstone present)
# --------------------------------------------------------------------------

def test_present_alert_publishes_topic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    assert integration.is_present() is True

    assert integration.alert("sweep_failed", {"message": "boom"}, level="error") is True
    # Topic follows <service>.<severity> so `skcapstone alerts` (*.error) sees it;
    # the event name lives in the payload.
    topic_dir = _home() / "pubsub" / "topics" / "skmemory.error"
    assert topic_dir.is_dir()
    msgs = list(topic_dir.glob("msg-*.json"))
    assert msgs
    data = json.loads(msgs[-1].read_text())
    assert data["topic"] == "skmemory.error"
    assert data["payload"]["event"] == "sweep_failed"
    assert data["payload"]["message"] == "boom"


def test_present_ensure_schedule_writes_dropin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    assert integration.ensure_schedule(interval_hours=6) is True

    fragment = _home() / "config" / "jobs.d" / "skmemory_sweep.yaml"
    assert fragment.exists()

    from skcapstone.scheduler_jobs import load_jobs_with_dropins

    jobs = {j.name: j for j in load_jobs_with_dropins(_home() / "config" / "jobs.yaml")}
    assert "skmemory_sweep" in jobs
    assert jobs["skmemory_sweep"].command == "skmemory sweep"
    assert jobs["skmemory_sweep"].every_seconds == 6 * 3600

    # idempotent cleanup
    assert integration.unregister_schedule() is True
    assert not fragment.exists()


def test_present_register_self_writes_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    assert integration.register_self(pid_file="/tmp/skmemory.pid") is True
    entry = json.loads((_home() / "registry" / "skmemory.json").read_text())
    assert entry["name"] == "skmemory"
    assert entry["pid_file"] == "/tmp/skmemory.pid"
