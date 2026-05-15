from types import SimpleNamespace

from skmemory.context_loader import LazyMemoryLoader, _load_recall_graphs, _load_shared_corpora


class _DummyDB:
    _conn = SimpleNamespace()


class _DummyGraph:
    def __init__(self, graph_name: str):
        self.graph_name = graph_name
        self.indexed = []

    def _ensure_initialized(self):
        return True

    def search(self, query: str, limit: int = 10):
        return [{"id": f"{self.graph_name}-title", "title": f"{self.graph_name} title", "layer": "long-term", "intensity": 1.0, "created_at": ""}]

    def search_by_tags(self, tags, limit: int = 20):
        return [{"id": f"{self.graph_name}-tags", "title": f"{self.graph_name} tags", "layer": "long-term", "intensity": 1.0}]

    def search_by_entity(self, query: str, limit: int = 20):
        return [{"id": f"{self.graph_name}-entity", "title": f"{self.graph_name} entity", "layer": "long-term", "intensity": 1.0, "matched_value": query}]

    def search_by_citation(self, query: str, limit: int = 20):
        return [{"id": f"{self.graph_name}-citation", "title": f"{self.graph_name} citation", "layer": "long-term", "intensity": 1.0, "matched_value": query}]

    def search_by_claim(self, query: str, limit: int = 20):
        return [{"id": f"{self.graph_name}-claim", "title": f"{self.graph_name} claim", "layer": "long-term", "intensity": 1.0, "matched_value": query}]

    def search_by_section(self, query: str, limit: int = 20):
        return [{"id": f"{self.graph_name}-section", "title": f"{self.graph_name} section", "layer": "long-term", "intensity": 1.0, "matched_value": query}]

    def index_memory(self, memory):
        self.indexed.append(memory.id)
        return True



class _DummyRecallBackend:
    def __init__(self):
        self._client = SimpleNamespace(scroll=self.scroll)

    def _ensure_initialized(self):
        return True

    def _memory_from_payload(self, payload):
        return SimpleNamespace(id=payload["id"], title=payload.get("title", payload["id"]), content=payload.get("content", ""), summary=payload.get("summary", ""), tags=payload.get("tags", []), layer=SimpleNamespace(value=payload.get("layer", "long-term")), created_at=payload.get("created_at", ""))

    def scroll(self, collection_name, offset=None, limit=256, with_payload=True, with_vectors=False):
        if offset is None:
            return ([SimpleNamespace(payload={"id": f"{collection_name}-1"}), SimpleNamespace(payload={"id": f"{collection_name}-2"})], "done")
        return ([], None)


def _make_loader(monkeypatch, tmp_path):
    base = tmp_path / "agent"
    config = base / "config"
    config.mkdir(parents=True)
    monkeypatch.setattr("skmemory.context_loader.get_agent_paths", lambda agent_name=None: {"base": base, "config": config})
    monkeypatch.setattr("skmemory.context_loader.SQLiteBackend", lambda *_args, **_kwargs: _DummyDB())
    loader = LazyMemoryLoader("jarvis")
    loader._backends_loaded = True
    loader._search_sqlite = lambda query: []
    return loader



def test_load_shared_corpora_reads_structured_yaml(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "skmemory.yaml").write_text("""shared_corpora:
  - name: hammertime
    vector_collection: hammertime-v3
    graph_name: hammertime-v4
    source_roots:
      - /data/hammerTime
    projection_profile: legal-retrieval
""")
    corpora = _load_shared_corpora(config_dir)
    assert corpora == [{
        "name": "hammertime",
        "vector_collection": "hammertime-v3",
        "graph_name": "hammertime-v4",
        "source_roots": ["/data/hammerTime"],
        "projection_profile": "legal-retrieval",
    }]


def test_load_shared_corpora_falls_back_to_legacy_recall_keys(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "skmemory.yaml").write_text("""recall_collections:
  - hammertime-v3
recall_graphs:
  - hammertime-v4
recall_source_roots:
  hammertime-v4:
    - /data/hammerTime
""")
    corpora = _load_shared_corpora(config_dir)
    assert corpora == [{
        "name": "hammertime-v3",
        "vector_collection": "hammertime-v3",
        "graph_name": "hammertime-v4",
        "source_roots": ["/data/hammerTime"],
        "projection_profile": None,
    }]


def test_load_recall_graphs_reads_yaml(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "skmemory.yaml").write_text("recall_graphs:\n  - hammertime-v3\n")
    assert _load_recall_graphs(config_dir) == ["hammertime-v3"]


def test_ensure_backends_loads_shared_corpora_from_structured_config(monkeypatch, tmp_path):
    base = tmp_path / "agent"
    config = base / "config"
    config.mkdir(parents=True)
    (config / "skmemory.yaml").write_text("""shared_corpora:
  - name: hammertime
    vector_collection: hammertime-v3
    graph_name: hammertime-v4
    source_roots:
      - /data/hammerTime
""")
    monkeypatch.setattr("skmemory.context_loader.get_agent_paths", lambda agent_name=None: {"base": base, "config": config})
    monkeypatch.setattr("skmemory.context_loader.SQLiteBackend", lambda *_args, **_kwargs: _DummyDB())
    monkeypatch.setattr("skmemory.context_loader._load_skvector_config", lambda _config_dir: {"env": "prod"})
    monkeypatch.setattr("skmemory.context_loader._build_skvector_backend", lambda cfg: None)
    monkeypatch.setattr("skmemory.context_loader._load_skgraph_config", lambda _config_dir: {"graph_name": "jarvis-memory"})
    monkeypatch.setattr("skmemory.context_loader._build_skgraph_backend", lambda cfg: _DummyGraph(cfg.get("graph_name", "graph")))
    monkeypatch.setattr("skmemory.context_loader.SKChromaBackend", None, raising=False)
    loader = LazyMemoryLoader("jarvis")
    loader._ensure_backends()
    assert loader._shared_corpora == [{
        "name": "hammertime",
        "vector_collection": "hammertime-v3",
        "graph_name": "hammertime-v4",
        "source_roots": ["/data/hammerTime"],
        "projection_profile": None,
    }]
    assert loader._recall_collections == ["hammertime-v3"]
    assert loader._recall_graphs == ["hammertime-v4"]
    assert "hammertime-v4" in loader._recall_graph_backends


def test_deep_search_includes_recall_graph_results(monkeypatch, tmp_path):
    loader = _make_loader(monkeypatch, tmp_path)
    loader._graph_backend = _DummyGraph("jarvis-memory")
    loader._recall_graph_backends = {"hammertime-v3": _DummyGraph("hammertime-v3")}
    results = loader.deep_search("UCC 3-301 demand claim", max_results=20)
    sources = {row["source_backend"] for row in results}
    assert "skgraph:jarvis-memory" in sources
    assert "skgraph:hammertime-v3" in sources
    assert "skgraph_citation:hammertime-v3" in sources
    assert "skgraph_claim:hammertime-v3" in sources



def test_prune_recall_decomposition_caps_and_prioritizes_legal_signals():
    from types import SimpleNamespace
    from skmemory.context_loader import _prune_recall_decomposition

    decomposition = SimpleNamespace(
        chunk_target=900,
        chunk_overlap=200,
        citations=[f"UCC § 3-{i}" for i in range(150)],
        section_titles=[f"Section {i}" for i in range(90)],
        entities=[f"Generic Entity {i}" for i in range(260)] + ["United States Postal Service", "Secured Party Creditor", "Uniform Commercial Code"],
        claims=[f"generic claim {i}" for i in range(220)] + ["The holder shall enforce the lien.", "The debtor must receive service.", "A secured party may levy collateral."],
    )
    payload = _prune_recall_decomposition(decomposition)
    assert len(payload["citations"]) == 96
    assert len(payload["section_titles"]) == 64
    assert len(payload["entities"]) == 192
    assert len(payload["claims"]) == 160
    assert "United States Postal Service" in payload["entities"][:40]
    assert "The holder shall enforce the lien." in payload["claims"][:40]



def test_append_graph_result_set_merges_duplicates_and_promotes_citation(monkeypatch, tmp_path):
    loader = _make_loader(monkeypatch, tmp_path)
    results = []
    seen_ids = set()
    loader._append_graph_result_set(
        results,
        seen_ids,
        [{"id": "doc-1", "title": "UCC_Complete.md", "layer": "long-term", "match_count": 1, "matched_value": "holder"}],
        "skgraph_entity:hammertime-v3",
    )
    loader._append_graph_result_set(
        results,
        seen_ids,
        [{"id": "doc-1", "title": "UCC_Complete.md", "layer": "long-term", "match_count": 2, "matched_values": ["UCC §§ 3-301", "section\n3-301"]}],
        "skgraph_citation:hammertime-v3",
    )
    assert len(results) == 1
    row = results[0]
    assert row["source_backend"] == "skgraph_citation:hammertime-v3"
    assert "skgraph_entity:hammertime-v3" in row["source_backends"]
    assert "skgraph_citation:hammertime-v3" in row["source_backends"]
    assert row["authority_tier"] == "statute"
    assert "§ 3-301" in row["matched_values"]
    assert row["graph_match_score"] > 0




def test_sync_recall_graphs_indexes_shared_vector_payloads(monkeypatch, tmp_path):
    loader = _make_loader(monkeypatch, tmp_path)
    loader._recall_qdrant_backend = _DummyRecallBackend()
    graph = _DummyGraph("hammertime-v3")
    loader._recall_graph_backends = {"hammertime-v3": graph}
    stats = loader.sync_recall_graphs(batch_size=2)
    assert stats["hammertime-v3"]["indexed"] == 2
    assert stats["hammertime-v3"]["errors"] == 0
    assert graph.indexed == ["hammertime-v3-1", "hammertime-v3-2"]


def test_fusion_score_prefers_citation_graph_hits(monkeypatch, tmp_path):
    loader = _make_loader(monkeypatch, tmp_path)
    base = {"title": "UCC holder rule", "content": "UCC 3-301 holder in due course", "layer": "long-term", "authority_tier": "memory", "created_at": ""}
    citation = loader._compute_fusion_score(dict(base, source_backend="skgraph_citation:hammertime-v3"), "UCC 3-301 holder", ["ucc", "3-301", "holder"] )
    plain = loader._compute_fusion_score(dict(base, source_backend="skgraph:hammertime-v3"), "UCC 3-301 holder", ["ucc", "3-301", "holder"] )
    assert citation > plain



def test_sync_recall_graphs_skips_unchanged_sources(monkeypatch, tmp_path):
    loader = _make_loader(monkeypatch, tmp_path)
    loader._recall_qdrant_backend = _DummyRecallBackend()
    graph = _DummyGraph("hammertime-v3")
    loader._recall_graph_backends = {"hammertime-v3": graph}
    first = loader.sync_recall_graphs(batch_size=2)
    second = loader.sync_recall_graphs(batch_size=2)
    assert first["hammertime-v3"]["indexed"] == 2
    assert second["hammertime-v3"]["indexed"] == 0
    assert second["hammertime-v3"]["skipped"] == 2
    assert graph.indexed == ["hammertime-v3-1", "hammertime-v3-2"]
