"""SQLite FTS5 lexical arm over the local index (SKMEM-P5-03).

The ``memories`` table only holds a 150-char ``content_preview``; the full
text lives in the flat JSON files. An FTS5 table over the preview would be a
weak arm, so the full title, content, summary and tags are indexed instead,
as a *contentless* FTS5 table (``content=''``, ``contentless_delete=1``). That
stores the inverted index only, never a second copy of the text (measured
0.59x of raw text bytes on Zipf-like prose, 0.95x on worst-case random
vocabulary; ``detail=column``/``none`` shrink it but make bm25() return 0), and rows are
removed by rowid. A small sidecar ``fts_meta`` table records which rows are
indexed (and their ``context_tag``, which has no column in ``memories``), so
filters push down into SQL and backfill can tell what is still missing.

Sync is done by the writer (``index_memory``) because the full content is not
in ``memories`` for a trigger to see; an AFTER DELETE trigger on ``memories``
keeps deletes and reindex rebuilds consistent.

Privacy: the vaulted backend encrypts flat files at rest, so it must not write
plaintext content into the index. It sets ``fts_enabled = False`` and calls
``drop`` so an FTS table left by an earlier unencrypted open is removed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Sequence

logger = logging.getLogger("skmemory.fts_local")

# title, content, summary, tags
_BM25_WEIGHTS = "8.0, 1.0, 3.0, 4.0"
_MAX_TERMS = 32
_WORD = re.compile(r"\w+", re.UNICODE)

_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
    "title, content, summary, tags, content='', contentless_delete=1, "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TABLE IF NOT EXISTS fts_meta ("
    "rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
    "content_hash TEXT NOT NULL DEFAULT '', context_tag TEXT NOT NULL DEFAULT '')",
    "CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN "
    "DELETE FROM memory_fts WHERE rowid IN (SELECT rowid FROM fts_meta WHERE id = old.id); "
    "DELETE FROM fts_meta WHERE id = old.id; END",
)


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the (empty) FTS tables if missing. Never backfills. True if usable."""
    try:
        for ddl in _DDL:
            conn.execute(ddl)
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.warning("fts_local: FTS5 unavailable, lexical-local arm disabled: %s", e)
        return False


def drop(conn: sqlite3.Connection) -> None:
    """Remove every FTS artefact (used by backends that must not hold plaintext).

    A dropped table's pages stay in the file's free list with their old bytes,
    so when anything was actually dropped the file is VACUUMed to scrub them.
    """
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE 'memory_fts%' OR name = 'fts_meta' LIMIT 1"
    ).fetchone()
    for stmt in (
        "DROP TRIGGER IF EXISTS memories_fts_ad",
        "DROP TABLE IF EXISTS memory_fts",
        "DROP TABLE IF EXISTS fts_meta",
    ):
        conn.execute(stmt)
    conn.commit()
    if existed:
        try:
            conn.execute("VACUUM")
        except sqlite3.Error as e:
            # A locked database must not make opening the vault raise. The
            # tables are already gone; only the freelist scrub is skipped.
            logger.warning("fts_local: VACUUM after drop skipped: %s", e)


def sanitize_match(query: str, joiner: str = " OR ") -> str | None:
    """Turn free text into a MATCH expression that cannot raise a syntax error.

    Each whitespace-separated chunk is split into its word characters and
    emitted as a double-quoted phrase (so ``prb-6f069c5e`` becomes the phrase
    ``"prb 6f069c5e"``, quotes, colons, parentheses, ``NEAR``, ``AND`` and
    friends are all inert). Chunks are OR-joined and ranked by bm25. Returns
    None when the query has no word characters.
    """
    terms: list[str] = []
    for chunk in (query or "").split():
        parts = _WORD.findall(chunk)
        if parts:
            terms.append('"' + " ".join(parts) + '"')
        if len(terms) >= _MAX_TERMS:
            break
    return joiner.join(terms) if terms else None


def _row_values(memory) -> tuple[str, str, str, str]:
    return (
        memory.title or "",
        memory.content or "",
        memory.summary or "",
        " ".join(memory.tags or []),
    )


def index_memory(conn: sqlite3.Connection, memory, content_hash: str) -> None:
    """Index one memory's full text. Call inside the caller's transaction.

    Keyed on the memory id, not ``memories.rowid`` (an implicit rowid that an
    external VACUUM may renumber). The FTS rowid is ``fts_meta.rowid``, an
    INTEGER PRIMARY KEY that VACUUM preserves.

    Raises sqlite3.Error; the caller decides whether that is fatal.
    """
    ctx = getattr(memory, "context_tag", "") or ""
    row = conn.execute("SELECT rowid FROM fts_meta WHERE id = ?", (memory.id,)).fetchone()
    if row is None:
        rid = conn.execute(
            "INSERT INTO fts_meta(id, content_hash, context_tag) VALUES (?, ?, ?)",
            (memory.id, content_hash, ctx),
        ).lastrowid
    else:
        rid = row[0]
        conn.execute(
            "UPDATE fts_meta SET content_hash = ?, context_tag = ? WHERE rowid = ?",
            (content_hash, ctx, rid),
        )
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rid,))
    conn.execute(
        "INSERT INTO memory_fts(rowid, title, content, summary, tags) VALUES (?, ?, ?, ?, ?)",
        (rid, *_row_values(memory)),
    )


def coverage(conn: sqlite3.Connection) -> dict:
    """Indexed vs total row counts, for health and the lazy-backfill decision."""
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    indexed = conn.execute(
        "SELECT COUNT(*) FROM memories m JOIN fts_meta f ON f.id = m.id"
    ).fetchone()[0]
    return {"total": total, "indexed": indexed, "missing": total - indexed}


def backfill(
    conn: sqlite3.Connection,
    load_row: Callable[[sqlite3.Row], object | None],
    *,
    max_rows: int | None = None,
    budget_s: float | None = None,
) -> dict:
    """Index rows that have no FTS entry yet, bounded by rows and/or seconds.

    ``load_row`` hydrates a ``memories`` row to a full Memory from its flat
    file. A row whose file cannot be loaded gets a marker in ``fts_meta`` with
    ``content_hash='unloadable'`` and no FTS document, so it is not retried on
    every call (a reindex clears it). Safe to call repeatedly and concurrently
    with searches on the same connection: it commits per batch.
    """
    start = time.monotonic()
    done = skipped = 0
    while True:
        batch = 200
        if max_rows is not None:
            batch = min(batch, max_rows - done - skipped)
            if batch <= 0:
                break
        rows = conn.execute(
            "SELECT m.* FROM memories m "
            "LEFT JOIN fts_meta f ON f.id = m.id WHERE f.id IS NULL LIMIT ?",
            (batch,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            mem = None
            try:
                mem = load_row(row)
            except Exception as e:  # unreadable file: mark, do not loop
                logger.debug("fts_local: backfill could not load %s: %s", row["id"], e)
            if mem is None:
                conn.execute(
                    "INSERT OR REPLACE INTO fts_meta(id, content_hash) VALUES (?, 'unloadable')",
                    (row["id"],),
                )
                skipped += 1
            else:
                index_memory(conn, mem, row["content_hash"] or "")
                done += 1
            if budget_s is not None and time.monotonic() - start >= budget_s:
                conn.commit()
                return {"indexed": done, "unloadable": skipped, "complete": False}
        conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM memories m LEFT JOIN fts_meta f ON f.id = m.id WHERE f.id IS NULL"
    ).fetchone()[0]
    return {"indexed": done, "unloadable": skipped, "complete": left == 0}


def search_rows(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    layer=None,
    tags: Sequence[str] | None = None,
    source: str | None = None,
    context_tag: str | Sequence[str] | None = None,
) -> list[sqlite3.Row]:
    """bm25-ranked ``memories`` rows with P0-05 filter semantics pushed into SQL.

    ``tags`` is AND. ``context_tag`` is an exact tag or an any-of list, and an
    empty list matches nothing (fails closed). ``None`` means no filter.
    """
    match_or = sanitize_match(query)
    if match_or is None or limit <= 0:
        return []
    where = ["memory_fts MATCH ?"]
    params: list = [None]
    if layer is not None:
        where.append("m.layer = ?")
        params.append(str(getattr(layer, "value", layer)))
    for tag in tags or []:
        where.append("instr(',' || m.tags || ',', ?) > 0")
        params.append(f",{tag},")
    if source:
        where.append("m.source = ?")
        params.append(source)
    if context_tag is not None:
        allowed = [context_tag] if isinstance(context_tag, str) else list(context_tag)
        if not allowed:
            return []
        where.append(f"f.context_tag IN ({','.join('?' * len(allowed))})")
        params.extend(allowed)
    params.append(limit)
    sql = (
        "SELECT m.* FROM memory_fts "
        "JOIN fts_meta f ON f.rowid = memory_fts.rowid "
        "JOIN memories m ON m.id = f.id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY bm25(memory_fts, {_BM25_WEIGHTS}) LIMIT ?"
    )
    # All terms first (precise, like the LIKE path's AND), any term as fallback.
    for match in (sanitize_match(query, " AND "), match_or):
        params[0] = match
        rows = conn.execute(sql, params).fetchall()
        if rows or match == match_or:
            return rows
    return []
