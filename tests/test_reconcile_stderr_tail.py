"""A failure message reduced to "^" is indistinguishable from no message.

`_stderr_tail` returned the LAST stderr line. psql reports a syntax error as
three lines, and the last one is the caret pointer:

    ERROR:  syntax error at or near "foo"
    LINE 1: select foo
                   ^

so the diagnostic that names the problem was discarded and the caret was kept.
Twice on 2026-10-20 a reconcile failure surfaced to the log as a single "^"
with the real error recorded nowhere, which sent the investigation looking for
a connection problem that did not exist.

Credential scrubbing must survive all of this, since the whole reason the
stderr is filtered is that a DSN can carry a password.
"""

from __future__ import annotations

import pytest

from skmemory.reconcile import _scrub_stderr, _stderr_tail

PSQL_SYNTAX_ERROR = """ERROR:  syntax error at or near "select"
LINE 1: select select
               ^
"""


class TestItKeepsTheDiagnostic:
    def test_a_syntax_error_returns_the_error_line_not_the_caret(self):
        out = _stderr_tail(PSQL_SYNTAX_ERROR)
        assert out.startswith("ERROR:")
        assert "syntax error" in out
        assert out != "^"

    def test_a_fatal_is_preferred_over_later_noise(self):
        stderr = 'FATAL:  password authentication failed for user "postgres"\nLINE 1: x\n   ^\n'
        assert _stderr_tail(stderr).startswith("FATAL:")

    @pytest.mark.parametrize("prefix", ["ERROR:", "FATAL:", "PANIC:", "DETAIL:", "HINT:"])
    def test_every_postgres_diagnostic_prefix_wins(self, prefix):
        stderr = f"some preamble\n{prefix}  the real problem\n       ^\n"
        assert _stderr_tail(stderr) == f"{prefix}  the real problem"

    def test_the_first_diagnostic_wins_when_there_are_several(self):
        stderr = "ERROR:  the first problem\nDETAIL:  more about it\n"
        assert _stderr_tail(stderr) == "ERROR:  the first problem"


class TestFallbacks:
    def test_without_a_diagnostic_it_skips_caret_noise(self):
        stderr = "something went wrong\nLINE 1: select x\n        ^\n"
        assert _stderr_tail(stderr) == "something went wrong"

    def test_a_plain_message_is_returned_unchanged(self):
        assert _stderr_tail("connection refused") == "connection refused"

    def test_it_never_returns_less_than_the_last_line(self):
        """Degenerate input: only caret noise. Must still say something."""
        assert _stderr_tail("   ^\n") == "^"

    @pytest.mark.parametrize("empty", ["", "   ", "\n\n", None])
    def test_empty_stderr_is_empty_not_an_exception(self, empty):
        assert _stderr_tail(empty or "") == ""

    def test_blank_lines_do_not_become_the_answer(self):
        stderr = "real detail here\n\n\n"
        assert _stderr_tail(stderr) == "real detail here"


class TestScrubbingStillApplies:
    def test_a_password_in_the_diagnostic_is_redacted(self):
        stderr = "FATAL:  bad dsn postgresql://postgres:hunter2@localhost:5432/db\n   ^\n"
        out = _stderr_tail(stderr)
        assert "hunter2" not in out
        assert "***" in out

    def test_a_password_kv_pair_is_redacted(self):
        out = _stderr_tail("ERROR:  could not connect password=hunter2 host=x\n")
        assert "hunter2" not in out

    def test_scrub_helper_is_unchanged_for_plain_text(self):
        assert _scrub_stderr("nothing secret") == "nothing secret"
