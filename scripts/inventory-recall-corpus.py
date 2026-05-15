#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from skmemory.corpus_registry import build_corpus_registry_report, inventory_cache_namespace
from skmemory.context_loader import LazyMemoryLoader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Inventory shared corpora and recall-cache coverage.')
    parser.add_argument('--agent', default='jarvis')
    parser.add_argument('--name', action='append', default=[], help='Filter by shared corpus name, vector collection, or graph name.')
    parser.add_argument('--graph-name', action='append', default=[], help='Legacy filter alias; matches shared corpus vector collection cache namespaces.')
    parser.add_argument('--pretty', action='store_true')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    filters = list(args.name or []) + list(args.graph_name or [])
    report = build_corpus_registry_report(agent=args.agent, names=filters or None)

    if args.graph_name and not args.name:
        loader = LazyMemoryLoader(args.agent)
        memory_dir = loader.paths['base'] / 'memory'
        report = {
            'agent': args.agent,
            'memory_dir': str(memory_dir),
            'graphs': [inventory_cache_namespace(memory_dir, graph_name) for graph_name in args.graph_name],
        }

    if args.pretty:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
