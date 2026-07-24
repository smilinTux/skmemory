"""
SQLite-indexed storage backend (Level 0.5).

Solves the file-scanning problem: instead of reading every JSON file
on every list/search/filter, we maintain a SQLite index alongside
the JSON files. Queries hit the index. Full content loads on demand.

Zero infrastructure. Ships with Python. Instant boot.

Performance:
    File scan (1000 memories): ~2-5 seconds
    SQLite query (1000 memories): ~2-5 milliseconds

Directory layout (same as FileBackend):
    base_path/
    ├── index.db           <-- NEW: SQLite index
    ├── short-term/
    │   └── {id}.json
    ├── mid-term/
    │   └── ...
    └── long-term/
        └── ...
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config import SKMEMORY_HOME
from ..models import Memory, MemoryLayer
from .base import BaseBackend

logger = logging.getLogger("skmemory.sqlite_backend")

# How long a busy/locked connection waits before raising (seconds).
_BUSY_TIMEOUT_S = 5.0


def _is_corruption(err: Exception) -> bool:
    """Return True if a SQLite error indicates a corrupt/unreadable index.

    We distinguish corruption (recoverable by rebuilding the index from the
    flat JSON source of truth) from transient conditions like a locked DB,
    which should NOT trigger a rebuild.
    """
    msg = str(err).lower()
    return (
        "malformed" in msg
        or "not a database" in msg
        or "disk image" in msg
        or "file is encrypted" in msg
        or "database corruption" in msg
    )


def _resilient_read(default_factory):
    """Decorate a read method to degrade gracefully on SQLite errors.

    Read paths must never crash a caller: the flat JSON files remain the
    source of truth, so an unavailable/locked/corrupt index just yields a
    safe empty default. Corruption additionally triggers a one-shot recovery
    attempt (quarantine + rebuild) before returning the default.

    Args:
        default_factory: Zero-arg callable producing the fallback value
            (e.g. ``list`` or ``dict``).
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except sqlite3.Error as e:
                logger.warning(
                    "sqlite_backend: %s degraded, index unavailable (%s)",
                    fn.__name__,
                    e,
                )
                if _is_corruption(e):
                    try:
                        self._recover_from_corruption(e)
                    except Exception as rec_err:  # pragma: no cover - defensive
                        logger.error("sqlite_backend: corruption recovery failed: %s", rec_err)
                return default_factory()

        return wrapper

    return decorator


DEFAULT_BASE_PATH = str(SKMEMORY_HOME / "memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
id TEXT PRIMARY KEY,
title TEXT NOT NULL,
layer TEXT NOT NULL,
role TEXT NOT NULL DEFAULT 'general',
tags TEXT NOT NULL DEFAULT '',
source TEXT NOT NULL DEFAULT 'manual',
source_ref TEXT NOT NULL DEFAULT '',
summary TEXT NOT NULL DEFAULT '',
content_preview TEXT NOT NULL DEFAULT '',
emotional_intensity REAL NOT NULL DEFAULT 0.0,
emotional_valence REAL NOT NULL DEFAULT 0.0,
emotional_labels TEXT NOT NULL DEFAULT '',
cloud9_achieved INTEGER NOT NULL DEFAULT 0,
importance REAL NOT NULL DEFAULT 0.5,  -- NEW: For prioritization (0.0-1.0)
access_count INTEGER NOT NULL DEFAULT 0,  -- NEW: LRU tracking
last_accessed TEXT,  -- NEW: For expiration/promotion decisions
parent_id TEXT,
related_ids TEXT NOT NULL DEFAULT '',
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
file_path TEXT NOT NULL,
content_hash TEXT NOT NULL DEFAULT ''
);

-- Core indexes
CREATE INDEX IF NOT EXISTS idx_layer ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intensity ON memories(emotional_intensity DESC);
CREATE INDEX IF NOT EXISTS idx_source ON memories(source);
CREATE INDEX IF NOT EXISTS idx_parent ON memories(parent_id);

-- NEW: Date-based indexes for lazy loading
CREATE INDEX IF NOT EXISTS idx_date_layer ON memories(DATE(created_at), layer);
CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance DESC);
CREATE INDEX IF NOT EXISTS idx_accessed ON memories(last_accessed DESC);
CREATE INDEX IF NOT EXISTS idx_access_count ON memories(access_count DESC);
CREATE INDEX IF NOT EXISTS idx_content_hash ON memories(content_hash);

-- Sync failure tracking table
CREATE TABLE IF NOT EXISTS sync_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    error TEXT,
    failed_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sync_failures_memory ON sync_failures(memory_id);

-- NEW: View for active context (today + recent summaries)
CREATE VIEW IF NOT EXISTS active_memories AS
SELECT
    id, title, summary, content_preview, tags, layer, created_at,
    importance, access_count,
    CASE
        WHEN DATE(created_at) = CURRENT_DATE THEN 'today'
        WHEN DATE(created_at) = DATE('now', '-1 day') THEN 'yesterday'
        WHEN DATE(created_at) >= DATE('now', '-7 days') THEN 'week'
        ELSE 'historical'
    END as context_tier
FROM memories
WHERE created_at >= DATE('now', '-30 days')
ORDER BY
    context_tier,
    importance DESC,
    access_count DESC;
"""

# Reason: 150 chars is enough for an agent to decide if it needs the full memory.
CONTENT_PREVIEW_LENGTH = 150


class SQLiteBackend(BaseBackend):
    """SQLite-indexed file storage for fast queries with full JSON on demand.

    Args:
        base_path: Root directory for memory storage and index.
    """

    def __init__(self, base_path: str = DEFAULT_BASE_PATH) -> None:
        self.base_path = Path(base_path)
        self._ensure_dirs()
        self._db_path = self.base_path / "index.db"
        self._conn: sqlite3.Connection | None = None
        self._ensure_db()

    def _ensure_dirs(self) -> None:
        """Create layer directories if they don't exist."""
        for layer in MemoryLayer:
            (self.base_path / layer.value).mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the SQLite connection.

        A ``busy_timeout`` is set so concurrent writers (Syncthing peers,
        another agent process) wait briefly instead of immediately raising
        "database is locked".

        Returns:
            sqlite3.Connection: Active database connection.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=_BUSY_TIMEOUT_S,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _reset_conn(self) -> None:
        """Drop the cached connection so the next call reopens cleanly."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def _quarantine_corrupt_db(self) -> None:
        """Move an unreadable index.db (and WAL/SHM) aside so it can be rebuilt.

        The flat JSON files are the source of truth, so discarding the index
        is always safe. The corrupt file is renamed rather than deleted so an
        operator can inspect it.
        """
        self._reset_conn()
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self._db_path) + suffix)
            if src.exists():
                dest = Path(f"{self._db_path}.corrupt-{stamp}{suffix}")
                try:
                    src.rename(dest)
                    logger.error("sqlite_backend: quarantined corrupt index %s -> %s", src, dest)
                except OSError as e:
                    logger.error("sqlite_backend: could not quarantine %s: %s", src, e)
                    # Last resort: remove so a fresh DB can be created.
                    with contextlib.suppress(OSError):
                        src.unlink()

    def _recover_from_corruption(self, err: Exception) -> None:
        """Quarantine a corrupt index and rebuild it from the flat JSON files."""
        logger.error(
            "sqlite_backend: index.db corrupt (%s); quarantining and rebuilding "
            "from flat files (source of truth)",
            err,
        )
        self._quarantine_corrupt_db()
        self._init_schema()
        try:
            self.reindex(force=True)
        except Exception as e:  # pragma: no cover - rebuild is best-effort
            logger.warning("sqlite_backend: rebuild after corruption failed: %s", e)

    def _ensure_db(self) -> None:
        """Initialize the schema, recovering if the index is corrupt.

        A corrupt/unreadable index.db must not make the whole backend
        unusable — the flat JSON files still hold every memory. On corruption
        we quarantine the bad file and rebuild. A transient lock is re-raised
        (it is not a reason to discard the index).
        """
        try:
            self._init_schema()
        except sqlite3.DatabaseError as e:
            if not _is_corruption(e):
                raise
            self._recover_from_corruption(e)

    def _init_schema(self) -> None:
        """Create/migrate the schema on the current connection."""
        conn = self._get_conn()

        # Migrate: add columns that may be missing from older schemas.
        # We check pragma_table_info first so this is safe on fresh DBs too.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if row is not None:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
            migrations = [
                (
                    "importance",
                    "ALTER TABLE memories ADD COLUMN importance REAL NOT NULL DEFAULT 0.5",
                ),
                (
                    "access_count",
                    "ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0",
                ),
                ("last_accessed", "ALTER TABLE memories ADD COLUMN last_accessed TEXT"),
                (
                    "content_hash",
                    "ALTER TABLE memories ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
                ),
            ]
            for col, ddl in migrations:
                if col not in existing:
                    conn.execute(ddl)
            conn.commit()

            # Drop stale view so CREATE VIEW IF NOT EXISTS picks up new columns.
            conn.execute("DROP VIEW IF EXISTS active_memories")
            conn.commit()

        conn.executescript(_SCHEMA)
        conn.commit()

    def _file_path(self, memory: Memory) -> Path:
        """Get the file path for a memory.

        Args:
            memory: The memory to locate.

        Returns:
            Path: Full path to the JSON file.
        """
        return self.base_path / memory.layer.value / f"{memory.id}.json"

    def _find_file(self, memory_id: str) -> Path | None:
        """Locate a memory file using the index first, then fallback.

        Args:
            memory_id: The memory ID to find.

        Returns:
            Optional[Path]: Path to the file if found.
        """
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT file_path FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row:
                path = Path(row["file_path"])
                if path.exists():
                    return path
        except sqlite3.Error as e:
            # Index unavailable — fall back to scanning the flat files, which
            # are the source of truth.
            logger.warning("sqlite_backend: _find_file index lookup failed (%s)", e)

        for layer in MemoryLayer:
            path = self.base_path / layer.value / f"{memory_id}.json"
            if path.exists():
                return path
        return None

    def _index_memory(self, memory: Memory, file_path: Path) -> bool:
        """Insert or update the index entry for a memory.

        The flat JSON file is the source of truth and is written before this
        is called, so an index write failure is logged and swallowed rather
        than propagated — the memory is still persisted and recoverable via
        ``reindex()``.

        Args:
            memory: The memory to index.
            file_path: Where the JSON file lives.

        Returns:
            bool: True if the index was updated, False if it degraded.
        """
        content_preview = memory.content[:CONTENT_PREVIEW_LENGTH]
        try:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO memories (
                    id, title, layer, role, tags, source, source_ref,
                    summary, content_preview, emotional_intensity,
                    emotional_valence, emotional_labels, cloud9_achieved,
                    parent_id, related_ids, created_at, updated_at,
                    file_path, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory.id,
                    memory.title,
                    memory.layer.value,
                    memory.role.value,
                    ",".join(memory.tags),
                    memory.source,
                    memory.source_ref,
                    memory.summary,
                    content_preview,
                    memory.emotional.intensity,
                    memory.emotional.valence,
                    ",".join(memory.emotional.labels),
                    1 if memory.emotional.cloud9_achieved else 0,
                    memory.parent_id,
                    ",".join(memory.related_ids),
                    memory.created_at,
                    memory.updated_at,
                    str(file_path),
                    memory.content_hash(),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.warning(
                "sqlite_backend: index write failed for %s (flat file intact): %s",
                memory.id,
                e,
            )
            if _is_corruption(e):
                try:
                    self._recover_from_corruption(e)
                except Exception as rec_err:  # pragma: no cover - defensive
                    logger.error("sqlite_backend: recovery failed: %s", rec_err)
            return False

    def _row_to_memory_summary(self, row: sqlite3.Row) -> dict:
        """Convert a database row to a lightweight memory summary dict.

        This is the token-efficient representation: no full content,
        just enough for an agent to decide if it needs more.

        Args:
            row: SQLite row.

        Returns:
            dict: Lightweight memory summary.
        """

        def _keys(row: sqlite3.Row) -> set[str]:
            try:
                return set(row.keys())
            except Exception:
                return set()

        def _get(key: str, default=None):
            # Tolerate schema drift: a row from an older/newer index may be
            # missing columns this code expects.
            if key in cols:
                return row[key]
            return default

        def _csv(key: str) -> list[str]:
            # Tolerate NULL and non-string values from malformed rows.
            val = _get(key, "")
            if not isinstance(val, str):
                return []
            return [item for item in val.split(",") if item]

        cols = _keys(row)
        return {
            "id": _get("id"),
            "title": _get("title"),
            "layer": _get("layer"),
            "role": _get("role"),
            "tags": _csv("tags"),
            "source": _get("source"),
            "summary": _get("summary"),
            "content_preview": _get("content_preview"),
            "emotional_intensity": _get("emotional_intensity"),
            "emotional_valence": _get("emotional_valence"),
            "emotional_labels": _csv("emotional_labels"),
            "cloud9_achieved": bool(_get("cloud9_achieved")),
            "created_at": _get("created_at"),
            "parent_id": _get("parent_id"),
            "related_ids": _csv("related_ids"),
        }

    def _row_to_memory(self, row: sqlite3.Row) -> Memory | None:
        """Load the full Memory object from disk using the index path.

        Args:
            row: SQLite row with file_path.

        Returns:
            Optional[Memory]: Full memory object, or None if file missing.
        """
        path = Path(row["file_path"])
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Memory(**data)
        except (json.JSONDecodeError, Exception):
            return None

    def save(self, memory: Memory) -> str:
        """Persist a memory as JSON and update the index.

        The flat JSON file is written first (it is the source of truth). If
        the index update fails, the save still succeeds — the memory is on
        disk and will be picked up by the next ``reindex()``.

        Args:
            memory: The Memory to store.

        Returns:
            str: The memory ID.
        """
        path = self._file_path(memory)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(memory.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
        self._index_memory(memory, path)  # non-fatal: degrades gracefully
        return memory.id

    def load(self, memory_id: str) -> Memory | None:
        """Load a memory by ID, using the index for fast lookup.

        Args:
            memory_id: The memory identifier.

        Returns:
            Optional[Memory]: The memory if found, None otherwise.
        """
        path = self._find_file(memory_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Memory(**data)
        except (json.JSONDecodeError, Exception):
            return None

    def delete(self, memory_id: str) -> bool:
        """Delete a memory file and its index entry.

        Args:
            memory_id: The memory identifier.

        Returns:
            bool: True if deleted, False if not found.
        """
        path = self._find_file(memory_id)

        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
        except sqlite3.Error as e:
            # Index removal failed, but the flat file (source of truth) is
            # deleted below. A later reindex will drop the stale row.
            logger.warning("sqlite_backend: index delete failed for %s: %s", memory_id, e)

        if path is None:
            return False
        if path.exists():
            path.unlink()
        return True

    @_resilient_read(list)
    def list_memories(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories using the index for filtering, loading full objects.

        Args:
            layer: Filter by memory layer (None = all layers).
            tags: Filter by tags (AND logic).
            limit: Maximum results.

        Returns:
            list[Memory]: Matching memories sorted newest first.
        """
        conn = self._get_conn()
        conditions = []
        params: list = []

        if layer:
            conditions.append("layer = ?")
            params.append(layer.value)

        if tags:
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

        results = []
        for row in rows:
            mem = self._row_to_memory(row)
            if mem is not None:
                results.append(mem)
        return results

    @_resilient_read(list)
    def list_summaries(
        self,
        layer: MemoryLayer | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        min_intensity: float = 0.0,
        order_by: str = "created_at",
    ) -> list[dict]:
        """List memory summaries from the index only (no file I/O).

        This is the token-efficient path: returns lightweight dicts
        with title, summary, preview, and emotional data — no full content.
        Use this for agent context loading.

        Args:
            layer: Filter by memory layer.
            tags: Filter by tags (AND logic).
            limit: Maximum results.
            min_intensity: Minimum emotional intensity filter.
            order_by: Sort field ('created_at' or 'emotional_intensity').

        Returns:
            list[dict]: Lightweight memory summaries.
        """
        conn = self._get_conn()
        conditions = []
        params: list = []

        if layer:
            conditions.append("layer = ?")
            params.append(layer.value)

        if tags:
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")

        if min_intensity > 0:
            conditions.append("emotional_intensity >= ?")
            params.append(min_intensity)

        where = " AND ".join(conditions) if conditions else "1=1"

        if order_by == "recency_weighted_intensity":
            # Combine intensity with recency: recent high-intensity memories
            # score higher than old high-intensity ones.
            # julianday('now') - julianday(created_at) gives days ago.
            # Decay: halve the recency bonus every 7 days.
            order = (
                "(emotional_intensity + "
                "CASE WHEN julianday('now') - julianday(created_at) < 1 THEN 5.0 "
                "WHEN julianday('now') - julianday(created_at) < 3 THEN 3.0 "
                "WHEN julianday('now') - julianday(created_at) < 7 THEN 1.5 "
                "ELSE 0.0 END) DESC"
            )
        elif order_by == "recency_weighted":
            # Time-decay ranking: importance decays by layer-specific half-life
            order = (
                "(importance * POWER(0.5, CAST(julianday('now') - julianday(created_at) AS REAL) / "
                "CASE layer WHEN 'short-term' THEN 7 WHEN 'mid-term' THEN 30 ELSE 365 END)) DESC"
            )
        elif order_by == "emotional_intensity":
            order = "emotional_intensity DESC"
        else:
            order = "created_at DESC"

        params.append(limit)

        rows = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY {order} LIMIT ?",
            params,
        ).fetchall()

        return [self._row_to_memory_summary(row) for row in rows]

    @_resilient_read(list)
    def search_text(self, query: str, limit: int = 10) -> list[Memory]:
        """Search memories using the SQLite index (title, summary, preview).

        For multi-word queries, tries AND first (all words must match).
        If AND returns nothing, falls back to OR (any word matches),
        ranked by how many query words each memory contains.

        Args:
            query: Search string.
            limit: Maximum results.

        Returns:
            list[Memory]: Matching memories.
        """
        conn = self._get_conn()
        words = query.split()

        if not words:
            return []

        cols = ["title", "summary", "content_preview", "tags"]

        def _word_clause(word: str) -> str:
            return "(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")"

        def _word_params(word: str) -> list[str]:
            pattern = f"%{word}%"
            return [pattern] * len(cols)

        # Try AND first: all words must match
        and_clauses = [_word_clause(w) for w in words]
        and_params: list[str] = []
        for w in words:
            and_params.extend(_word_params(w))
        and_params.append(str(limit))

        rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE {" AND ".join(and_clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            and_params,
        ).fetchall()

        if not rows and len(words) > 1:
            # Fall back to OR: any word matches, ranked by match count
            or_clauses = [_word_clause(w) for w in words]
            # Use SUM of CASE expressions to count matching words
            score_expr = " + ".join(
                f"CASE WHEN {_word_clause(w)} THEN 1 ELSE 0 END" for w in words
            )
            or_params: list[str] = []
            # params for WHERE (OR)
            for w in words:
                or_params.extend(_word_params(w))
            # params for ORDER BY score (same patterns again)
            for w in words:
                or_params.extend(_word_params(w))
            or_params.append(str(limit))

            rows = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE {" OR ".join(or_clauses)}
                ORDER BY ({score_expr}) DESC, created_at DESC
                LIMIT ?
                """,
                or_params,
            ).fetchall()

        results = []
        for row in rows:
            mem = self._row_to_memory(row)
            if mem is not None:
                results.append(mem)
        return results

    @_resilient_read(list)
    def get_related(self, memory_id: str, depth: int = 1) -> list[dict]:
        """Get related memories by traversing related_ids (shallow graph).

        Args:
            memory_id: Starting memory ID.
            depth: How many hops to follow (1 = direct relations).

        Returns:
            list[dict]: Related memory summaries.
        """
        conn = self._get_conn()
        visited: set[str] = {memory_id}
        frontier: list[str] = []
        results: list[dict] = []

        # Reason: seed the frontier from the starting node's relationships
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return results

        related = [r for r in row["related_ids"].split(",") if r]
        frontier.extend(r for r in related if r not in visited)
        if row["parent_id"] and row["parent_id"] not in visited:
            frontier.append(row["parent_id"])

        for _ in range(depth):
            next_frontier: list[str] = []
            for mid in frontier:
                if mid in visited:
                    continue
                visited.add(mid)

                neighbor = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
                if neighbor is None:
                    continue

                results.append(self._row_to_memory_summary(neighbor))

                child_related = [r for r in neighbor["related_ids"].split(",") if r]
                next_frontier.extend(r for r in child_related if r not in visited)
                if neighbor["parent_id"] and neighbor["parent_id"] not in visited:
                    next_frontier.append(neighbor["parent_id"])

            frontier = next_frontier

        return results

    def list_backups(self, backup_dir: str | None = None) -> list[dict]:
        """List all skmemory backup files, sorted newest first.

        Args:
            backup_dir: Directory to scan. Defaults to
                ``<base_path>/../backups/``.

        Returns:
            list[dict]: Backup entries, newest first. Each entry has:
                ``path``, ``name``, ``size_bytes``, ``date``.
        """
        bdir = self.base_path.parent / "backups" if backup_dir is None else Path(backup_dir)

        if not bdir.exists():
            return []

        entries = []
        for f in sorted(bdir.glob("skmemory-backup-*.json"), reverse=True):
            entries.append(
                {
                    "path": str(f),
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "date": f.stem.replace("skmemory-backup-", ""),
                }
            )
        return entries

    def prune_backups(self, keep: int = 7, backup_dir: str | None = None) -> list[str]:
        """Delete oldest backups, retaining only the N most recent.

        Args:
            keep: Number of most-recent backups to keep (default: 7).
            backup_dir: Directory to prune. Defaults to
                ``<base_path>/../backups/``.

        Returns:
            list[str]: Paths of the deleted backup files.
        """
        backups = self.list_backups(backup_dir)
        to_delete = backups[keep:]  # list is already newest-first
        deleted: list[str] = []
        for entry in to_delete:
            try:
                Path(entry["path"]).unlink()
                deleted.append(entry["path"])
            except OSError:
                pass
        return deleted

    def export_all(self, output_path: str | None = None) -> str:
        """Export all memories as a single JSON file for backup.

        Reads every JSON file on disk and bundles them into one
        git-friendly backup. One file per day by default (overwrites
        same-day exports). When using the default backup directory,
        automatically prunes to keep the last 7 daily backups.

        Args:
            output_path: Where to write the backup. If None, uses
                ``~/.skcapstone/backups/skmemory-backup-YYYY-MM-DD.json``
                and triggers automatic rotation (keep last 7).

        Returns:
            str: Path to the written backup file.
        """
        from datetime import date as _date

        _auto_rotate = output_path is None

        if output_path is None:
            backup_dir = self.base_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(backup_dir / f"skmemory-backup-{_date.today().isoformat()}.json")

        memories: list[dict] = []
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in sorted(layer_dir.glob("*.json")):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    memories.append(data)
                except (json.JSONDecodeError, Exception):
                    continue

        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from .. import __version__

        payload = {
            "skmemory_version": __version__,
            "exported_at": _dt.now(_tz.utc).isoformat(),
            "memory_count": len(memories),
            "base_path": str(self.base_path),
            "memories": memories,
        }

        Path(output_path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        if _auto_rotate:
            self.prune_backups(keep=7)

        return output_path

    def import_backup(self, backup_path: str) -> int:
        """Restore memories from a JSON backup file.

        Each memory is written as a JSON file in its layer directory
        and re-indexed. Existing memories with the same ID are overwritten.

        Args:
            backup_path: Path to the backup JSON file.

        Returns:
            int: Number of memories restored.

        Raises:
            FileNotFoundError: If backup_path does not exist.
            ValueError: If the file is not a valid skmemory backup.
        """
        path = Path(backup_path)
        if not path.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")

        data = json.loads(path.read_text(encoding="utf-8"))

        if "memories" not in data or not isinstance(data["memories"], list):
            raise ValueError("Invalid backup file: missing 'memories' array")

        count = 0
        for mem_data in data["memories"]:
            try:
                memory = Memory(**mem_data)
                file_path = self._file_path(memory)
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(
                    json.dumps(memory.model_dump(), indent=2, default=str),
                    encoding="utf-8",
                )
                self._index_memory(memory, file_path)
                count += 1
            except Exception as e:
                logger.warning("sqlite_backend.py: %s", e)
                continue

        return count

    def export_orphans_to_flat(self) -> dict:
        """Write any SQLite-only memories out as flat JSON files.

        SQLite-only = present in the index but with no flat .json file at
        ``base_path/<layer>/<id>.json`` (or 12-char shortform). Reconstructs
        each orphan from its SQLite row and writes a Memory JSON. Note:
        SQLite stores ``content_preview`` (~150 chars), not full content —
        recovered memories carry ``metadata.recovered_from_sqlite_preview =
        True`` so consumers know the content was truncated at recovery time.

        Safe: non-destructive, idempotent.

        Returns:
            dict: ``{"exported": N, "skipped": M, "errors": K, "orphan_ids": [...]}``.
        """
        from .file_backend import FileBackend

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, title, layer, role, tags, source, source_ref, summary, "
            "content_preview, emotional_intensity, emotional_valence, "
            "emotional_labels, importance, parent_id, related_ids, "
            "created_at, updated_at FROM memories"
        ).fetchall()

        fb = FileBackend(base_path=str(self.base_path))
        stats = {"exported": 0, "skipped": 0, "errors": 0, "orphan_ids": []}
        # Memories moved to the archive tree are intentional cold storage —
        # never resurrect them into an active tier as a truncated preview stub.
        archived_stems = self._archived_stems()

        for row in rows:
            (
                memory_id,
                title,
                layer,
                role,
                tags,
                source,
                source_ref,
                summary,
                content_preview,
                e_intensity,
                e_valence,
                e_labels,
                importance,
                parent_id,
                related_ids,
                created_at,
                updated_at,
            ) = row

            short = memory_id[:12].replace("-", "")
            full_path = self.base_path / layer / f"{memory_id}.json"
            short_path = self.base_path / layer / f"{short}.json"
            if full_path.exists() or short_path.exists():
                stats["skipped"] += 1
                continue
            if memory_id in archived_stems or short in archived_stems:
                # Archived (cold storage); the full-content flat file already
                # lives under memory/archive/ — leave it there.
                stats["skipped"] += 1
                continue

            try:
                mem = Memory(
                    id=memory_id,
                    title=title or "Recovered memory",
                    content=(content_preview or "")
                    + "\n\n[recovered from SQLite preview — full content lost]",
                    summary=summary or "",
                    layer=layer,
                    role=role or "general",
                    tags=[t for t in (tags or "").split(",") if t.strip()],
                    source=source or "manual",
                    source_ref=source_ref or "",
                    emotional={
                        "intensity": e_intensity or 0.0,
                        "valence": e_valence or 0.0,
                        "labels": [lbl for lbl in (e_labels or "").split(",") if lbl.strip()],
                    },
                    related_ids=[r for r in (related_ids or "").split(",") if r.strip()],
                    parent_id=parent_id or None,
                    created_at=created_at,
                    updated_at=updated_at,
                    metadata={
                        "recovered_from_sqlite_preview": True,
                        "importance": importance or 0.5,
                    },
                )
                fb.save(mem)
                stats["exported"] += 1
                stats["orphan_ids"].append(memory_id)
            except Exception as e:
                logger.warning("sqlite_backend.py: %s", e)
                stats["errors"] += 1

        return stats

    def reindex(self, force: bool = False) -> int:
        """Rebuild the entire index from JSON files on disk.

        DESTRUCTIVE: deletes every row in the index, then re-reads flat files.
        Any SQLite-only memories (with no backing flat file) are dropped.

        Safety: by default, ``export_orphans_to_flat()`` runs first to write
        SQLite-only entries to disk, so they survive the rebuild. Pass
        ``force=True`` to skip that step (the old destructive behavior).

        Use this after manual file edits or migration from FileBackend.

        Args:
            force: If True, skip orphan export before rebuilding.

        Returns:
            int: Number of memories indexed.
        """
        if not force:
            self.export_orphans_to_flat()

        conn = self._get_conn()
        conn.execute("DELETE FROM memories")
        conn.commit()

        count = 0
        for layer in MemoryLayer:
            layer_dir = self.base_path / layer.value
            if not layer_dir.exists():
                continue
            for json_file in layer_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    memory = Memory(**data)
                    self._index_memory(memory, json_file)
                    count += 1
                except (json.JSONDecodeError, Exception):
                    continue

        return count

    def stats(self) -> dict:
        """Get index statistics.

        Returns:
            dict: Counts by layer, total, and index size.
        """
        conn = self._get_conn()

        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        layer_counts = {}
        for layer in MemoryLayer:
            count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE layer = ?",
                (layer.value,),
            ).fetchone()[0]
            layer_counts[layer.value] = count

        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0

        return {
            "total": total,
            "by_layer": layer_counts,
            "index_size_bytes": db_size,
            "index_path": str(self._db_path),
        }

    def _archived_stems(self) -> set[str]:
        """Stems (filename without .json) of all memories in the archive tree.

        The memory promoter (skcapstone.memory_promoter) moves aged/duplicate
        memories out of the active tiers into ``memory/archive/`` and
        ``memory/archive/deduped/``. Those are intentional cold storage, NOT
        orphans — so drift/orphan accounting must recognize them. Returns both
        the full-uuid stem and the 12-char shortform for each archived file.
        """
        stems: set[str] = set()
        archive_root = self.base_path / "archive"
        if not archive_root.exists():
            return stems
        for f in archive_root.rglob("*.json"):
            stems.add(f.stem)
        return stems

    def drift_check(self) -> dict:
        """Compare SQLite row count vs. flat-file count per layer.

        Returns counts and the drift directions:
            sqlite_only — rows whose flat file is truly gone (not in a tier and
                not in the archive tree); recoverable via export_orphans_to_flat.
            flat_only   — flat files whose id is not indexed in SQLite
                (recoverable via reindex).
            archived    — rows whose flat file has been moved to memory/archive/
                (intentional cold storage; NOT drift).

        Returns:
            dict: ``{"in_sync": bool, "sqlite_total", "flat_total",
                    "sqlite_only", "flat_only", "archived", "by_layer": {...}}``.
        """
        conn = self._get_conn()
        sqlite_ids: set[str] = set()
        sqlite_orphans = 0
        archived = 0
        archived_stems = self._archived_stems()
        by_layer = {ml.value: {"sqlite": 0, "flat": 0} for ml in MemoryLayer}

        for memory_id, layer, file_path in conn.execute(
            "SELECT id, layer, file_path FROM memories"
        ):
            sqlite_ids.add(memory_id)
            if layer in by_layer:
                by_layer[layer]["sqlite"] += 1
            if not Path(file_path).exists():
                # Also tolerate the 12-char shortform path
                short = memory_id[:12].replace("-", "")
                if (self.base_path / layer / f"{short}.json").exists():
                    continue
                # Moved to the archive tree ⇒ cold storage, not an orphan.
                if memory_id in archived_stems or short in archived_stems:
                    archived += 1
                else:
                    sqlite_orphans += 1

        flat_ids: set[str] = set()
        for layer in MemoryLayer:
            tier = self.base_path / layer.value
            if not tier.exists():
                continue
            for f in tier.glob("*.json"):
                flat_ids.add(f.stem)
                by_layer[layer.value]["flat"] += 1

        # flat_only = files whose stem isn't in SQLite (allow shortform → fullform match)
        sqlite_short = {sid[:12].replace("-", "") for sid in sqlite_ids}
        flat_only = sum(
            1 for stem in flat_ids if stem not in sqlite_ids and stem not in sqlite_short
        )

        return {
            "in_sync": sqlite_orphans == 0 and flat_only == 0,
            "sqlite_total": len(sqlite_ids),
            "flat_total": len(flat_ids),
            "sqlite_only": sqlite_orphans,
            "flat_only": flat_only,
            "archived": archived,
            "by_layer": by_layer,
        }

    def prune_archived(self) -> int:
        """Delete index rows for memories that have been moved to archive/.

        The memory promoter moves aged/duplicate memories into the archive tree
        and removes them from the active store. Their SQLite index rows point at
        a now-nonexistent tier path — cold storage should not appear in the
        active index. This deletes exactly those rows (a row is only pruned when
        its flat file is absent from its tier *and* its id is present in the
        archive tree), leaving genuinely-missing orphans untouched.

        Returns:
            int: Number of stale archived rows removed from the index.
        """
        conn = self._get_conn()
        archived_stems = self._archived_stems()
        if not archived_stems:
            return 0

        to_delete: list[str] = []
        for memory_id, layer, file_path in conn.execute(
            "SELECT id, layer, file_path FROM memories"
        ):
            if Path(file_path).exists():
                continue
            short = memory_id[:12].replace("-", "")
            if (self.base_path / layer / f"{short}.json").exists():
                continue
            if memory_id in archived_stems or short in archived_stems:
                to_delete.append(memory_id)

        for mid in to_delete:
            conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        if to_delete:
            conn.commit()
        return len(to_delete)

    def health_check(self) -> dict:
        """Check SQLite backend health.

        Returns:
            dict: Status with path, counts, index info, and drift summary.
        """
        try:
            s = self.stats()
            drift = self.drift_check()
            return {
                "ok": True,
                "backend": "SQLiteBackend",
                "base_path": str(self.base_path),
                "total_memories": s["total"],
                "by_layer": s["by_layer"],
                "index_size_bytes": s["index_size_bytes"],
                "sync": {
                    "in_sync": drift["in_sync"],
                    "sqlite_only": drift["sqlite_only"],
                    "flat_only": drift["flat_only"],
                    "hint": (None if drift["in_sync"] else "Run `skmemory sync` to reconcile."),
                },
            }
        except Exception as e:
            logger.warning("sqlite_backend.py: %s", e)
            return {
                "ok": False,
                "backend": "SQLiteBackend",
                "error": str(e),
            }

    def find_by_content_hash(self, content_hash: str) -> Memory | None:
        """Find an existing memory by content hash. Returns None if not found."""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT file_path FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            # Load the full memory from its flat file
            file_path = Path(row[0])
            if file_path.exists():
                return Memory.model_validate_json(file_path.read_text())
        except Exception as e:
            logger.warning("find_by_content_hash failed: %s", e)
        return None

    def record_sync_failure(self, memory_id: str, backend: str, error: str):
        """Record a sync failure for a memory to a backend."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO sync_failures (memory_id, backend, error, failed_at, retry_count) VALUES (?, ?, ?, ?, 0)",
            (memory_id, backend, error, datetime.utcnow().isoformat()),
        )
        conn.commit()

    def get_sync_failures(self, backend: str | None = None, limit: int = 100) -> list[dict]:
        """Get sync failures, optionally filtered by backend."""
        conn = self._get_conn()
        if backend:
            cursor = conn.execute(
                "SELECT memory_id, backend, error, failed_at, retry_count FROM sync_failures WHERE backend = ? ORDER BY failed_at DESC LIMIT ?",
                (backend, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT memory_id, backend, error, failed_at, retry_count FROM sync_failures ORDER BY failed_at DESC LIMIT ?",
                (limit,),
            )
        return [
            {
                "memory_id": r[0],
                "backend": r[1],
                "error": r[2],
                "failed_at": r[3],
                "retry_count": r[4],
            }
            for r in cursor.fetchall()
        ]

    def clear_sync_failure(self, memory_id: str, backend: str):
        """Clear a sync failure record after successful sync."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM sync_failures WHERE memory_id = ? AND backend = ?", (memory_id, backend)
        )
        conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
