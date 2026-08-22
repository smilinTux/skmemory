"""Small psql-compatible transport for the configured skmem-pg DSN.

The reconcile engine historically required Docker socket access so it could
run psql inside the skmem-pg container. Fleet clients already receive a
node-local ``SKMEMORY_PG_DSN`` through protected environment state. This shim
lets the existing reconcile SQL use that same connection without placing the
DSN in process arguments or logs.
"""

from __future__ import annotations

import os
import sys


def _sql_from_argv(argv: list[str]) -> tuple[str, bool]:
    """Return SQL and whether tuple-only output was requested."""
    tuple_only = any(arg.startswith("-tA") for arg in argv)
    if "-c" in argv:
        index = argv.index("-c")
        if index + 1 >= len(argv):
            raise ValueError("-c requires SQL")
        return argv[index + 1], tuple_only
    if "-f" in argv:
        index = argv.index("-f")
        if index + 1 >= len(argv) or argv[index + 1] != "-":
            raise ValueError("only -f - is supported")
        return sys.stdin.read(), tuple_only
    raise ValueError("expected -c SQL or -f -")


def _print_rows(rows: list[tuple[object, ...]]) -> None:
    for row in rows:
        print("\t".join("" if value is None else str(value) for value in row))


def main(argv: list[str] | None = None) -> int:
    """Execute the reconcile engine's supported psql argument subset."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sql, _tuple_only = _sql_from_argv(args)
        dsn = os.environ.get("SKMEMORY_PG_DSN", "").strip()
        if not dsn:
            raise RuntimeError("SKMEMORY_PG_DSN is required for the DSN transport")

        import psycopg

        with (
            psycopg.connect(dsn, autocommit=True, connect_timeout=10) as connection,
            connection.cursor() as cursor,
        ):
            normalized = sql.lstrip().lower()
            if normalized.startswith("copy ") and " from stdin" in normalized:
                with cursor.copy(sql.rstrip().rstrip(";")) as copy:
                    copy.write(sys.stdin.buffer.read())
            else:
                cursor.execute(sql)
                if cursor.description:
                    _print_rows(cursor.fetchall())
        return 0
    except Exception as exc:
        print(f"skmem-pg SQL transport failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
