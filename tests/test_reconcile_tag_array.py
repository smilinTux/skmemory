"""One unescaped backslash in one tag threw away 40 minutes of embedding.

The backfill COPYs every row into a scratch table and then issues ONE
`INSERT ... SELECT`. Tags were rendered into a Postgres array literal by
escaping `"` and nothing else. Inside a double-quoted array element Postgres
treats BOTH `"` and `\\` as escape-requiring, so a tag ENDING in a backslash
was fatal: the trailing `\\` escaped the closing quote, the element never
terminated, and the whole statement died with

    ERROR:  malformed array literal: "{...,"section:\\"}"
    DETAIL:  Unexpected end of input.

Not a theoretical input. Session-digest tags embed a section heading verbatim
(`section:<heading>`) and headings contain quotes and backslashes. Measured
2026-10-20 on a real run: 200 of 24,429 staged lumina rows carried a backslash,
and the single INSERT aborted on the first one, leaving pg at its old count
with every embedding discarded.

Verified against a live Postgres before and after:

    case                  OLD     NEW
    plain                True    True
    embedded quote       True    True
    TRAILING slash      False    True
    embedded slash       True    True
    the real tag        False    True
"""

from __future__ import annotations

import pytest

from skmemory.reconcile import pg_text_array


def _old_broken(values):
    """The implementation this replaced, kept so the tests can contrast it."""
    return "{" + ",".join('"' + str(t).replace('"', '\\"') + '"' for t in (values or [])) + "}"


class TestTheFatalCase:
    def test_a_tag_ending_in_a_backslash_is_escaped(self):
        """The exact break: the trailing slash used to eat the closing quote."""
        assert pg_text_array(["section:\\"]) == '{"section:\\\\"}'

    def test_the_old_version_produced_an_unterminated_element(self):
        """Contrast, so the regression is legible without a database."""
        broken = _old_broken(["section:\\"])
        assert broken == '{"section:\\"}'
        # The closing quote is consumed by the backslash, so after stripping
        # the braces the element has no terminating quote of its own.
        assert not broken[1:-1].endswith('\\"\\"')

    def test_the_real_production_tag_set(self):
        tags = ["auto-save", "pre-compact", "agent:lumina", "section:\\"]
        out = pg_text_array(tags)
        assert out.endswith('"section:\\\\"}')
        assert out.count('"') == 2 * len(tags)


class TestEscaping:
    def test_plain_values_are_quoted(self):
        assert pg_text_array(["hermes", "decomposed"]) == '{"hermes","decomposed"}'

    def test_an_embedded_quote_is_escaped(self):
        assert pg_text_array(['say "hi"']) == '{"say \\"hi\\""}'

    def test_an_embedded_backslash_is_doubled(self):
        assert pg_text_array(["a\\b"]) == '{"a\\\\b"}'

    def test_backslashes_are_doubled_before_quotes_are_escaped(self):
        """Order matters, or the backslash we ADD gets doubled too."""
        assert pg_text_array(['a\\"b']) == '{"a\\\\\\"b"}'

    def test_a_lone_backslash(self):
        assert pg_text_array(["\\"]) == '{"\\\\"}'

    def test_consecutive_backslashes(self):
        assert pg_text_array(["a\\\\b"]) == '{"a\\\\\\\\b"}'


class TestEdges:
    @pytest.mark.parametrize("empty", [[], None, ()])
    def test_nothing_renders_an_empty_array(self, empty):
        assert pg_text_array(empty) == "{}"

    def test_non_strings_are_coerced(self):
        assert pg_text_array([1, True, 2.5]) == '{"1","True","2.5"}'

    def test_a_comma_inside_a_value_stays_inside_its_element(self):
        assert pg_text_array(["a,b", "c"]) == '{"a,b","c"}'

    def test_braces_inside_a_value_are_not_special(self):
        assert pg_text_array(["{nested}"]) == '{"{nested}"}'

    def test_unicode_survives(self):
        assert pg_text_array(["section:🛑 stop"]) == '{"section:🛑 stop"}'

    def test_every_element_is_individually_quoted(self):
        out = pg_text_array(["a", "b", "c"])
        assert out == '{"a","b","c"}'
        assert out.startswith("{") and out.endswith("}")
