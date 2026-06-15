#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path

from skmemory.agents import get_agent_paths
from skmemory.backends.skvector_backend import SKVectorBackend
from skmemory.config import SharedCorpusConfig, load_config, save_config
from skmemory.decompose import decompose_content
from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer, MemoryRole
from skmemory.recall_cache import write_source_manifest
from skmemory.retrieval import prepare_metadata

DEFAULT_EXTENSIONS = [".md", ".txt", ".json", ".yaml", ".yml"]
DEFAULT_IGNORES = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a shared corpus from a raw source root into SKVector and a recall manifest."
    )
    parser.add_argument("--agent", default="jarvis")
    parser.add_argument("--corpus-name", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--vector-collection", required=True)
    parser.add_argument("--graph-name", default="")
    parser.add_argument("--projection-profile", default="")
    parser.add_argument("--include-ext", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chunk-target", type=int, default=900)
    parser.add_argument("--chunk-overlap", type=int, default=200)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-hidden", action="store_true")
    return parser.parse_args()


def _allowed_extensions(args: argparse.Namespace) -> set[str]:
    values = args.include_ext or DEFAULT_EXTENSIONS
    return {value if value.startswith(".") else f".{value}" for value in values}


def _iter_source_files(root: Path, allowed_extensions: set[str], *, include_hidden: bool = False):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in DEFAULT_IGNORES for part in parts):
            continue
        if not include_hidden and any(part.startswith(".") for part in parts):
            continue
        if path.suffix.lower() not in allowed_extensions:
            continue
        yield path


def _infer_category(relative_path: Path) -> str:
    parts = [part for part in relative_path.parts[:-1] if part and not part.startswith(".")]
    if not parts:
        return "document"
    return parts[0].replace("_", "-").replace(" ", "-").casefold()


def _infer_type(relative_path: Path, content: str) -> str:
    joined = f"{relative_path.as_posix()}\n{content[:400]}".casefold()
    workflow_markers = (
        "checklist",
        "procedure",
        "workflow",
        "service-route",
        "runbook",
        "operational-manual",
        "setup",
        "verification",
    )
    reference_markers = (
        "guide",
        "reference",
        "overview",
        "usage",
        "template",
        "api",
        "configuration",
    )
    if any(marker in joined for marker in workflow_markers):
        return "process"
    if any(marker in joined for marker in reference_markers):
        return "guide"
    return "document"


def _shared_source(corpus_name: str) -> str:
    return f"shared-corpus:{corpus_name}"


def _shared_tags(corpus_name: str, category: str, doc_type: str) -> list[str]:
    return [
        f"shared-corpus:{corpus_name}",
        f"category:{category}",
        f"type:{doc_type}",
    ]


def _build_chunk_memory(
    *,
    title: str,
    chunk,
    source: str,
    source_ref: str,
    tags: list[str],
    metadata: dict,
    parent_id: str | None = None,
    related_ids: list[str] | None = None,
) -> Memory:
    memory = Memory(
        title=title,
        content=chunk.text,
        layer=MemoryLayer.LONG,
        role=MemoryRole.GENERAL,
        tags=tags,
        emotional=EmotionalSnapshot(),
        source=source,
        source_ref=source_ref,
        related_ids=related_ids or [],
        parent_id=parent_id,
        metadata={
            **metadata,
            "decomposition": {
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "total_chunks": chunk.total_chunks,
                "section_title": chunk.section_title,
                "citations": chunk.citations,
                "entities": chunk.entities,
                "claims": chunk.claims,
            },
        },
    )
    memory.seal()
    return memory


def _build_parent_memory(
    *,
    title: str,
    content: str,
    source: str,
    source_ref: str,
    tags: list[str],
    metadata: dict,
    decomposition,
    child_ids: list[str],
) -> Memory:
    memory = Memory(
        title=title,
        content=content if len(content) <= 10000 else (content[:200] + "..."),
        summary=content[:200] + ("..." if len(content) > 200 else ""),
        layer=MemoryLayer.LONG,
        role=MemoryRole.GENERAL,
        tags=tags + ["document-parent"],
        emotional=EmotionalSnapshot(),
        source=source,
        source_ref=source_ref,
        related_ids=child_ids,
        metadata={
            **metadata,
            "decomposition": decomposition.model_dump(exclude={"chunks"}),
            "chunk_memory_ids": child_ids,
            "original_length": len(content),
        },
    )
    memory.seal()
    return memory


def _load_agent_config(agent: str):
    cfg_path = get_agent_paths(agent)["config_yaml"]
    return cfg_path, load_config(cfg_path)


def _register_shared_corpus(args: argparse.Namespace, source_root: Path) -> Path:
    cfg_path, cfg = _load_agent_config(args.agent)
    if cfg is None:
        raise SystemExit(f"No skmemory config found for agent {args.agent}; cannot register shared corpus.")
    graph_name = args.graph_name or args.vector_collection
    existing = [item for item in cfg.shared_corpora if item.name != args.corpus_name]
    existing.append(
        SharedCorpusConfig(
            name=args.corpus_name,
            vector_collection=args.vector_collection,
            graph_name=graph_name,
            source_roots=[str(source_root)],
            projection_profile=args.projection_profile or None,
            enabled=True,
        )
    )
    cfg.shared_corpora = existing
    if args.vector_collection not in cfg.recall_collections:
        cfg.recall_collections.append(args.vector_collection)
    if graph_name not in cfg.recall_graphs:
        cfg.recall_graphs.append(graph_name)
    roots = list(cfg.recall_source_roots.get(args.vector_collection, []))
    if str(source_root) not in roots:
        roots.append(str(source_root))
    cfg.recall_source_roots[args.vector_collection] = roots
    save_config(cfg, cfg_path)
    return cfg_path


def main() -> None:
    args = parse_args()
    source_root = Path(args.source_root).expanduser().resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise SystemExit(f"source root not found: {source_root}")
    allowed_extensions = _allowed_extensions(args)
    host = socket.gethostname()

    _, cfg = _load_agent_config(args.agent)
    if cfg is None or not cfg.skvector_url:
        raise SystemExit(f"SKVector config unavailable for agent {args.agent}")
    backend = SKVectorBackend(
        url=cfg.skvector_url,
        api_key=cfg.skvector_key,
        collection=args.vector_collection,
        embedding_model=cfg.skvector_embedding_model or "mxbai-embed-large",
        vector_dim=cfg.skvector_vector_dim,
    )
    if not args.dry_run and not backend._ensure_initialized():
        raise SystemExit("SKVector backend unavailable")

    memory_dir = get_agent_paths(args.agent)["base"] / "memory"

    manifest_entries: list[dict] = []
    processed = indexed = skipped = 0
    start = time.time()

    for path in _iter_source_files(source_root, allowed_extensions, include_hidden=args.include_hidden):
        relative_path = path.relative_to(source_root)
        source_ref = relative_path.as_posix()
        try:
            content = path.read_text(errors="ignore")
        except Exception:
            skipped += 1
            continue
        if not content.strip():
            skipped += 1
            continue
        category = _infer_category(relative_path)
        doc_type = _infer_type(relative_path, content)
        tags = _shared_tags(args.corpus_name, category, doc_type)
        source = _shared_source(args.corpus_name)
        prepared_metadata = prepare_metadata(
            title=path.name,
            source=source,
            source_ref=source_ref,
            tags=tags,
            metadata={
                "file_path": source_ref,
                "filename": path.name,
                "type": doc_type,
                "category": category,
                "parent_doc": source_ref,
            },
        )
        decomposition = decompose_content(
            content,
            chunk_target=args.chunk_target,
            chunk_overlap=args.chunk_overlap,
        )
        child_ids: list[str] = []
        chunk_memories: list[Memory] = []
        for chunk in decomposition.chunks:
            title = (
                f"{path.name} [chunk {chunk.chunk_index + 1}/{chunk.total_chunks}]"
                if chunk.total_chunks > 1
                else path.name
            )
            section_tags = [f"section:{chunk.section_title}"] if chunk.section_title else []
            chunk_memory = _build_chunk_memory(
                title=title,
                chunk=chunk,
                source=source,
                source_ref=source_ref,
                tags=tags + ["decomposed", "content-chunk"] + section_tags,
                metadata=prepared_metadata,
            )
            child_ids.append(chunk_memory.id)
            chunk_memories.append(chunk_memory)
        parent_memory = _build_parent_memory(
            title=path.name,
            content=content,
            source=source,
            source_ref=source_ref,
            tags=tags + ["decomposed"],
            metadata=prepared_metadata,
            decomposition=decomposition,
            child_ids=child_ids,
        )
        for idx, chunk_memory in enumerate(chunk_memories):
            neighbours = [parent_memory.id]
            if idx > 0:
                neighbours.append(child_ids[idx - 1])
            if idx + 1 < len(child_ids):
                neighbours.append(child_ids[idx + 1])
            chunk_memory.parent_id = parent_memory.id
            chunk_memory.related_ids = neighbours
            chunk_memory.metadata["decomposition"]["parent_id"] = parent_memory.id
            chunk_memory.seal()
        if not args.dry_run:
            for chunk_memory in chunk_memories:
                backend.save(chunk_memory)
                indexed += 1
            backend.save(parent_memory)
            indexed += 1
        manifest_entries.append(
            {
                "source_ref": source_ref,
                "source_path": str(path),
                "payload": {
                    "source": source,
                    "file_path": source_ref,
                    "filename": path.name,
                    "parent_doc": source_ref,
                    "category": category,
                    "type": doc_type,
                },
            }
        )
        processed += 1
        elapsed = time.time() - start
        rate = processed / elapsed if elapsed > 0 else 0.0
        print(
            json.dumps(
                {
                    "stage": "shared-corpus-bootstrap",
                    "host": host,
                    "corpus_name": args.corpus_name,
                    "vector_collection": args.vector_collection,
                    "processed": processed,
                    "indexed": indexed,
                    "skipped": skipped,
                    "rate_docs_per_sec": round(rate, 4),
                    "last_source": source_ref,
                }
            ),
            flush=True,
        )
        if args.limit and processed >= args.limit:
            break

    manifest_path = None
    if args.write_manifest:
        manifest_path = write_source_manifest(memory_dir, args.vector_collection, manifest_entries)

    registered_path = None
    if args.register:
        registered_path = _register_shared_corpus(args, source_root)

    print(
        json.dumps(
            {
                "done": True,
                "corpus_name": args.corpus_name,
                "vector_collection": args.vector_collection,
                "graph_name": args.graph_name or args.vector_collection,
                "processed": processed,
                "indexed": indexed,
                "skipped": skipped,
                "manifest_entries": len(manifest_entries),
                "manifest_path": str(manifest_path) if manifest_path else None,
                "registered": bool(args.register),
                "registered_config": str(registered_path) if registered_path else None,
                "dry_run": bool(args.dry_run),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
