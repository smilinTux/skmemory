from pathlib import Path
from types import SimpleNamespace

from skmemory.config import SKMemoryConfig
from skmemory.corpus_registry import build_corpus_registry_report, inventory_cache_namespace
from skmemory.recall_cache import write_cache_document, write_source_manifest


def test_inventory_cache_namespace_reports_projection_counts(tmp_path: Path):
    write_source_manifest(tmp_path, 'hammertime-v3', [{'source_ref': 'reference/a.md', 'source_path': '/tmp/a.md', 'payload': {'category': 'legal', 'type': 'document'}}])
    write_cache_document(tmp_path, 'hammertime-v3', 'reference/a.md', {
        'graph_name': 'hammertime-v3',
        'source_collection': 'hammertime-v3',
        'source_ref': 'reference/a.md',
        'source_name': 'a.md',
        'category': 'legal',
        'type': 'document',
        'host': 'chiap01',
        'projection': {
            'projection_profile': 'legal-retrieval',
            'projection_counts': {'citations': 3, 'section_titles': 2, 'entities': 5, 'claims': 4},
            'full_counts': {'citations': 6, 'section_titles': 4, 'entities': 10, 'claims': 8},
        },
    })
    report = inventory_cache_namespace(tmp_path, 'hammertime-v3')
    assert report['manifest_sources'] == 1
    assert report['cache_documents'] == 1
    assert report['projection_profiles'] == {'legal-retrieval': 1}
    assert report['projection_reduction']['citations']['retained_ratio'] == 0.5


def test_build_corpus_registry_report_includes_local_and_shared(monkeypatch, tmp_path: Path):
    memory_dir = tmp_path / 'agent' / 'memory'
    config_dir = tmp_path / 'agent' / 'config'
    config_dir.mkdir(parents=True)
    memory_dir.mkdir(parents=True)
    write_source_manifest(memory_dir, 'hammertime-v3', [{'source_ref': 'reference/a.md', 'source_path': '/tmp/a.md', 'payload': {'category': 'legal', 'type': 'document'}}])
    write_cache_document(memory_dir, 'hammertime-v3', 'reference/a.md', {
        'graph_name': 'hammertime-v3',
        'source_collection': 'hammertime-v3',
        'source_ref': 'reference/a.md',
        'source_name': 'a.md',
        'category': 'legal',
        'type': 'document',
        'host': 'chiap01',
        'projection': {
            'projection_profile': 'legal-retrieval',
            'projection_counts': {'citations': 3, 'section_titles': 2, 'entities': 5, 'claims': 4},
            'full_counts': {'citations': 6, 'section_titles': 4, 'entities': 10, 'claims': 8},
        },
    })

    fake_loader = SimpleNamespace(
        paths={'base': tmp_path / 'agent', 'config_yaml': config_dir / 'skmemory.yaml'},
        _shared_corpora=[{
            'name': 'hammertime',
            'vector_collection': 'hammertime-v3',
            'graph_name': 'hammertime-v4',
            'source_roots': ['/data/hammerTime'],
            'projection_profile': 'legal-retrieval',
        }],
        _ensure_backends=lambda: None,
    )
    monkeypatch.setattr('skmemory.corpus_registry.LazyMemoryLoader', lambda agent: fake_loader)
    monkeypatch.setattr('skmemory.corpus_registry.load_config', lambda path: SKMemoryConfig(
        backends_enabled=['sqlite', 'skvector', 'skgraph'],
        skvector_collection='jarvis-memory',
        skgraph_graph_name='jarvis-memory',
        chroma_collection='jarvis-memory',
    ))
    report = build_corpus_registry_report(agent='jarvis')
    assert report['local']['primary_vector_collection'] == 'jarvis-memory'
    assert report['shared_corpora'][0]['name'] == 'hammertime'
    assert report['shared_corpora'][0]['graph_name'] == 'hammertime-v4'
    assert report['shared_corpora'][0]['inventory']['cache_documents'] == 1
