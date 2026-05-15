from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import load_config
from .context_loader import LazyMemoryLoader
from .recall_cache import iter_cache_documents, load_source_manifest


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def inventory_cache_namespace(memory_dir: Path, cache_name: str) -> dict[str, Any]:
    manifest = load_source_manifest(memory_dir, cache_name)
    cache_docs = [cache_doc for _path, cache_doc in iter_cache_documents(memory_dir, cache_name)]
    projection_profiles = Counter()
    source_collections = Counter()
    categories = Counter()
    types = Counter()
    hosts = Counter()
    reduction_totals = Counter()
    reduction_full = Counter()

    for cache_doc in cache_docs:
        projection = dict(cache_doc.get('projection') or {})
        full_counts = dict(projection.get('full_counts') or {})
        projection_counts = dict(projection.get('projection_counts') or {})
        projection_profiles[str(projection.get('projection_profile') or 'default')] += 1
        source_collections[str(cache_doc.get('source_collection') or cache_doc.get('graph_name') or '')] += 1
        categories[str(cache_doc.get('category') or 'unknown')] += 1
        types[str(cache_doc.get('type') or 'unknown')] += 1
        hosts[str(cache_doc.get('host') or 'unknown')] += 1
        for key in ('citations', 'section_titles', 'entities', 'claims'):
            reduction_totals[key] += int(projection_counts.get(key, 0) or 0)
            reduction_full[key] += int(full_counts.get(key, 0) or 0)

    projection_reduction = {
        key: {
            'projected': reduction_totals[key],
            'full': reduction_full[key],
            'retained_ratio': _ratio(reduction_totals[key], reduction_full[key]),
        }
        for key in ('citations', 'section_titles', 'entities', 'claims')
    }

    return {
        'cache_name': cache_name,
        'manifest_sources': len(manifest),
        'cache_documents': len(cache_docs),
        'manifest_minus_cache': max(len(manifest) - len(cache_docs), 0),
        'projection_profiles': dict(sorted(projection_profiles.items())),
        'source_collections': dict(sorted(source_collections.items())),
        'categories': dict(sorted(categories.items())),
        'types': dict(sorted(types.items())),
        'hosts': dict(sorted(hosts.items())),
        'projection_reduction': projection_reduction,
    }


def build_corpus_registry_report(agent: str = 'jarvis', names: list[str] | None = None) -> dict[str, Any]:
    loader = LazyMemoryLoader(agent)
    loader._ensure_backends()
    cfg = load_config(loader.paths['config_yaml'])
    memory_dir = loader.paths['base'] / 'memory'
    name_filters = {value.casefold() for value in (names or []) if str(value).strip()}

    local = {
        'agent': agent,
        'backends_enabled': list(cfg.backends_enabled if cfg else []),
        'primary_vector_collection': cfg.skvector_collection if cfg else None,
        'primary_graph_name': cfg.skgraph_graph_name if cfg else None,
        'primary_chroma_collection': cfg.chroma_collection if cfg else None,
    }

    shared_corpora: list[dict[str, Any]] = []
    for corpus in loader._shared_corpora:
        corpus_name = str(corpus.get('name') or corpus.get('vector_collection') or '').strip()
        vector_collection = str(corpus.get('vector_collection') or '').strip()
        graph_name = str(corpus.get('graph_name') or vector_collection).strip()
        if name_filters and corpus_name.casefold() not in name_filters and vector_collection.casefold() not in name_filters and graph_name.casefold() not in name_filters:
            continue
        inventory = inventory_cache_namespace(memory_dir, vector_collection)
        shared_corpora.append({
            'name': corpus_name,
            'vector_collection': vector_collection,
            'graph_name': graph_name,
            'source_roots': list(corpus.get('source_roots') or []),
            'projection_profile': corpus.get('projection_profile'),
            'inventory': inventory,
        })

    return {
        'agent': agent,
        'memory_dir': str(memory_dir),
        'local': local,
        'shared_corpora': shared_corpora,
    }
