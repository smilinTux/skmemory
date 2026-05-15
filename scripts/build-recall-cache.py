#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path

from skmemory.context_loader import LazyMemoryLoader
from skmemory.recall_cache import (
    build_cache_document,
    load_cache_document,
    load_source_manifest,
    shard_for_source,
    write_cache_document,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build distributed recall decomposition cache for a shared collection.')
    parser.add_argument('--agent', default='jarvis')
    parser.add_argument('--graph-name', required=True)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--limit', type=int, default=0, help='Stop after this many processed source docs (0 = no limit).')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--projection-profile', default='', help='Optional named projection profile override, e.g. legal-retrieval.')
    parser.add_argument('--scan-vector', action='store_true', help='Fallback to direct vector scan instead of a prebuilt source manifest.')
    return parser.parse_args()


def _iter_manifest_sources(loader: LazyMemoryLoader, graph_name: str):
    memory_dir = loader.paths['base'] / 'memory'
    manifest = load_source_manifest(memory_dir, graph_name)
    if not manifest:
        raise SystemExit('source manifest missing; run scripts/build-recall-manifest.py first or use --scan-vector')
    for entry in manifest:
        yield str(entry.get('source_ref', '')), dict(entry.get('payload') or {}), entry.get('source_path')


def _iter_vector_sources(loader: LazyMemoryLoader, graph_name: str, batch_size: int):
    recall_backend = loader._recall_qdrant_backend or loader._vector_backend
    if recall_backend is None or not recall_backend._ensure_initialized():
        raise SystemExit('recall backend unavailable')
    next_offset = None
    seen_sources: set[str] = set()
    while True:
        points, next_offset = recall_backend._client.scroll(
            collection_name=graph_name,
            offset=next_offset,
            limit=batch_size,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        for point in points:
            payload = point.payload or {}
            source_ref = str(payload.get('parent_doc') or payload.get('file_path') or payload.get('filename') or payload.get('id') or getattr(point, 'id', ''))
            if not source_ref or source_ref in seen_sources:
                continue
            seen_sources.add(source_ref)
            yield source_ref, dict(payload), None
        if next_offset is None:
            break


def main() -> None:
    args = parse_args()
    loader = LazyMemoryLoader(args.agent)
    loader._ensure_backends()
    memory_dir = loader.paths['base'] / 'memory'
    host = socket.gethostname()
    processed = written = skipped = errors = 0
    start = time.time()

    source_iter = (
        _iter_vector_sources(loader, args.graph_name, args.batch_size)
        if args.scan_vector
        else _iter_manifest_sources(loader, args.graph_name)
    )
    eligible_entries: list[tuple[str, dict, str | None]] = []
    for source_ref, payload, source_path_text in source_iter:
        if shard_for_source(source_ref, args.shard_count) == args.shard_index:
            eligible_entries.append((source_ref, payload, source_path_text))
    total_docs = len(eligible_entries)

    for source_ref, payload, source_path_text in eligible_entries:
        processed += 1
        try:
            source_path = Path(source_path_text) if source_path_text else loader._resolve_recall_source_path(args.graph_name, payload)
            if source_path is None or not source_path.exists():
                skipped += 1
                continue
            existing = load_cache_document(memory_dir, args.graph_name, source_ref)
            if existing and not args.force and existing.get('fingerprint') == loader._fingerprint_recall_source(payload, source_path):
                skipped += 1
            else:
                cache_doc = build_cache_document(
                    graph_name=args.graph_name,
                    source_ref=source_ref,
                    source_path=source_path,
                    payload=payload,
                    host=host,
                    projection_profile=args.projection_profile or None,
                )
                write_cache_document(memory_dir, args.graph_name, source_ref, cache_doc)
                written += 1
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed > 0 else 0.0
            remaining = max(total_docs - processed, 0)
            eta_seconds = round(remaining / rate, 1) if rate > 0 else None
            print(json.dumps({
                'stage': 'cache-build',
                'host': host,
                'graph_name': args.graph_name,
                'shard_index': args.shard_index,
                'shard_count': args.shard_count,
                'processed': processed,
                'total_docs': total_docs,
                'written': written,
                'skipped': skipped,
                'errors': errors,
                'rate_docs_per_sec': round(rate, 4),
                'eta_seconds': eta_seconds,
                'last_source': source_ref,
            }), flush=True)
        except Exception as exc:
            errors += 1
            print(json.dumps({'stage': 'cache-build', 'host': host, 'error': str(exc), 'source_ref': source_ref}), flush=True)
        if args.limit and processed >= args.limit:
            print(json.dumps({'done': True, 'processed': processed, 'total_docs': total_docs, 'written': written, 'skipped': skipped, 'errors': errors}), flush=True)
            return
    print(json.dumps({'done': True, 'processed': processed, 'total_docs': total_docs, 'written': written, 'skipped': skipped, 'errors': errors}), flush=True)


if __name__ == '__main__':
    main()
