"""`deploy/skmem-pg/schema.sql` must be able to build a database from nothing.

The file is a pg_dump, and the dump's `CREATE SCHEMA paradedb` /
`CREATE SCHEMA ag_catalog` statements were stripped from it (the extensions
create their own schemas). The `CREATE EXTENSION ... WITH SCHEMA <name>`
clauses were left behind, so on a FRESH database the clause named a schema
that nothing had created and initdb aborted with "schema paradedb does not
exist". An existing database bootstrapped by the older
`02-enable-bm25-age.sql` never hit it, so the break was invisible until
someone built a new node.

That is the shape this guards: a deploy artifact that only works where the
thing it is supposed to create already exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "deploy" / "skmem-pg" / "schema.sql"

#: Extensions that create their own schema, so naming one is both redundant
#: and a fresh-bootstrap failure.
SELF_SCHEMA_EXTENSIONS = ("pg_search", "age")


@pytest.fixture(scope="module")
def sql() -> str:
    assert SCHEMA_SQL.is_file(), f"missing deploy artifact: {SCHEMA_SQL}"
    return SCHEMA_SQL.read_text(encoding="utf-8")


def _created_schemas(sql: str) -> set[str]:
    return {
        m.group(1).strip('"')
        for m in re.finditer(r"CREATE SCHEMA (?:IF NOT EXISTS )?([\w\"]+)", sql, re.I)
    }


@pytest.mark.parametrize("extension", SELF_SCHEMA_EXTENSIONS)
def test_extension_does_not_name_a_schema_nobody_creates(sql, extension):
    """The exact break: WITH SCHEMA pointing at a schema this file never creates."""
    pattern = re.compile(
        rf"CREATE EXTENSION\s+(?:IF NOT EXISTS\s+)?{extension}\s+WITH SCHEMA\s+([\w\"]+)",
        re.I,
    )
    match = pattern.search(sql)
    if match is None:
        return  # no WITH SCHEMA clause at all, which is the fixed state
    named = match.group(1).strip('"')
    created = _created_schemas(sql)
    assert named in created, (
        f"CREATE EXTENSION {extension} WITH SCHEMA {named}, but this file never "
        f"creates {named}. A fresh initdb aborts here. Drop the WITH SCHEMA "
        f"clause: the extension creates its own schema."
    )


@pytest.mark.parametrize("extension", SELF_SCHEMA_EXTENSIONS)
def test_the_extension_is_created_at_all(sql, extension):
    """Guard the other direction: do not 'fix' this by deleting the extension."""
    assert re.search(rf"CREATE EXTENSION\s+(?:IF NOT EXISTS\s+)?{extension}\b", sql, re.I), (
        f"{extension} is no longer created; BM25 or AGE would be missing entirely"
    )


def test_vector_extension_is_present(sql):
    """pgvector is the reason this database exists."""
    assert re.search(r"CREATE EXTENSION\s+(?:IF NOT EXISTS\s+)?vector\b", sql, re.I)


def test_it_agrees_with_the_proven_bootstrap_script(sql):
    """`02-enable-bm25-age.sql` is the historical, known-working bootstrap.

    It has always used bare `CREATE EXTENSION IF NOT EXISTS <name>;`. If the
    dump disagrees with it on how extensions are created, one of them is wrong
    and it is not the one that has been building databases successfully.
    """
    proven = SCHEMA_SQL.parent / "02-enable-bm25-age.sql"
    if not proven.is_file():
        pytest.skip(f"{proven.name} not present")
    proven_sql = proven.read_text(encoding="utf-8")
    for extension in SELF_SCHEMA_EXTENSIONS:
        assert not re.search(
            rf"CREATE EXTENSION\s+(?:IF NOT EXISTS\s+)?{extension}\s+WITH SCHEMA", proven_sql, re.I
        ), f"{proven.name} unexpectedly uses WITH SCHEMA for {extension}"
        assert not re.search(
            rf"CREATE EXTENSION\s+(?:IF NOT EXISTS\s+)?{extension}\s+WITH SCHEMA", sql, re.I
        ), f"schema.sql uses WITH SCHEMA for {extension} but {proven.name} does not"
