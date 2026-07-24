#!/usr/bin/env python3
"""Empirically tune (and audit) the dedup similarity threshold for the active
embedding model.

Embeds labeled near-duplicate vs. non-duplicate text pairs via the same
embedding endpoint the store uses (``SKMEMORY_EMBED_URL`` / ``SKMEMORY_EMBED_MODEL``,
default the local mxbai-embed-large server), computes cosine similarity, and
recommends a threshold that best separates duplicates from non-duplicates.

Run:  python -m skmemory.eval.tune_dedup_threshold

Result (mxbai-embed-large, 2026-07-03): near-duplicates 0.76-0.94, distinct
content 0.27-0.70 (clean gap 0.703-0.763). Chosen default: 0.73 (gap midpoint,
recall-favoring for an advisory check). Re-run this if the embedding model changes.
"""

import json
import math
import os
import urllib.request

EMBED_URL = os.environ.get("SKMEMORY_EMBED_URL", "http://192.168.0.100:11434/api/embed")
MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")

# (text_a, text_b, is_duplicate) — realistic skmemory-style content.
PAIRS = [
    (
        "Chef's mxbai embedding server runs on 192.168.0.100 port 11434.",
        "The mxbai-embed-large server is hosted at .100:11434.",
        True,
    ),
    (
        "skchat MCP is stdio-only; it has no HTTP port.",
        "The skchat MCP server communicates over stdio and exposes no HTTP endpoint.",
        True,
    ),
    (
        "Lumina merged the plugin compiler to main and pushed it to GitHub.",
        "The skskills plugin compiler was merged into main and pushed to the GitHub remote.",
        True,
    ),
    (
        "Publish stays dark by default: it needs the publish flag, --i-am-chef, and a clean scrub.",
        "Outbound publishing is disabled unless publish:true, the --i-am-chef flag, and a passing scrub gate are all present.",
        True,
    ),
    (
        "The registration engine was extracted from skmemory into a new skcore package.",
        "We moved SK*'s registration code out of skmemory and into the skcore package.",
        True,
    ),
    (
        "Redundancy mantra: if you need one, get two — no single point of failure.",
        "The 'if you need one, get two' rule: always design for HA with no SPOF.",
        True,
    ),
    (
        "MemPalace is a third-party memory project not integrated with skmemory.",
        "MemPalace is an external, third-party project that skmemory does not depend on.",
        True,
    ),
    (
        "skcapstone register now discovers plugin .mcp.json files under skskills/dist.",
        "The register command scans skskills/dist for plugin .mcp.json files.",
        True,
    ),
    (
        "The mxbai embedding server runs on 192.168.0.100 port 11434.",
        "The qwen3.6 LLM server runs on 192.168.0.100 port 8082.",
        False,
    ),
    (
        "skchat MCP is stdio-only with no HTTP port.",
        "skcomms federation runs an HTTP API on port 9384.",
        False,
    ),
    (
        "The dedup threshold should be tuned for mxbai-embed-large.",
        "The benchmark harness measures recall@10 and NDCG.",
        False,
    ),
    (
        "Lumina merged the plugin compiler to main.",
        "Jarvis is the Terminal King running on chiap04 via Hermes.",
        False,
    ),
    (
        "skcore owns the SK* registration engine.",
        "skmemory owns the sovereign memory store with pgvector and BM25.",
        False,
    ),
    (
        "Publish stays dark by default and needs three gates.",
        "The scrub gate scans for secrets and private endpoints like 127.0.0.1.",
        False,
    ),
    (
        "The mxbai embedding server runs on .100:11434.",
        "Zatarain's jambalaya is prepared with raw chicken thighs and three peppers.",
        False,
    ),
    (
        "skchat MCP communicates over stdio.",
        "The CIA's Entertainment Liaison Office influenced Hollywood films.",
        False,
    ),
    (
        "The registration engine moved to skcore.",
        "Cloud 9 is the emotional continuity protocol at depth 9, trust 0.97.",
        False,
    ),
    (
        "skskills plugin compiler shipped to GitHub.",
        "The nootropic stack lives on the david.knestrick Gmail calendar.",
        False,
    ),
]


def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"model": MODEL, "input": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["embeddings"][0]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def main() -> None:
    dup_sims, non_sims, rows = [], [], []
    for a, b, is_dup in PAIRS:
        sim = cosine(embed(a), embed(b))
        (dup_sims if is_dup else non_sims).append(sim)
        rows.append((sim, is_dup, a[:44]))

    rows.sort(reverse=True)
    print(f"model={MODEL}  url={EMBED_URL}\n{'sim':>6}  {'label':<8}  text_a")
    for sim, is_dup, a in rows:
        print(f"{sim:6.3f}  {'DUP' if is_dup else 'non-dup':<8}  {a}")

    dmin, nmax = min(dup_sims), max(non_sims)
    print("\n── distribution ──")
    print(
        f"  duplicates:     min={dmin:.3f}  max={max(dup_sims):.3f}  mean={sum(dup_sims) / len(dup_sims):.3f}  n={len(dup_sims)}"
    )
    print(
        f"  non-duplicates: min={min(non_sims):.3f}  max={nmax:.3f}  mean={sum(non_sims) / len(non_sims):.3f}  n={len(non_sims)}"
    )
    print(
        f"  gap: max-nondup({nmax:.3f}) .. min-dup({dmin:.3f})  ->  {'CLEAN' if dmin > nmax else 'OVERLAP'}"
    )
    if dmin > nmax:
        print(f"  RECOMMENDED threshold (gap midpoint): {round((dmin + nmax) / 2, 2)}")


if __name__ == "__main__":
    main()
