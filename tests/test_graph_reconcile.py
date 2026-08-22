"""Guarded AGE orphan reconciliation + parity reporting (card c25e2513).

chg-a76c0aee left hundreds of stale graph Memory nodes per node because
sync_all backfills with MERGE but never deletes. These tests pin the other
half against a fake backend (no docker, no Postgres, no AGE):

  * dry-run parity: correct stale/missing/matched counts, zero deletes;
  * the prune guard (floor / fraction / min-sample, force override) which
    is the pgvector ``prune_guard`` itself;
  * the prune path: exact stale id list deleted via remove_memory, a JSON
    backup written first with node props + edge inventory, and zero-edge
    aux nodes cleaned with per-label counts;
  * tier-race tolerance: a flat file vanishing mid-run is not stale proof;
  * transport: a dead graph aborts before any delete;
  * the ``skmemory graph reconcile`` CLI surface.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skmemory.backends.age_backend import GraphTransportError
from skmemory.cli import cli
from skmemory.graph_reconcile import flat_memory_ids, graph_parity, reconcile_graph


class FakeBackend:
    """Duck-typed AGEGraphBackend stand-in recording every mutation."""

    def __init__(self, graph_ids, *, nodes=None, edges=None, aux_orphans=None, dead=False):
        self.graph_ids = set(graph_ids)
        self.nodes = nodes or {}
        self.edges = edges or {}
        self.aux_orphans = aux_orphans or {}
        self.dead = dead
        self.agent = "lumina"
        self.graph = "lumina_knowledge"
        self.deleted = []
        self.aux_deleted = {}

    def probe_connection(self):
        if self.dead:
            raise GraphTransportError("AGE connection unavailable (fake)")

    def graph_memory_ids(self):
        if self.dead:
            raise GraphTransportError("AGE connection unavailable (fake)")
        return set(self.graph_ids)

    def memory_node_strict(self, memory_id):
        return self.nodes.get(memory_id)

    def memory_edge_inventory(self, memory_id):
        return list(self.edges.get(memory_id, []))

    def remove_memory(self, memory_id):
        self.deleted.append(memory_id)
        self.graph_ids.discard(memory_id)
        return True

    def count_orphaned_aux_nodes(self, labels):
        return {label: len(self.aux_orphans.get(label, ())) for label in labels}

    def delete_orphaned_aux_nodes(self, labels):
        out = {}
        for label in labels:
            orphans = sorted(self.aux_orphans.get(label, ()))
            out[label] = len(orphans)
            self.aux_deleted[label] = orphans
        return out


def _write_flat(mem_dir: Path, mem_id: str, layer: str = "short-term") -> None:
    tier = mem_dir / layer
    tier.mkdir(parents=True, exist_ok=True)
    (tier / f"{mem_id}.json").write_text(
        json.dumps({"id": mem_id, "content": f"fixture {mem_id[:8]}", "layer": layer}),
        encoding="utf-8",
    )


def _ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{uuid.uuid4().hex[:12]}" for _ in range(count)]


# ------------------------------------------------------------------ parity ---


def test_dry_run_parity_counts_and_zero_deletes(tmp_path):
    mem = tmp_path / "memory"
    matched = _ids("match", 2)
    missing = _ids("missing", 1)
    for mid in matched + missing:
        _write_flat(mem, mid)
    stale = _ids("stale", 2)
    fake = FakeBackend(matched + stale)

    stats = reconcile_graph(fake, mem, dry_run=True)

    assert stats["dry_run"] is True
    assert stats["matched"] == 2
    assert stats["stale_candidates"] == 2
    assert stats["missing"] == 1
    assert stats["stale_ids"] == sorted(stale)
    assert stats["missing_ids"] == missing
    assert fake.deleted == []
    assert stats["backup_path"] is None
    assert not list((tmp_path / "backups").glob("*")) if (tmp_path / "backups").exists() else True


def test_graph_parity_reports_explicit_id_lists(tmp_path):
    mem = tmp_path / "memory"
    _write_flat(mem, "flat-only-1")
    fake = FakeBackend(["graph-only-1"])
    report = graph_parity(fake, mem)
    assert report["stale_ids"] == ["graph-only-1"]
    assert report["missing_ids"] == ["flat-only-1"]
    assert report["matched"] == 0


def test_dry_run_reports_aux_orphan_preview(tmp_path):
    mem = tmp_path / "memory"
    _write_flat(mem, "kept-1")
    fake = FakeBackend(["kept-1"], aux_orphans={"Tag": [101, 102], "Claim": [103]})
    stats = reconcile_graph(fake, mem, dry_run=True)
    assert stats["aux_orphans"]["Tag"] == 2
    assert stats["aux_orphans"]["Claim"] == 1
    assert stats["aux_removed"] == {}


# -------------------------------------------------------------------- guard ---


def test_guard_refuses_when_flat_store_empty(tmp_path):
    mem = tmp_path / "memory"
    (mem / "short-term").mkdir(parents=True)
    stale = _ids("stale", 10)
    fake = FakeBackend(stale)
    stats = reconcile_graph(fake, mem, dry_run=False)
    assert stats["guard_allowed"] is False
    assert stats["prune_skipped"] is True
    assert "floor" in stats["guard_reason"]
    assert stats["pruned"] == 0
    assert fake.deleted == []


def test_guard_refuses_when_fraction_exceeds_cap(tmp_path):
    mem = tmp_path / "memory"
    kept = _ids("kept", 50)
    for mid in kept:
        _write_flat(mem, mid)
    stale = _ids("stale", 50)  # 50 of 100 = 50% > 20% cap at pg >= min_sample
    fake = FakeBackend(kept + stale)
    stats = reconcile_graph(fake, mem, dry_run=False)
    assert stats["prune_skipped"] is True
    assert "cap" in stats["guard_reason"]
    assert fake.deleted == []


def test_guard_fraction_cap_skipped_below_min_sample(tmp_path):
    mem = tmp_path / "memory"
    kept = _ids("kept", 2)
    for mid in kept:
        _write_flat(mem, mid)
    stale = _ids("stale", 1)  # 1 of 3 = 33%, but graph < min_sample (20)
    fake = FakeBackend(kept + stale, nodes={stale[0]: {"id": stale[0]}})
    stats = reconcile_graph(fake, mem, dry_run=False)
    assert stats["prune_skipped"] is False
    assert stats["pruned"] == 1
    assert fake.deleted == stale


def test_force_override_prunes_past_the_guard(monkeypatch, tmp_path):
    mem = tmp_path / "memory"
    (mem / "short-term").mkdir(parents=True)
    stale = _ids("stale", 5)
    nodes = {mid: {"id": mid, "title": "x"} for mid in stale}
    fake = FakeBackend(stale, nodes=nodes)
    stats = reconcile_graph(fake, mem, dry_run=False, force=True)
    assert stats["guard_allowed"] is True
    assert stats["pruned"] == 5
    assert sorted(fake.deleted) == sorted(stale)


def test_force_env_override(monkeypatch, tmp_path):
    mem = tmp_path / "memory"
    (mem / "short-term").mkdir(parents=True)
    stale = _ids("stale", 3)
    fake = FakeBackend(stale, nodes={mid: {"id": mid} for mid in stale})
    monkeypatch.setenv("SKMEMORY_GRAPH_RECONCILE_FORCE", "1")
    stats = reconcile_graph(fake, mem, dry_run=False)
    assert stats["pruned"] == 3


# ----------------------------------------------------------- prune path ---


def test_prune_deletes_exactly_the_stale_ids_with_backup(tmp_path):
    mem = tmp_path / "memory"
    kept = _ids("kept", 2)
    for mid in kept:
        _write_flat(mem, mid)
    stale = _ids("stale", 2)
    nodes = {mid: {"id": mid, "title": f"node {mid[:6]}"} for mid in stale}
    edges = {
        stale[0]: [{"type": "TAGGED", "other_label": "Tag", "other_properties": {"name": "ops"}}],
        stale[1]: [
            {"type": "MENTIONS", "other_label": "Entity", "other_properties": {"name": "Acme"}},
            {"type": "RELATED_TO", "other_label": "Memory", "other_properties": {"id": "x"}},
        ],
    }
    aux = {"Tag": [101], "Entity": [102, 103]}
    fake = FakeBackend(kept + stale, nodes=nodes, edges=edges, aux_orphans=aux)

    stats = reconcile_graph(fake, mem, dry_run=False)

    assert sorted(fake.deleted) == sorted(stale)
    assert stats["pruned"] == 2
    assert stats["pruned_ids"] == sorted(stale)
    assert stats["edges_removed"] == 3
    assert stats["aux_removed"]["Tag"] == 1
    assert stats["aux_removed"]["Entity"] == 2
    assert stats["aux_removed"]["Claim"] == 0
    assert fake.aux_deleted["Tag"] == [101]

    backup = Path(stats["backup_path"])
    assert backup.is_file()
    assert backup.parent == tmp_path / "backups"
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["agent"] == "lumina"
    backed_ids = sorted(node["id"] for node in payload["nodes"])
    assert backed_ids == sorted(stale)
    first = next(node for node in payload["nodes"] if node["id"] == stale[0])
    assert first["properties"]["title"] == nodes[stale[0]]["title"]
    assert first["edges"][0]["type"] == "TAGGED"


def test_prune_aborts_when_a_delete_fails(tmp_path):
    mem = tmp_path / "memory"
    stale = _ids("stale", 3)
    fake = FakeBackend(stale, nodes={mid: {"id": mid} for mid in stale})
    calls = {"count": 0}

    def flaky_remove(memory_id):
        calls["count"] += 1
        if calls["count"] == 2:
            return False
        fake.deleted.append(memory_id)
        return True

    fake.remove_memory = flaky_remove
    with pytest.raises(GraphTransportError):
        reconcile_graph(fake, mem, dry_run=False, force=True)
    assert calls["count"] == 2  # stopped at the failure; the third never ran
    assert len(fake.deleted) == 1


# ---------------------------------------------------------------- tier race ---


def test_vanishing_flat_file_is_not_stale_proof(monkeypatch, tmp_path):
    """A flat file that vanishes mid-run keeps its id authoritative."""
    mem = tmp_path / "memory"
    mem_id = "race-" + uuid.uuid4().hex[:8]
    _write_flat(mem, mem_id)
    target = mem / "short-term" / f"{mem_id}.json"

    original_read_text = Path.read_text

    def racing_read_text(self, *args, **kwargs):
        if self == target:
            raise FileNotFoundError(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", racing_read_text)
    assert mem_id in flat_memory_ids(mem)
    fake = FakeBackend([mem_id])
    report = graph_parity(fake, mem)
    assert report["stale_candidates"] == 0


def test_tombstoned_flat_id_reads_as_stale(tmp_path):
    """A deliberately forgotten memory is pruned from the graph too."""
    mem = tmp_path / "memory"
    mem_id = "gone-" + uuid.uuid4().hex[:8]
    kept = "kept-" + uuid.uuid4().hex[:8]
    _write_flat(mem, mem_id)
    _write_flat(mem, kept)  # healthy flat file keeps the floor guard satisfied
    tombstones = mem / "tombstones"
    tombstones.mkdir(parents=True)
    (tombstones / f"{mem_id}.json").write_text("{}", encoding="utf-8")
    assert flat_memory_ids(mem) == {kept}
    fake = FakeBackend([mem_id, kept], nodes={mem_id: {"id": mem_id}})
    stats = reconcile_graph(fake, mem, dry_run=False)
    assert stats["pruned"] == 1
    assert fake.deleted == [mem_id]


# ---------------------------------------------------------------- transport ---


def test_dead_graph_aborts_before_any_delete(tmp_path):
    mem = tmp_path / "memory"
    _write_flat(mem, "flat-1")
    fake = FakeBackend(["graph-1"], dead=True)
    with pytest.raises(GraphTransportError):
        reconcile_graph(fake, mem, dry_run=False)
    assert fake.deleted == []
    assert not (tmp_path / "backups").exists()


def test_dead_graph_parity_raises_not_zero_counts(tmp_path):
    mem = tmp_path / "memory"
    _write_flat(mem, "flat-1")
    fake = FakeBackend([], dead=True)
    with pytest.raises(GraphTransportError):
        graph_parity(fake, mem)


# ---------------------------------------------------------------------- CLI ---


def _cli_patches(tmp_path, fake):
    base = tmp_path / "lumina"
    (base / "memory").mkdir(parents=True, exist_ok=True)
    paths = {"base": base, "config": base / "config", "memory": base / "memory"}
    return (
        patch("skmemory.agents.get_agent_paths", return_value=paths),
        patch("skmemory.config.load_config", return_value=None),
        patch("skmemory.backends.age_backend.AGEGraphBackend", return_value=fake),
    )


def test_cli_reconcile_dry_run_reports_and_writes_nothing(tmp_path):
    mem = tmp_path / "lumina" / "memory"
    _write_flat(mem, "kept-1")
    fake = FakeBackend(["kept-1", "stale-1"], nodes={"stale-1": {"id": "stale-1"}})
    paths_patch, cfg_patch, backend_patch = _cli_patches(tmp_path, fake)
    with paths_patch, cfg_patch, backend_patch:
        result = CliRunner().invoke(cli, ["graph", "reconcile"], obj={"store": None})
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "stale=1" in result.output
    assert "stale-1" in result.output
    assert fake.deleted == []


def test_cli_reconcile_json_output_parses(tmp_path):
    mem = tmp_path / "lumina" / "memory"
    _write_flat(mem, "kept-1")
    fake = FakeBackend(["kept-1"])
    paths_patch, cfg_patch, backend_patch = _cli_patches(tmp_path, fake)
    with paths_patch, cfg_patch, backend_patch:
        result = CliRunner().invoke(cli, ["graph", "reconcile", "--json"], obj={"store": None})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["stale_candidates"] == 0
    assert payload["dry_run"] is True


def test_cli_apply_refused_by_guard_exits_nonzero(tmp_path):
    mem = tmp_path / "lumina" / "memory"  # empty flat store: floor guard trips
    mem.mkdir(parents=True, exist_ok=True)
    fake = FakeBackend(_ids("stale", 5))
    paths_patch, cfg_patch, backend_patch = _cli_patches(tmp_path, fake)
    with paths_patch, cfg_patch, backend_patch:
        result = CliRunner().invoke(cli, ["graph", "reconcile", "--apply"], obj={"store": None})
    assert result.exit_code == 1
    assert "PRUNE REFUSED" in result.output
    assert fake.deleted == []


def test_cli_dead_graph_exits_nonzero(tmp_path):
    fake = FakeBackend([], dead=True)
    paths_patch, cfg_patch, backend_patch = _cli_patches(tmp_path, fake)
    with paths_patch, cfg_patch, backend_patch:
        result = CliRunner().invoke(cli, ["graph", "reconcile"], obj={"store": None})
    assert result.exit_code == 1
    assert "graph reconcile failed" in result.output
