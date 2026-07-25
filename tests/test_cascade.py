"""Tests for the derived-backend cascade executor (card 471d43c5).

Cover the executor contract directly: ordered fan-out, best-effort partial
failure that is *reported* rather than swallowed, absent-backend skipping, and
the required-method-missing gap. The forget() refactor that routes through this
executor is covered (unchanged) by tests/test_store.py and
tests/test_store_graph_integration.py.
"""

from __future__ import annotations

import logging

from skmemory.cascade import (
    FAILED,
    MISSING,
    OK,
    CascadeExecutor,
    CascadeStep,
)


class _Recorder:
    """A fake backend that records calls, or raises on demand."""

    def __init__(self, name: str, boom: bool = False) -> None:
        self._name = name
        self._boom = boom
        self.calls: list[tuple] = []

    def do(self, *args) -> None:
        self.calls.append(args)
        if self._boom:
            raise RuntimeError(f"{self._name} exploded")


class _NoMethod:
    """A backend missing the cascaded method (presence-check gap)."""


def test_runs_all_steps_in_order() -> None:
    order: list[str] = []
    a = _Recorder("a")
    b = _Recorder("b")

    def rec_a(*_):
        order.append("a")

    def rec_b(*_):
        order.append("b")

    a.do = rec_a  # type: ignore[method-assign]
    b.do = rec_b  # type: ignore[method-assign]

    res = CascadeExecutor().run(
        "op",
        [
            CascadeStep("first", a, "do", args=(1,)),
            CascadeStep("second", b, "do", args=(2,)),
        ],
    )

    assert order == ["a", "b"]  # order preserved
    assert res.ok
    assert [s.status for s in res.steps] == [OK, OK]


def test_mid_cascade_failure_reported_and_others_still_attempted(caplog) -> None:
    """A failing backend does not stop the cascade and is not swallowed."""
    good_before = _Recorder("before")
    bad = _Recorder("bad", boom=True)
    good_after = _Recorder("after")

    with caplog.at_level(logging.WARNING):
        res = CascadeExecutor().run(
            "op",
            [
                CascadeStep("before", good_before, "do", args=("x",)),
                CascadeStep(
                    "bad",
                    bad,
                    "do",
                    args=("x",),
                    warn_fail=lambda e: f"bad step failed: {e}",
                ),
                CascadeStep("after", good_after, "do", args=("x",)),
            ],
        )

    # The step after the failure still ran (best-effort, not fail-fast).
    assert good_before.calls == [("x",)]
    assert good_after.calls == [("x",)]

    # The failure is reported, not silently dropped.
    assert not res.ok
    assert len(res.failed) == 1
    assert res.failed[0].role == "bad"
    assert res.failed[0].status == FAILED
    assert "exploded" in (res.failed[0].error or "")
    assert any("bad step failed" in r.getMessage() for r in caplog.records)


def test_absent_backend_skipped_not_recorded() -> None:
    present = _Recorder("present")
    res = CascadeExecutor().run(
        "op",
        [
            CascadeStep("vector", None, "do", args=(1,)),
            CascadeStep("graph", present, "do", args=(1,)),
        ],
    )
    assert [s.role for s in res.steps] == ["graph"]  # None backend skipped
    assert res.ok


def test_required_missing_method_reported_and_warned(caplog) -> None:
    """check_presence=True: a missing method is a reported MISSING gap."""
    with caplog.at_level(logging.WARNING):
        res = CascadeExecutor().run(
            "op",
            [
                CascadeStep(
                    "vector",
                    _NoMethod(),
                    "remove",
                    args=(1,),
                    check_presence=True,
                    warn_missing="backend has no remove()",
                ),
            ],
        )
    assert not res.ok
    assert len(res.missing) == 1
    assert res.missing[0].status == MISSING
    assert any("no remove()" in r.getMessage() for r in caplog.records)


def test_no_presence_check_missing_method_is_a_failure() -> None:
    """check_presence=False: a missing method surfaces as FAILED, not MISSING."""
    res = CascadeExecutor().run(
        "op",
        [
            CascadeStep(
                "graph",
                _NoMethod(),
                "remove_memory",
                args=(1,),
                check_presence=False,
            ),
        ],
    )
    assert len(res.failed) == 1  # AttributeError caught as a plain failure
    assert res.missing == []
