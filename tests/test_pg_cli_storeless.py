"""`skmemory pg ...` never opens an agent memory store.

A schema migration targets the database, not an agent's memory profile. On a
fresh node no agent is registered yet, and the group callback used to build a
MemoryStore first, so `skmemory pg migrate` (the first step of
`skos install skbrain`) refused with "No valid registered memory profile".
"""

from __future__ import annotations

from click.testing import CliRunner

from skmemory import cli as cli_mod


def test_pg_group_does_not_open_a_store(monkeypatch):
    def refuse(**_kw):
        raise ValueError("No valid registered memory profile is available")

    monkeypatch.setattr(cli_mod, "_get_store", refuse)
    result = CliRunner().invoke(cli_mod.cli, ["pg", "migrate", "--help"])
    assert result.exit_code == 0, result.output
    assert "memory profile" not in result.output
