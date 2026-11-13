"""`skmemory fortress reseal`: re-seal only memories PROVEN to be victims of a fixed bug.

Dry-run by default. Writing needs an explicit flag, every re-seal is logged, and a
memory that is really changed (content edited, parent unrelated, sidecar present) is
never touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer
from skmemory.reseal import (
    REASON_PROMOTED_COPY,
    REASON_UNSTRIPPED_TITLE,
    find_victims,
    reseal,
)
from skmemory.store import MemoryStore
from tests.tamper import tamper_on_disk


@pytest.fixture()
def backend(tmp_path: Path) -> SQLiteBackend:
    return SQLiteBackend(base_path=tmp_path / "memories")


def _seal_over(m: Memory, title: str) -> str:
    payload = f"{m.id}:{title}:{m.content}:{m.emotional.signature()}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _unstripped_title_victim(backend, title="Observation: x ", suffix=" ") -> Memory:
    """A memory sealed over a title with surrounding whitespace the model strips on load."""
    m = Memory(title="Observation: x", content="body", layer=MemoryLayer.MID)
    m.integrity_hash = _seal_over(m, "Observation: x" + suffix)
    backend.save(m)
    assert not backend.load(m.id).verify_integrity()
    return m


def _promoted_copy_victim(backend) -> tuple[Memory, Memory]:
    """A copy made by the old Memory.promote: new id, the source's hash inherited."""
    src = Memory(
        title="source",
        content="source body",
        layer=MemoryLayer.SHORT,
        emotional=EmotionalSnapshot(intensity=2.0, labels=["joy"]),
    )
    src.seal()
    backend.save(src)
    copy = src.promote(MemoryLayer.MID)
    copy.integrity_hash = src.integrity_hash  # what promote did before #173
    backend.save(copy)
    assert not backend.load(copy.id).verify_integrity()
    return src, copy


class TestFindVictims:
    def test_unstripped_title_is_a_victim_with_a_proof(self, backend):
        m = _unstripped_title_victim(backend)
        (v,) = find_victims(backend.base_path)
        assert v.memory_id == m.id
        assert v.reason == REASON_UNSTRIPPED_TITLE
        assert v.old_hash == _seal_over(m, "Observation: x ")
        assert v.new_hash == m.compute_integrity_hash()
        assert "title" in v.proof

    def test_leading_whitespace_variant(self, backend):
        m = Memory(title="t", content="c")
        m.integrity_hash = _seal_over(m, "\nt")
        backend.save(m)
        (v,) = find_victims(backend.base_path)
        assert v.reason == REASON_UNSTRIPPED_TITLE

    def test_promoted_copy_with_inherited_hash(self, backend):
        src, copy = _promoted_copy_victim(backend)
        (v,) = find_victims(backend.base_path)
        assert v.memory_id == copy.id
        assert v.reason == REASON_PROMOTED_COPY
        assert v.old_hash == src.integrity_hash
        assert src.id in v.proof

    def test_clean_store_has_no_victims(self, backend):
        m = Memory(title="t", content="c")
        m.seal()
        backend.save(m)
        assert find_victims(backend.base_path) == []

    def test_content_edit_is_not_a_victim(self, backend):
        m = Memory(title="t", content="c")
        m.seal()
        backend.save(m)
        tamper_on_disk(backend, m.id, content="changed behind the store's back")
        assert find_victims(backend.base_path) == []

    def test_title_edit_that_is_not_whitespace_is_not_a_victim(self, backend):
        m = Memory(title="t", content="c")
        m.seal()
        backend.save(m)
        tamper_on_disk(backend, m.id, title="different title")
        assert find_victims(backend.base_path) == []

    def test_promoted_copy_whose_content_changed_is_not_a_victim(self, backend):
        _, copy = _promoted_copy_victim(backend)
        tamper_on_disk(backend, copy.id, content="edited after the promotion")
        assert find_victims(backend.base_path) == []

    def test_copy_of_a_failing_source_is_not_a_victim(self, backend):
        src, copy = _promoted_copy_victim(backend)
        tamper_on_disk(backend, src.id, content="source edited behind the store's back")
        assert find_victims(backend.base_path) == []

    def test_copy_with_an_unrelated_hash_is_not_a_victim(self, backend):
        src, copy = _promoted_copy_victim(backend)
        tamper_on_disk(backend, copy.id, integrity_hash="f" * 64)
        assert find_victims(backend.base_path) == []

    def test_unsealed_is_not_a_victim(self, backend):
        backend.save(Memory(title="t", content="c"))
        assert find_victims(backend.base_path) == []

    def test_signature_sidecar_blocks_it(self, backend):
        m = _unstripped_title_victim(backend)
        path = backend._find_file(m.id)
        Path(str(path) + ".sig").write_text("x")
        assert find_victims(backend.base_path) == []

    def test_read_only(self, backend):
        _unstripped_title_victim(backend)
        _promoted_copy_victim(backend)
        before = {p: p.read_bytes() for p in backend.base_path.rglob("*.json")}
        find_victims(backend.base_path)
        assert {p: p.read_bytes() for p in backend.base_path.rglob("*.json")} == before


class TestLegacyPromotedCopies:
    def test_copy_written_before_parent_relation_existed_is_found(self, backend):
        _, copy = _promoted_copy_victim(backend)
        data = backend.load(copy.id).model_dump()
        data["metadata"].pop("parent_relation")  # files from before the key existed
        data["metadata"]["promoted_from"] = "short-term"
        path = backend._find_file(copy.id)
        path.write_text(json.dumps(data, default=str))
        assert [v.memory_id for v in find_victims(backend.base_path)] == [copy.id]

    def test_a_decomposed_chunk_is_not_a_promoted_copy(self, backend):
        _, copy = _promoted_copy_victim(backend)
        data = backend.load(copy.id).model_dump()
        data["metadata"].pop("parent_relation")
        data["metadata"]["decomposition"] = {"parent_id": data["parent_id"]}
        backend._find_file(copy.id).write_text(json.dumps(data, default=str))
        assert find_victims(backend.base_path) == []


class TestPromotionChains:
    def _chain(self, backend):
        """short -> mid -> long, every copy carrying the root's hash (the old promote)."""
        root, mid = _promoted_copy_victim(backend)
        long = mid.promote(MemoryLayer.LONG)
        long.integrity_hash = root.integrity_hash
        backend.save(long)
        return root, mid, long

    def test_every_copy_in_a_chain_is_found(self, backend):
        _, mid, long = self._chain(backend)
        assert {v.memory_id for v in find_victims(backend.base_path)} == {mid.id, long.id}

    def test_chain_is_resealed_whatever_the_order(self, backend, tmp_path):
        _, mid, long = self._chain(backend)
        victims = find_victims(backend.base_path)
        out = reseal(MemoryStore(primary=backend), victims, log_path=tmp_path / "l")
        assert out.resealed == 2 and out.skipped == 0 and out.failed == 0
        assert backend.load(mid.id).verify_integrity()
        assert backend.load(long.id).verify_integrity()

    def test_chain_through_a_failing_root_is_not_found(self, backend):
        root, mid, long = self._chain(backend)
        tamper_on_disk(backend, root.id, content="root edited behind the store's back")
        assert find_victims(backend.base_path) == []

    def test_chain_with_a_changed_link_is_not_found(self, backend):
        root, mid, long = self._chain(backend)
        tamper_on_disk(backend, mid.id, content="middle copy edited")
        assert find_victims(backend.base_path) == []


class TestReseal:
    def test_reseal_verifies_and_logs(self, backend, tmp_path):
        m = _unstripped_title_victim(backend)
        _, copy = _promoted_copy_victim(backend)
        store = MemoryStore(primary=backend)
        log = tmp_path / "reseal.jsonl"
        out = reseal(store, find_victims(backend.base_path), log_path=log)
        assert out.resealed == 2 and out.skipped == 0
        assert backend.load(m.id).verify_integrity()
        assert backend.load(copy.id).verify_integrity()
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        assert {x["memory_id"] for x in lines} == {m.id, copy.id}
        assert all(x["old_hash"] and x["new_hash"] and x["reason"] for x in lines)

    def test_reseal_changes_only_the_hash_and_updated_title(self, backend, tmp_path):
        m = _unstripped_title_victim(backend)
        before = backend.load(m.id).model_dump()
        reseal(
            MemoryStore(primary=backend), find_victims(backend.base_path), log_path=tmp_path / "l"
        )
        after = backend.load(m.id).model_dump()
        diff = {k for k in before if before[k] != after[k]}
        assert diff == {"integrity_hash"}

    def test_proof_is_rechecked_at_write_time(self, backend, tmp_path):
        m = _unstripped_title_victim(backend)
        victims = find_victims(backend.base_path)
        tamper_on_disk(backend, m.id, content="edited between the scan and the write")
        out = reseal(MemoryStore(primary=backend), victims, log_path=tmp_path / "l")
        assert out.resealed == 0 and out.skipped == 1
        assert not backend.load(m.id).verify_integrity()


class TestCli:
    def _run(self, backend, *args):
        from skmemory.cli import cli

        store = MemoryStore(primary=backend)
        return CliRunner().invoke(cli, ["fortress", "reseal", *args], obj={"store": store})

    def test_dry_run_is_the_default_and_writes_nothing(self, backend):
        m = _unstripped_title_victim(backend)
        res = self._run(backend)
        assert res.exit_code == 0, res.output
        assert m.id in res.output
        assert REASON_UNSTRIPPED_TITLE in res.output
        assert "dry-run" in res.output.lower()
        assert not backend.load(m.id).verify_integrity()

    def test_write_flag_reseals(self, backend, tmp_path, monkeypatch):
        monkeypatch.setenv("SKMEMORY_RESEAL_LOG", str(tmp_path / "reseal.jsonl"))
        m = _unstripped_title_victim(backend)
        res = self._run(backend, "--write")
        assert res.exit_code == 0, res.output
        assert backend.load(m.id).verify_integrity()
        assert (tmp_path / "reseal.jsonl").exists()

    def test_json_output(self, backend):
        m = _unstripped_title_victim(backend)
        res = self._run(backend, "--json")
        data = json.loads(res.output)
        assert data["dry_run"] is True
        assert data["victims"][0]["memory_id"] == m.id

    def test_ids_filter(self, backend):
        a = _unstripped_title_victim(backend)
        b = Memory(title="t2", content="c2")
        b.integrity_hash = _seal_over(b, "t2 ")
        backend.save(b)
        res = self._run(backend, "--json", "--id", a.id)
        assert [v["memory_id"] for v in json.loads(res.output)["victims"]] == [a.id]
