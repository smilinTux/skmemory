#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Plan shard assignments for distributed recall-cache workers.')
    parser.add_argument('--shards', type=int, default=32)
    parser.add_argument('--worker', action='append', default=[], help='Worker spec in host:weight form. Repeat for each worker.')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    specs: list[tuple[str, int]] = []
    for item in args.worker:
        host, _, weight_text = item.partition(':')
        weight = int(weight_text or '1')
        specs.append((host, max(weight, 1)))
    if not specs:
        specs = [('chiap01', 8)]
    slots: list[str] = []
    for host, weight in specs:
        slots.extend([host] * weight)
    assignments: dict[str, list[int]] = {host: [] for host, _ in specs}
    for shard in range(args.shards):
        host = slots[shard % len(slots)]
        assignments.setdefault(host, []).append(shard)
    print(json.dumps({'shards': args.shards, 'workers': specs, 'assignments': assignments}, indent=2))


if __name__ == '__main__':
    main()
