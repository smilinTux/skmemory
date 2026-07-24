from pathlib import Path

from skmemory.recall_cache import (
    build_cache_document,
    build_source_manifest,
    graph_state_path,
    infer_projection_profile,
    load_cache_document,
    load_source_manifest,
    memory_from_cache_document,
    project_decomposition,
    shard_for_source,
    write_cache_document,
    write_source_manifest,
)


def test_project_decomposition_caps_and_prioritizes_legal_signals():
    projection = project_decomposition(
        {
            "chunk_target": 900,
            "chunk_overlap": 200,
            "citations": [f"UCC § 3-{i}" for i in range(150)],
            "section_titles": [f"Section {i}" for i in range(90)]
            + ["Notice of Levy", "Article 9"],
            "entities": [f"Generic Entity {i}" for i in range(260)]
            + [
                "United States Postal Service",
                "Secured Party Creditor",
                "Uniform Commercial Code",
                "TreasuryDirect",
            ],
            "claims": [f"generic claim {i}" for i in range(220)]
            + [
                "The holder shall enforce the lien.",
                "The debtor must receive service.",
                "A secured party may levy collateral.",
            ],
        },
        graph_name="hammertime-v3",
        source_ref="reference/legal/test.md",
        payload={"category": "document", "type": "document"},
    )
    assert projection["projection_profile"] == "legal-retrieval"
    assert len(projection["citations"]) == 128
    assert len(projection["section_titles"]) <= 32
    assert len(projection["entities"]) <= 72
    assert len(projection["claims"]) <= 48
    assert "United States Postal Service" in projection["entities"][:24]
    assert "TreasuryDirect" in projection["entities"][:24]
    assert "The holder shall enforce the lien." in projection["claims"][:24]
    assert projection["full_counts"]["entities"] > projection["projection_counts"]["entities"]


def test_infer_projection_profile_supports_explicit_override():
    assert (
        infer_projection_profile(graph_name="notes", requested_profile="legal")
        == "legal-retrieval"
    )
    assert (
        infer_projection_profile(graph_name="hammertime-v3", requested_profile="default")
        == "default"
    )


def test_infer_projection_profile_supports_reference_and_workflow_profiles():
    assert (
        infer_projection_profile(graph_name="knowledge-base", requested_profile="reference")
        == "reference-retrieval"
    )
    assert (
        infer_projection_profile(graph_name="ops-runbooks", requested_profile="workflow")
        == "workflow-retrieval"
    )
    assert (
        infer_projection_profile(
            graph_name="guide-template-corpus",
            source_ref="docs/reference/tooling.md",
            payload={"category": "reference", "type": "guide"},
        )
        == "reference-retrieval"
    )
    assert (
        infer_projection_profile(
            graph_name="ops-corpus",
            source_ref="runbooks/restart-service.md",
            payload={"category": "workflow", "type": "process"},
        )
        == "workflow-retrieval"
    )


def test_project_decomposition_reference_profile_prefers_reference_signals():
    projection = project_decomposition(
        {
            "chunk_target": 900,
            "chunk_overlap": 200,
            "citations": [f"RFC-{i}" for i in range(80)],
            "section_titles": ["Overview", "Usage", "Configuration", "Troubleshooting"]
            + [f"Noise {i}" for i in range(80)],
            "entities": [f"Generic Entity {i}" for i in range(160)]
            + ["OpenAI API", "Qdrant Client", "Tool Registry"],
            "claims": [f"generic claim {i}" for i in range(140)]
            + [
                "The API supports JSON responses.",
                "The endpoint returns structured metadata.",
                "The configuration requires an api_key parameter.",
            ],
        },
        graph_name="reference-corpus",
        source_ref="docs/reference/api.md",
        payload={"category": "reference", "type": "guide"},
        projection_profile="reference",
    )
    assert projection["projection_profile"] == "reference-retrieval"
    assert len(projection["citations"]) == 48
    assert len(projection["section_titles"]) <= 56
    assert len(projection["entities"]) <= 96
    assert len(projection["claims"]) <= 72
    assert "OpenAI API" in projection["entities"][:24]
    assert "The API supports JSON responses." in projection["claims"][:24]


def test_project_decomposition_workflow_profile_prefers_process_signals():
    projection = project_decomposition(
        {
            "chunk_target": 900,
            "chunk_overlap": 200,
            "citations": [f"STEP-{i}" for i in range(40)],
            "section_titles": ["Setup", "Procedure", "Verification", "Checklist"]
            + [f"Noise {i}" for i in range(80)],
            "entities": [f"Generic Entity {i}" for i in range(160)]
            + ["Task Queue", "Agent Router", "Scheduler Service"],
            "claims": [f"generic claim {i}" for i in range(140)]
            + [
                "Run the verification step after restart.",
                "Execute the workflow in order.",
                "Check the service health before handoff.",
            ],
        },
        graph_name="workflow-corpus",
        source_ref="runbooks/restart-service.md",
        payload={"category": "workflow", "type": "process"},
        projection_profile="workflow",
    )
    assert projection["projection_profile"] == "workflow-retrieval"
    assert len(projection["citations"]) == 24
    assert len(projection["section_titles"]) <= 48
    assert len(projection["entities"]) <= 80
    assert len(projection["claims"]) <= 96
    assert "Task Queue" in projection["entities"][:24]
    assert "Run the verification step after restart." in projection["claims"][:24]


def test_source_manifest_round_trip_and_dedup(tmp_path: Path):
    manifest = build_source_manifest(
        [
            {"source_ref": "reference/b.md", "payload": {"id": "b"}, "source_path": "/tmp/b.md"},
            {"source_ref": "reference/a.md", "payload": {"id": "a"}, "source_path": "/tmp/a.md"},
            {"source_ref": "reference/b.md", "payload": {"id": "b2"}, "source_path": "/tmp/b2.md"},
        ]
    )
    assert [entry["source_ref"] for entry in manifest] == ["reference/a.md", "reference/b.md"]
    write_source_manifest(tmp_path, "hammertime-v3", manifest)
    loaded = load_source_manifest(tmp_path, "hammertime-v3")
    assert [entry["source_ref"] for entry in loaded] == ["reference/a.md", "reference/b.md"]
    assert loaded[1]["source_path"] == "/tmp/b.md"


def test_graph_state_paths_are_shard_specific(tmp_path: Path):
    base = graph_state_path(tmp_path, "hammertime-v3")
    shard = graph_state_path(tmp_path, "hammertime-v3", shard_key="s00-of-32")
    assert base.name == "graph-state.json"
    assert shard.name == "graph-state-s00-of-32.json"
    assert shard != base


def test_cache_round_trip_and_memory_projection(tmp_path: Path):
    source = tmp_path / "source.md"
    source.write_text(
        "UCC § 3-301\n\nThe holder shall enforce the lien.\n\nUnited States Postal Service"
    )
    cache_doc = build_cache_document(
        graph_name="hammertime-v3",
        source_ref="reference/postal/source.md",
        source_path=source,
        payload={"category": "document", "type": "document"},
        host="chiap01",
    )
    write_cache_document(tmp_path, "hammertime-v3", "reference/postal/source.md", cache_doc)
    loaded = load_cache_document(tmp_path, "hammertime-v3", "reference/postal/source.md")
    assert loaded is not None
    assert loaded["host"] == "chiap01"
    assert loaded["projection"]["projection_profile"] == "legal-retrieval"
    memory = memory_from_cache_document(loaded, target_graph_name="hammertime-v4")
    assert memory.source == "recall:hammertime-v4"
    assert "recall-source:hammertime-v3" in memory.tags
    assert memory.metadata["source_collection"] == "hammertime-v3"
    assert memory.metadata["target_graph"] == "hammertime-v4"
    assert memory.metadata["authority_tier"] == "secondary"
    assert "UCC § 3-301" in memory.metadata["decomposition"]["citations"]


def test_shard_for_source_is_stable():
    shard = shard_for_source("reference/postal/source.md", 8)
    assert 0 <= shard < 8
    assert shard == shard_for_source("reference/postal/source.md", 8)
