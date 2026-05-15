#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time

from skmemory.context_loader import LazyMemoryLoader
from skmemory.recall_cache import iter_cache_documents, load_graph_state, memory_from_cache_document, save_graph_state, shard_for_source


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Ingest cached recall decomposition into SKGraph.')
    parser.add_argument('--agent', default='jarvis')
    parser.add_argument('--graph-name', required=True, help='Target graph name to ingest into.')
    parser.add_argument('--cache-name', default='', help='Cache namespace to read from; defaults to --graph-name.')
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--checkpoint-every', type=int, default=25)
    return parser.parse_args()


def _state_key(shard_index: int, shard_count: int) -> str | None:
    if shard_count <= 1:
        return None
    return f's{shard_index:02d}-of-{shard_count:02d}'


def main() -> None:
    args = parse_args()
    loader = LazyMemoryLoader(args.agent)
    loader._ensure_backends()
    graph_backend = loader._recall_graph_backends.get(args.graph_name)
    if graph_backend is None or not graph_backend._ensure_initialized():
        raise SystemExit(f'graph backend unavailable for {args.graph_name}')
    memory_dir = loader.paths['base'] / 'memory'
    cache_name = args.cache_name or args.graph_name
    state_key = _state_key(args.shard_index, args.shard_count)
    state = load_graph_state(memory_dir, args.graph_name, shard_key=state_key)
    cache_entries = [
        (_path, cache_doc)
        for _path, cache_doc in iter_cache_documents(memory_dir, cache_name)
        if shard_for_source(str(cache_doc.get('source_ref', '')), args.shard_count) == args.shard_index
    ]
    total_docs = len(cache_entries)
    processed = indexed = skipped = errors = 0
    host = socket.gethostname()
    start = time.time()
    dirty = False

    for _path, cache_doc in cache_entries:
        source_ref = str(cache_doc.get('source_ref', ''))
        processed += 1
        fingerprint = str(cache_doc.get('fingerprint', ''))
        if not args.force and state.get(source_ref) == fingerprint:
            skipped += 1
            continue
        try:
            memory = memory_from_cache_document(cache_doc, target_graph_name=args.graph_name)
            if graph_backend.index_memory(memory):
                indexed += 1
                state[source_ref] = fingerprint
                dirty = True
                if args.checkpoint_every > 0 and indexed % args.checkpoint_every == 0:
                    save_graph_state(memory_dir, args.graph_name, state, shard_key=state_key)
                    dirty = False
            else:
                errors += 1
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed > 0 else 0.0
            remaining = max(total_docs - processed, 0)
            eta_seconds = round(remaining / rate, 1) if rate > 0 else None
            print(json.dumps({
                'stage': 'graph-ingest',
                'host': host,
                'graph_name': args.graph_name,
                'cache_name': cache_name,
                'shard_index': args.shard_index,
                'shard_count': args.shard_count,
                'state_key': state_key,
                'processed': processed,
                'total_docs': total_docs,
                'indexed': indexed,
                'skipped': skipped,
                'errors': errors,
                'rate_docs_per_sec': round(rate, 4),
                'eta_seconds': eta_seconds,
                'last_source': source_ref,
            }), flush=True)
        except Exception as exc:
            errors += 1
            print(json.dumps({'stage': 'graph-ingest', 'host': host, 'error': str(exc), 'source_ref': source_ref}), flush=True)
        if args.limit and processed >= args.limit:
            if dirty:
                save_graph_state(memory_dir, args.graph_name, state, shard_key=state_key)
                dirty = False
            print(json.dumps({'done': True, 'processed': processed, 'total_docs': total_docs, 'indexed': indexed, 'skipped': skipped, 'errors': errors, 'state_key': state_key}), flush=True)
            return
    if dirty:
        save_graph_state(memory_dir, args.graph_name, state, shard_key=state_key)
    print(json.dumps({'done': True, 'processed': processed, 'total_docs': total_docs, 'indexed': indexed, 'skipped': skipped, 'errors': errors, 'state_key': state_key}), flush=True)


if __name__ == '__main__':
    main()
