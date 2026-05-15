#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time

from skmemory.context_loader import LazyMemoryLoader
from skmemory.recall_cache import write_source_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build a unique-source manifest for distributed recall-cache workers.')
    parser.add_argument('--agent', default='jarvis')
    parser.add_argument('--graph-name', required=True)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--limit', type=int, default=0, help='Stop after this many unique source docs (0 = no limit).')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    loader = LazyMemoryLoader(args.agent)
    loader._ensure_backends()
    recall_backend = loader._recall_qdrant_backend or loader._vector_backend
    if recall_backend is None or not recall_backend._ensure_initialized():
        raise SystemExit('recall backend unavailable')
    memory_dir = loader.paths['base'] / 'memory'
    next_offset = None
    seen_sources: set[str] = set()
    manifest_entries: list[dict] = []
    host = socket.gethostname()
    scanned_points = unresolved = collapsed = 0
    start = time.time()

    while True:
        points, next_offset = recall_backend._client.scroll(
            collection_name=args.graph_name,
            offset=next_offset,
            limit=args.batch_size,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        for point in points:
            scanned_points += 1
            payload = point.payload or {}
            source_ref = str(payload.get('parent_doc') or payload.get('file_path') or payload.get('filename') or payload.get('id') or getattr(point, 'id', ''))
            if not source_ref:
                unresolved += 1
                continue
            if source_ref in seen_sources:
                collapsed += 1
                continue
            seen_sources.add(source_ref)
            source_path = loader._resolve_recall_source_path(args.graph_name, payload)
            if source_path is None:
                unresolved += 1
                continue
            manifest_entries.append({
                'source_ref': source_ref,
                'source_path': str(source_path),
                'payload': dict(payload),
            })
            elapsed = time.time() - start
            rate = len(manifest_entries) / elapsed if elapsed > 0 else 0.0
            print(json.dumps({
                'stage': 'manifest-build',
                'host': host,
                'graph_name': args.graph_name,
                'scanned_points': scanned_points,
                'unique_sources': len(manifest_entries),
                'collapsed': collapsed,
                'unresolved': unresolved,
                'rate_docs_per_sec': round(rate, 4),
                'last_source': source_ref,
            }), flush=True)
            if args.limit and len(manifest_entries) >= args.limit:
                path = write_source_manifest(memory_dir, args.graph_name, manifest_entries)
                print(json.dumps({'done': True, 'manifest_path': str(path), 'unique_sources': len(manifest_entries), 'collapsed': collapsed, 'unresolved': unresolved}), flush=True)
                return
        if next_offset is None:
            break

    path = write_source_manifest(memory_dir, args.graph_name, manifest_entries)
    print(json.dumps({'done': True, 'manifest_path': str(path), 'unique_sources': len(manifest_entries), 'collapsed': collapsed, 'unresolved': unresolved}), flush=True)


if __name__ == '__main__':
    main()
