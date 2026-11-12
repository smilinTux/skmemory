"""Static guards for the shipped skmemory-sync@ units.

A 15-min sync on a spinning disk ran back to back and saturated noroc2027
(2026-11-12). The default must stay hourly and the job must stay at the
lowest CPU and IO priority.
"""

from pathlib import Path

_SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"


def _lines(name: str) -> set[str]:
    return {ln.strip() for ln in (_SYSTEMD / name).read_text().splitlines()}


def test_sync_timer_defaults_to_hourly():
    lines = _lines("skmemory-sync@.timer")
    assert "OnUnitActiveSec=1h" in lines
    assert "OnBootSec=5min" in lines
    assert any(ln.startswith("RandomizedDelaySec=") for ln in lines)


def test_sync_service_runs_at_idle_priority():
    lines = _lines("skmemory-sync@.service")
    for want in ("Nice=19", "IOSchedulingClass=idle", "IOWeight=10"):
        assert want in lines, want


def test_sync_service_has_a_start_timeout():
    assert any(ln.startswith("TimeoutStartSec=") for ln in _lines("skmemory-sync@.service"))
