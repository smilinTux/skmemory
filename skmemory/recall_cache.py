"""Helpers for distributed recall corpus decomposition and graph projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .decompose import decompose_content
from .models import Memory, MemoryLayer
from .retrieval import prepare_metadata

CACHE_VERSION = 3


PROJECTION_PROFILE_DEFAULT = "default"
PROJECTION_PROFILE_LEGAL = "legal-retrieval"
PROJECTION_PROFILE_REFERENCE = "reference-retrieval"
PROJECTION_PROFILE_WORKFLOW = "workflow-retrieval"


def _unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        clean = str(item).strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(clean)
    return ordered


def source_cache_key(source_ref: str) -> str:
    return hashlib.sha256(source_ref.encode("utf-8")).hexdigest()


def shard_for_source(source_ref: str, shard_count: int) -> int:
    if shard_count <= 1:
        return 0
    digest = hashlib.sha256(source_ref.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def cache_root(memory_dir: Path, graph_name: str) -> Path:
    return memory_dir / "recall-cache" / graph_name


def cache_doc_path(memory_dir: Path, graph_name: str, source_ref: str) -> Path:
    key = source_cache_key(source_ref)
    root = cache_root(memory_dir, graph_name)
    return root / key[:2] / f"{key}.json"


def graph_state_path(memory_dir: Path, graph_name: str, shard_key: str | None = None) -> Path:
    filename = "graph-state.json" if not shard_key else f"graph-state-{shard_key}.json"
    return cache_root(memory_dir, graph_name) / filename


def manifest_path(memory_dir: Path, graph_name: str) -> Path:
    return cache_root(memory_dir, graph_name) / "source-manifest.json"


def load_graph_state(memory_dir: Path, graph_name: str, shard_key: str | None = None) -> dict[str, str]:
    path = graph_state_path(memory_dir, graph_name, shard_key=shard_key)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_graph_state(memory_dir: Path, graph_name: str, state: dict[str, str], shard_key: str | None = None) -> None:
    path = graph_state_path(memory_dir, graph_name, shard_key=shard_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def build_source_manifest(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        source_ref = str(entry.get("source_ref", "")).strip()
        if not source_ref or source_ref in collapsed:
            continue
        payload = dict(entry.get("payload") or {})
        normalized = {
            "source_ref": source_ref,
            "payload": payload,
        }
        source_path = str(entry.get("source_path") or "").strip()
        if source_path:
            normalized["source_path"] = source_path
        collapsed[source_ref] = normalized
    return [collapsed[key] for key in sorted(collapsed)]


def load_source_manifest(memory_dir: Path, graph_name: str) -> list[dict[str, Any]]:
    path = manifest_path(memory_dir, graph_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return build_source_manifest(data)


def write_source_manifest(memory_dir: Path, graph_name: str, entries: Iterable[dict[str, Any]]) -> Path:
    path = manifest_path(memory_dir, graph_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_source_manifest(entries)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


def compute_source_fingerprint(source_path: Path | None, payload: dict[str, Any]) -> str:
    if source_path is not None and source_path.exists():
        stat = source_path.stat()
        basis: dict[str, Any] = {
            "path": str(source_path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "category": payload.get("category"),
            "type": payload.get("type"),
        }
    else:
        basis = dict(payload)
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _score_entity(value: str) -> tuple[int, int, str]:
    text = value.strip()
    words = text.split()
    lowered = text.casefold()
    legal_bonus = 2 if any(marker in lowered for marker in ("ucc", "usc", "cfr", "trust", "court", "bank", "postal", "credit", "debtor", "secured", "treasurydirect", "treasury")) else 0
    titleish_bonus = 1 if any(ch.isupper() for ch in text) and len(words) > 1 else 0
    return (legal_bonus + titleish_bonus, len(words), lowered)


def _score_claim(value: str) -> tuple[int, int, str]:
    text = value.strip()
    lowered = text.casefold()
    legal_bonus = sum(1 for marker in ("shall", "must", "may", "holder", "debtor", "secured", "trust", "court", "jurisdiction", "service", "levy", "lien") if marker in lowered)
    return (legal_bonus, len(text), lowered)


def _path_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).casefold()


def infer_projection_profile(
    graph_name: str = "",
    source_ref: str = "",
    payload: dict[str, Any] | None = None,
    requested_profile: str | None = None,
) -> str:
    payload = payload or {}
    requested = str(requested_profile or "").strip().casefold()
    if requested:
        if requested in {PROJECTION_PROFILE_DEFAULT, "default"}:
            return PROJECTION_PROFILE_DEFAULT
        if requested in {PROJECTION_PROFILE_LEGAL, "legal", "legal-recall", "legal_retrieval"}:
            return PROJECTION_PROFILE_LEGAL
        if requested in {PROJECTION_PROFILE_REFERENCE, "reference", "reference-recall", "reference_retrieval"}:
            return PROJECTION_PROFILE_REFERENCE
        if requested in {PROJECTION_PROFILE_WORKFLOW, "workflow", "workflow-recall", "workflow_retrieval", "process"}:
            return PROJECTION_PROFILE_WORKFLOW
    joined = _path_text(graph_name, source_ref, payload.get("category"), payload.get("type"))
    if graph_name.casefold().startswith("hammertime") or "hammertime" in joined:
        return PROJECTION_PROFILE_LEGAL
    if any(marker in joined for marker in ("reference", "knowledge", "guides", "template", "templates", "reference-books", "byteclave")):
        return PROJECTION_PROFILE_REFERENCE
    if any(marker in joined for marker in ("workflow", "process", "protocol", "playbook", "runbook")):
        return PROJECTION_PROFILE_WORKFLOW
    return PROJECTION_PROFILE_DEFAULT


def _is_hammertime_projection(graph_name: str, source_ref: str, payload: dict[str, Any], requested_profile: str | None = None) -> bool:
    return infer_projection_profile(graph_name, source_ref, payload, requested_profile) == PROJECTION_PROFILE_LEGAL


def _looks_legal_entity(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("ucc", "u.s.c", "usc", "c.f.r", "cfr", "internal revenue", "postal", "treasury", "treasurydirect", "trust", "court", "clerk", "debtor", "creditor", "secured", "lien", "levy", "notary", "executor", "bank", "state", "united states")):
        return True
    words = value.split()
    return len(words) >= 2 and any(ch.isupper() for ch in value)


def _looks_legal_claim(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("shall", "must", "may", "holder", "debtor", "secured", "trust", "court", "jurisdiction", "service", "levy", "lien", "notary", "executor", "affidavit", "notice", "claim", "rebut", "postal", "treasury")):
        return True
    return "§" in value or " ucc " in f" {lowered} " or " usc " in f" {lowered} " or " cfr " in f" {lowered} "


def _looks_legal_section(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in ("section", "article", "chapter", "notice", "demand", "claim", "affidavit", "jurisdiction", "service", "levy", "lien", "trust", "executor", "postal", "treasury", "ucc"))


def _looks_reference_entity(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith("generic entity"):
        return False
    if any(marker in lowered for marker in ("api", "sdk", "tool", "service", "endpoint", "platform", "framework", "registry", "template", "guide", "reference", "library")):
        return True
    return len(value.split()) >= 2 and any(ch.isupper() for ch in value)


def _looks_reference_claim(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith("generic claim"):
        return False
    return any(marker in lowered for marker in ("supports", "provides", "includes", "allows", "returns", "uses", "requires", "endpoint", "parameter", "configuration", "template", "reference"))


def _looks_reference_section(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in ("overview", "usage", "reference", "api", "configuration", "example", "examples", "template", "parameter", "tool", "endpoint"))


def _looks_workflow_entity(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith("generic entity"):
        return False
    return any(marker in lowered for marker in ("step", "agent", "service", "queue", "task", "workflow", "process", "pipeline", "job", "stage", "state", "trigger"))


def _looks_workflow_claim(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith("generic claim"):
        return False
    return any(marker in lowered for marker in ("step", "run", "execute", "verify", "check", "start", "stop", "restart", "schedule", "route", "complete", "handoff", "pipeline", "workflow"))


def _looks_workflow_section(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in ("step", "workflow", "process", "runbook", "procedure", "checklist", "setup", "verification", "operation", "routing"))


def _projection_limits(graph_name: str, source_ref: str, payload: dict[str, Any], requested_profile: str | None = None) -> dict[str, int]:
    profile = infer_projection_profile(graph_name, source_ref, payload, requested_profile)
    if profile == PROJECTION_PROFILE_LEGAL:
        return {
            "citations": 128,
            "sections": 32,
            "entities": 72,
            "claims": 48,
        }
    if profile == PROJECTION_PROFILE_REFERENCE:
        return {
            "citations": 48,
            "sections": 56,
            "entities": 96,
            "claims": 72,
        }
    if profile == PROJECTION_PROFILE_WORKFLOW:
        return {
            "citations": 24,
            "sections": 48,
            "entities": 80,
            "claims": 96,
        }
    return {
        "citations": 96,
        "sections": 64,
        "entities": 192,
        "claims": 160,
    }


def project_decomposition(
    decomposition: dict[str, Any],
    *,
    graph_name: str = "",
    source_ref: str = "",
    payload: dict[str, Any] | None = None,
    projection_profile: str | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    resolved_profile = infer_projection_profile(graph_name, source_ref, payload, projection_profile)
    limits = _projection_limits(graph_name, source_ref, payload, resolved_profile)
    all_citations = _unique_preserve(decomposition.get("citations", []))
    raw_sections = _unique_preserve(decomposition.get("section_titles", []))
    raw_entities = _unique_preserve(decomposition.get("entities", []))
    raw_claims = _unique_preserve(decomposition.get("claims", []))
    citations = all_citations[: limits["citations"]]
    if resolved_profile == PROJECTION_PROFILE_LEGAL:
        section_titles = [value for value in raw_sections if _looks_legal_section(value)][: limits["sections"]]
        if not section_titles:
            section_titles = raw_sections[: limits["sections"]]
        entities = sorted([value for value in raw_entities if _looks_legal_entity(value)], key=_score_entity, reverse=True)[: limits["entities"]]
        if len(entities) < min(16, limits["entities"]):
            fallback_entities = [value for value in sorted(raw_entities, key=_score_entity, reverse=True) if value not in entities]
            entities = (entities + fallback_entities)[: limits["entities"]]
        claims = sorted([value for value in raw_claims if _looks_legal_claim(value)], key=_score_claim, reverse=True)[: limits["claims"]]
        if len(claims) < min(12, limits["claims"]):
            fallback_claims = [value for value in sorted(raw_claims, key=_score_claim, reverse=True) if value not in claims]
            claims = (claims + fallback_claims)[: limits["claims"]]
    elif resolved_profile == PROJECTION_PROFILE_REFERENCE:
        section_titles = [value for value in raw_sections if _looks_reference_section(value)][: limits["sections"]]
        if len(section_titles) < min(16, limits["sections"]):
            section_titles = raw_sections[: limits["sections"]]
        entities = sorted([value for value in raw_entities if _looks_reference_entity(value)], key=_score_entity, reverse=True)[: limits["entities"]]
        if len(entities) < min(24, limits["entities"]):
            fallback_entities = [value for value in sorted(raw_entities, key=_score_entity, reverse=True) if value not in entities]
            entities = (entities + fallback_entities)[: limits["entities"]]
        claims = sorted([value for value in raw_claims if _looks_reference_claim(value)], key=_score_claim, reverse=True)[: limits["claims"]]
        if len(claims) < min(20, limits["claims"]):
            fallback_claims = [value for value in sorted(raw_claims, key=_score_claim, reverse=True) if value not in claims]
            claims = (claims + fallback_claims)[: limits["claims"]]
    elif resolved_profile == PROJECTION_PROFILE_WORKFLOW:
        section_titles = [value for value in raw_sections if _looks_workflow_section(value)][: limits["sections"]]
        if len(section_titles) < min(16, limits["sections"]):
            section_titles = raw_sections[: limits["sections"]]
        entities = sorted([value for value in raw_entities if _looks_workflow_entity(value)], key=_score_entity, reverse=True)[: limits["entities"]]
        if len(entities) < min(16, limits["entities"]):
            fallback_entities = [value for value in sorted(raw_entities, key=_score_entity, reverse=True) if value not in entities]
            entities = (entities + fallback_entities)[: limits["entities"]]
        claims = sorted([value for value in raw_claims if _looks_workflow_claim(value)], key=_score_claim, reverse=True)[: limits["claims"]]
        if len(claims) < min(20, limits["claims"]):
            fallback_claims = [value for value in sorted(raw_claims, key=_score_claim, reverse=True) if value not in claims]
            claims = (claims + fallback_claims)[: limits["claims"]]
    else:
        section_titles = raw_sections[: limits["sections"]]
        entities = sorted(raw_entities, key=_score_entity, reverse=True)[: limits["entities"]]
        claims = sorted(raw_claims, key=_score_claim, reverse=True)[: limits["claims"]]
    return {
        "chunk_target": decomposition.get("chunk_target", 900),
        "chunk_overlap": decomposition.get("chunk_overlap", 200),
        "section_titles": section_titles,
        "citations": citations,
        "entities": entities,
        "claims": claims,
        "projection_profile": resolved_profile,
        "projection_counts": {
            "section_titles": len(section_titles),
            "citations": len(citations),
            "entities": len(entities),
            "claims": len(claims),
        },
        "full_counts": {
            "section_titles": len(raw_sections),
            "citations": len(all_citations),
            "entities": len(raw_entities),
            "claims": len(raw_claims),
        },
    }


def build_cache_document(*, graph_name: str, source_ref: str, source_path: Path, payload: dict[str, Any], host: str = "", decompose_kwargs: dict[str, Any] | None = None, projection_profile: str | None = None) -> dict[str, Any]:
    content = source_path.read_text(errors="ignore")
    decomposition = decompose_content(content, **(decompose_kwargs or {})).model_dump()
    projection = project_decomposition(
        decomposition,
        graph_name=graph_name,
        source_ref=source_ref,
        payload=payload,
        projection_profile=projection_profile,
    )
    fingerprint = compute_source_fingerprint(source_path, payload)
    return {
        "cache_version": CACHE_VERSION,
        "graph_name": graph_name,
        "source_collection": graph_name,
        "source_ref": source_ref,
        "source_path": str(source_path),
        "source_name": source_path.name,
        "category": payload.get("category"),
        "type": payload.get("type"),
        "fingerprint": fingerprint,
        "host": host,
        "content_chars": len(content),
        "summary": content[:400] + ("..." if len(content) > 400 else ""),
        "content_preview": content[:4000],
        "decomposition": decomposition,
        "projection": projection,
    }


def write_cache_document(memory_dir: Path, graph_name: str, source_ref: str, cache_doc: dict[str, Any]) -> Path:
    path = cache_doc_path(memory_dir, graph_name, source_ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache_doc, indent=2, sort_keys=True))
    return path


def load_cache_document(memory_dir: Path, graph_name: str, source_ref: str) -> dict[str, Any] | None:
    path = cache_doc_path(memory_dir, graph_name, source_ref)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def iter_cache_documents(memory_dir: Path, graph_name: str):
    root = cache_root(memory_dir, graph_name)
    if not root.exists():
        return
    for path in sorted(root.rglob("*.json")):
        if path.name == "source-manifest.json" or path.name.startswith("graph-state"):
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            yield path, data


def memory_from_cache_document(cache_doc: dict[str, Any], *, target_graph_name: str | None = None) -> Memory:
    source_ref = str(cache_doc.get("source_ref", ""))
    source_collection = str(cache_doc.get("source_collection") or cache_doc.get("graph_name", ""))
    graph_name = str(target_graph_name or cache_doc.get("target_graph_name") or cache_doc.get("graph_name", ""))
    projection = dict(cache_doc.get("projection") or {})
    projection_profile = str(projection.get("projection_profile") or PROJECTION_PROFILE_DEFAULT)
    tags = _unique_preserve([
        "recall-graph",
        f"recall:{graph_name}",
        f"recall-source:{source_collection}",
        f"projection:{projection_profile}",
        "category:" + str(cache_doc.get("category", "document")),
        "type:" + str(cache_doc.get("type", "document")),
    ])
    metadata = prepare_metadata(
        title=str(cache_doc.get("source_name", source_ref or "document")),
        source=f"recall:{graph_name}",
        source_ref=source_ref,
        tags=tags,
        metadata={
            "file_path": source_ref,
            "parent_doc": source_ref,
            "category": cache_doc.get("category"),
            "type": cache_doc.get("type"),
            "decomposition": projection,
            "source_collection": source_collection,
            "target_graph": graph_name,
            "projection_profile": projection_profile,
            "cache_version": cache_doc.get("cache_version", CACHE_VERSION),
            "cache_fingerprint": cache_doc.get("fingerprint", ""),
            "cache_host": cache_doc.get("host", ""),
        },
    )
    memory = Memory(
        id=hashlib.sha256(f"{graph_name}:{source_ref}".encode("utf-8")).hexdigest()[:16],
        title=str(cache_doc.get("source_name", source_ref or "document")),
        content=str(cache_doc.get("content_preview", "")),
        summary=str(cache_doc.get("summary", "")),
        layer=MemoryLayer.LONG,
        tags=tags,
        source=f"recall:{graph_name}",
        source_ref=source_ref,
        metadata=metadata,
    )
    memory.seal()
    return memory
