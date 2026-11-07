"""SKMEM-P4-05: failure procedures are warnings, ranked below successes for how-to."""

from __future__ import annotations

from skmemory.models import Memory, MemoryLayer
from skmemory.procedures import WARNING_PREFIX, index_title, is_howto_query, order_procedures


def _m(i, kind="procedure", outcome=None, title="t"):
    return Memory(
        id=i, title=title, content="c", layer=MemoryLayer.SHORT, kind=kind, outcome=outcome
    )


def test_prefix_only_for_failure_procedure_and_content_untouched():
    f = _m("f", outcome="failure", title="run sync")
    assert index_title(f) == WARNING_PREFIX + "run sync"
    assert f.title == "run sync" and f.content == "c"
    assert index_title(_m("s", outcome="success", title="x")) == "x"
    assert index_title(_m("e", kind="episode", outcome="failure", title="x")) == "x"


def test_howto_ordering_and_failure_queries():
    f, s, e = _m("f", outcome="failure"), _m("s", outcome="success"), _m("e", kind="fact")
    got = order_procedures([f, e, s], "how do I recover the host")
    assert [m.id for m in got] == ["s", "e", "f"]  # kept, below the success
    for q in ("should I run sync", "how do I avoid the failed sync", "what is the schema"):
        assert not is_howto_query(q)
        assert [m.id for m in order_procedures([f, e, s], q)] == ["f", "e", "s"]


def test_failure_never_dropped_alone():
    f = _m("f", outcome="failure")
    assert order_procedures([f], "how do I x") == [f]
