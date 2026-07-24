"""End-to-end tests for the Notion importer prototype + admission gates."""

from __future__ import annotations

import json
from pathlib import Path

from skmemory.admission import (
    SENTINEL_UNRECOVERABLE_SOURCE,
    review_queue_path,
)
from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.importers.notion import import_notion, iter_rows
from skmemory.store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    backend = SQLiteBackend(base_path=str(tmp_path / "mem"))
    return MemoryStore(primary=backend)


def _row(rid: str, **overrides):
    base = {
        "row_id": rid,
        "title": f"Page {rid}",
        "content": f"Body of {rid}",
        "source": "notion",
        "tags": ["notion-import"],
    }
    base.update(overrides)
    return base


def _notion_jsonl(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "notion-export.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


# ── AC-A1 — admission runs on the import path ──────────────────────────────


class TestImportPath:
    def test_admitted_row_lands_with_known_source(self, tmp_path: Path):
        store = _store(tmp_path)
        stats = import_notion(
            store,
            tmp_path,
            rows=[_row("r1")],
            agent_home=tmp_path,
        )
        assert stats.seen == 1
        assert stats.admitted == 1
        assert stats.refused == 0

        rows = store.primary.list_memories()
        assert len(rows) == 1
        mem = rows[0]
        assert mem.source == "notion"
        assert mem.metadata["admission_admit"] is True
        assert mem.metadata["admission_excluded_from_retrieval"] is False
        assert "admission:admitted" in mem.tags

    def test_refused_row_lands_under_sentinel(self, tmp_path: Path):
        store = _store(tmp_path)
        stats = import_notion(
            store,
            tmp_path,
            rows=[_row("r2", tags=["egregore"])],
            agent_home=tmp_path,
        )
        assert stats.refused == 1
        assert stats.admitted == 0
        rows = store.primary.list_memories()
        assert len(rows) == 1
        assert rows[0].source == SENTINEL_UNRECOVERABLE_SOURCE
        assert rows[0].metadata["admission_excluded_from_retrieval"] is True
        assert "admission:refused" in rows[0].tags


# ── AC-A3 — policy version stamp + monotonic-tightness review queue ───────


class TestRerunBehavior:
    def test_loosening_blocks_and_queues_review(self, tmp_path: Path):
        # Stored decision: previously refused (admit=False).
        stored = {
            "row-loose": {
                "admission_admit": False,
                "admission_policy_version": "0.9.0",
                "admission_reason": "refuse_collective_echo",
            }
        }
        # Fresh decision: admit (clean known-source row).
        store = _store(tmp_path)
        stats = import_notion(
            store,
            tmp_path,
            rows=[_row("row-loose")],
            agent_home=tmp_path,
            stored_decisions=stored,
        )
        assert stats.queued_for_review == 1
        assert stats.admitted == 0
        # The row was NOT written — review queue holds it instead.
        assert store.primary.list_memories() == []

        queue = review_queue_path(tmp_path)
        assert queue.exists()
        record = json.loads(queue.read_text(encoding="utf-8").strip())
        assert record["row_id"] == "row-loose"
        assert record["importer"] == "notion"
        assert record["new_admit"] is True
        assert record["stored_admit"] is False

    def test_tightening_writes_refused_row(self, tmp_path: Path):
        stored = {
            "row-tight": {
                "admission_admit": True,
                "admission_policy_version": "0.9.0",
                "admission_reason": "admit_known_source",
            }
        }
        store = _store(tmp_path)
        stats = import_notion(
            store,
            tmp_path,
            rows=[_row("row-tight", tags=["egregore"])],
            agent_home=tmp_path,
            stored_decisions=stored,
        )
        assert stats.refused == 1
        assert stats.queued_for_review == 0
        rows = store.primary.list_memories()
        assert len(rows) == 1
        assert rows[0].source == SENTINEL_UNRECOVERABLE_SOURCE


# ── AC-A4 — refused rows are queryable but excluded by metadata flag ───────


class TestAuditability:
    def test_refused_rows_are_persisted_for_audit(self, tmp_path: Path):
        store = _store(tmp_path)
        import_notion(
            store,
            tmp_path,
            rows=[_row("audit-1", tags=["egregore"])],
            agent_home=tmp_path,
        )
        # Direct backend hit returns the row — retrieval-layer filtering
        # happens via the admission_excluded_from_retrieval flag.
        rows = store.primary.list_memories()
        assert len(rows) == 1
        assert rows[0].metadata["admission_excluded_from_retrieval"] is True


# ── Drift smoke test against the importer's policy stamping ────────────────


class TestPolicyStamping:
    def test_admitted_row_carries_policy_version(self, tmp_path: Path):
        from skmemory.admission import ADMISSION_POLICY_VERSION

        store = _store(tmp_path)
        import_notion(
            store,
            tmp_path,
            rows=[_row("stamp-1")],
            agent_home=tmp_path,
        )
        rows = store.primary.list_memories()
        assert rows[0].metadata["admission_policy_version"] == ADMISSION_POLICY_VERSION


# ── iter_rows source-format dispatch ───────────────────────────────────────


class TestIterRows:
    def test_jsonl_dispatch(self, tmp_path: Path):
        path = _notion_jsonl(tmp_path, [_row("a"), _row("b")])
        out = list(iter_rows(path))
        assert [r["row_id"] for r in out] == ["a", "b"]

    def test_markdown_dir_dispatch(self, tmp_path: Path):
        export = tmp_path / "export"
        export.mkdir()
        (export / "Page One.md").write_text("# hello\n", encoding="utf-8")
        (export / "Page Two.md").write_text("body\n", encoding="utf-8")
        rows = list(iter_rows(export))
        assert len(rows) == 2
        assert all(r["source"] == "notion" for r in rows)
