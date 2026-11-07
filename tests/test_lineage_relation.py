"""Promotion and chunking must not close the parent's validity (card 32e2b5fa).

No database: a recording stub stands in for the AGE cypher layer.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from skmemory.backends.age_backend import AGEGraphBackend
from skmemory.lineage_repair import repair_lineage_edges
from skmemory.models import (
    PARENT_RELATION_KEY,
    Memory,
    parent_relation_of,
)


def _mem(**kw) -> Memory:
    return Memory(id=str(uuid.uuid4()), title="t", content="c", **kw)


def _backend() -> AGEGraphBackend:
    b = object.__new__(AGEGraphBackend)
    b.graph = "g"
    b.agent = "lumina"
    b.queries = []

    def cypher(query, params=None, cols="v agtype", timeout_ms=None):
        b.queries.append(query)
        # path walks get no rows; everything else a truthy row
        return [] if "path =" in query else [("x",)]

    b._cypher = cypher
    return b


def _edge_queries(b: AGEGraphBackend) -> str:
    return "\n".join(b.queries)


def test_promote_records_relation_and_survives_json_roundtrip():
    parent = _mem()
    child = parent.promote("long-term")
    assert child.metadata[PARENT_RELATION_KEY] == "promoted_from"
    back = Memory.model_validate(json.loads(json.dumps(child.model_dump(mode="json"))))
    assert parent_relation_of(back.parent_id, back.metadata) == "promoted_from"
    assert PARENT_RELATION_KEY not in parent.metadata


def test_promotion_does_not_write_supersedes():
    parent = _mem()
    b = _backend()
    b.index_memory(parent.promote("long-term"))
    q = _edge_queries(b)
    assert "PROMOTED_FROM" in q
    assert "SUPERSEDES" not in q


def test_chunk_writes_chunk_of_only():
    b = _backend()
    b.index_memory(_mem(parent_id="p1", metadata={"decomposition": {"parent_id": "p1"}}))
    q = _edge_queries(b)
    assert "CHUNK_OF" in q and "SUPERSEDES" not in q


def test_explicit_supersedes_still_closes_parent():
    b = _backend()
    b.index_memory(_mem(parent_id="p1", metadata={PARENT_RELATION_KEY: "supersedes"}))
    q = _edge_queries(b)
    assert "SUPERSEDES" in q and "valid_to" in q


def test_legacy_bare_parent_is_never_supersedes():
    assert parent_relation_of("p", {}) == "promoted_from"
    assert parent_relation_of(None, {}) is None


class _RepairBackend:
    agent = "lumina"

    def __init__(self, edges):
        self._edges = edges
        self.converted = []

    def probe_connection(self):
        pass

    def supersedes_edges(self):
        return self._edges

    def convert_supersedes_to_lineage(self, child, parent, rel):
        self.converted.append((child, parent, rel))


def _flat(base: Path, mem: Memory):
    d = base / "long-term"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mem.id}.json").write_text(json.dumps(mem.model_dump(mode="json")))


def _setup(tmp_path):
    mem_dir = tmp_path / "memory"
    parent = _mem()
    promo = _mem(parent_id=parent.id)
    chunk = _mem(parent_id=parent.id, metadata={"decomposition": {"parent_id": parent.id}})
    real = _mem(parent_id=parent.id, metadata={PARENT_RELATION_KEY: "supersedes"})
    for m in (parent, promo, chunk, real):
        _flat(mem_dir, m)
    edges = [
        {"child": m.id, "parent": parent.id, "properties": {}} for m in (promo, chunk, real)
    ] + [{"child": "ghost", "parent": parent.id, "properties": {}}]
    return mem_dir, edges, promo, chunk, real


def test_repair_dry_run_counts_and_changes_nothing(tmp_path):
    mem_dir, edges, *_ = _setup(tmp_path)
    b = _RepairBackend(edges)
    stats = repair_lineage_edges(b, mem_dir)
    assert stats["dry_run"] is True
    assert (stats["to_promoted_from"], stats["to_chunk_of"]) == (1, 1)
    assert stats["kept_explicit"] == 1 and stats["skipped_unknown"] == 1
    assert b.converted == [] and stats["backup_path"] is None


def test_repair_apply_backs_up_then_rewrites_only_misclassified(tmp_path):
    mem_dir, edges, promo, chunk, real = _setup(tmp_path)
    b = _RepairBackend(edges)
    stats = repair_lineage_edges(b, mem_dir, dry_run=False)
    assert sorted(b.converted) == sorted(
        [
            (promo.id, promo.parent_id, "PROMOTED_FROM"),
            (chunk.id, chunk.parent_id, "CHUNK_OF"),
        ]
    )
    assert stats["rewritten"] == 2
    assert len(json.loads(Path(stats["backup_path"]).read_text())) == 2
    # idempotent: once the graph holds no such edges, nothing is left to do
    again = repair_lineage_edges(_RepairBackend([]), mem_dir, dry_run=False)
    assert again["rewritten"] == 0


def test_ingest_document_chunks_record_chunk_relation(tmp_path):
    from skmemory.backends.file_backend import FileBackend
    from skmemory.store import MemoryStore

    store = MemoryStore(primary=FileBackend(base_path=str(tmp_path / "mem")))
    content = "The Internal Revenue Service shall respond promptly. Chef claims it. " * 60
    parent = store.ingest_document("Long doc", content)
    chunk_ids = parent.metadata["chunk_memory_ids"]
    assert len(chunk_ids) >= 2
    for cid in chunk_ids:
        chunk = store.recall(cid)
        assert chunk.parent_id == parent.id
        assert chunk.metadata[PARENT_RELATION_KEY] == "chunk_of"
    assert PARENT_RELATION_KEY not in parent.metadata


def test_walks_follow_lineage_edges_but_chain_stays_supersedes_only():
    b = _backend()
    b.get_related("m1")
    b.get_lineage("m1")
    b.neighbors("m1")
    walked = _edge_queries(b)
    for rel in ("RELATED_TO", "SUPERSEDES", "PROMOTED_FROM", "CHUNK_OF"):
        assert f"[:{rel}*" in walked or f"[e:{rel}]" in walked, rel
    b.queries.clear()
    b.supersedes_chain("m1")
    chain = _edge_queries(b)
    assert "SUPERSEDES" in chain
    assert "PROMOTED_FROM" not in chain and "CHUNK_OF" not in chain


def test_lineage_walk_per_edge_type_queries():
    b = _backend()
    b.get_lineage("m1")
    for rel in ("PROMOTED_FROM", "CHUNK_OF", "SUPERSEDES"):
        assert any(f"[:{rel}*1..10]" in q for q in b.queries), rel


def test_convert_merges_new_edge_before_deleting_old():
    b = _backend()
    b._cypher_strict = lambda q, *a, **k: b.queries.append(q) or []
    b.convert_supersedes_to_lineage("c", "p", "CHUNK_OF")
    assert "MERGE" in b.queries[0] and "CHUNK_OF" in b.queries[0]
    assert "DELETE e" in b.queries[1] and "SUPERSEDES" in b.queries[1]
