"""
Lazy Memory Context Loader - Three-Tier Memory Architecture.

Loads memories efficiently based on date tiers to optimize token usage:
- TODAY: Full content (active work)
- YESTERDAY: Summaries only (recent context)
- HISTORICAL: Reference count (deep search available)

Usage:
    loader = LazyMemoryLoader("lumina")
    context = loader.load_active_context()  # Token-optimized

    # Deep search when needed
    results = loader.deep_search("project gentis")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from .agents import get_agent_paths
from .backends.sqlite_backend import SQLiteBackend
from .recall_cache import (
    build_cache_document,
    compute_source_fingerprint,
    load_cache_document,
    memory_from_cache_document,
    write_cache_document,
)

logger = logging.getLogger(__name__)

_HAMMERTIME_RECALL_ROOTS = (
    Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime"),
    Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime/reference"),
)
_RE_LEGAL_PROBE = re.compile(r"\b(?:\d{1,3}\s+(?:U\.?S\.?C\.?|C\.?F\.?R\.?)\s+(?:§+\s*)?\d[\w\.\-\(\)]*|U\.?C\.?C\.?\s*(?:§+\s*)?\d[\w\.\-]*|§+\s*\d[\w\.\-\(\)]*|[A-Z][A-Z\.]+\s+\d[\w\.\-]*|\d+-\d+[A-Za-z0-9\-]*)", re.IGNORECASE)

def _unique_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        clean = item.strip()
        if len(clean) < 3:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(clean)
    return ordered

def _extract_legal_probes(query: str) -> list[str]:
    probes = [query.strip()]
    try:
        from .decompose import decompose_content
        pivot = decompose_content(query, chunk_target=max(240, len(query) + 48), chunk_overlap=0)
        probes.extend(pivot.citations)
        probes.extend(pivot.entities)
        probes.extend(pivot.section_titles)
    except Exception:
        pass
    probes.extend(match.group(0) for match in _RE_LEGAL_PROBE.finditer(query))
    bare_sections = re.findall(r"\b\d+-\d+[A-Za-z0-9\-]*\b", query)
    if bare_sections and "ucc" in query.casefold():
        probes.extend("UCC " + section for section in bare_sections)
        probes.extend("§ " + section for section in bare_sections)
    return _unique_preserve(probes)

def _default_recall_source_roots(name: str) -> list[Path]:
    if name.casefold().startswith("hammertime"):
        return [root for root in _HAMMERTIME_RECALL_ROOTS if root.exists()]
    return []


def _normalize_legal_match_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    cleaned = re.sub(r"(?i)\bsection\s+(\d[\w\.\-\(\)]*)", r"§ \1", cleaned)
    cleaned = re.sub(r"§{2,}", "§", cleaned)
    cleaned = re.sub(r"(?i)\bu\.?\s*c\.?\s*c\.?\s*", "UCC ", cleaned)
    return cleaned


def _graph_backend_priority(source_backend: str) -> int:
    backend = (source_backend or "").split(":")[0]
    priorities = {
        "skgraph_citation": 5,
        "skgraph_claim": 4,
        "skgraph_section": 3,
        "skgraph_entity": 2,
        "skgraph_tags": 1,
        "skgraph": 0,
    }
    return priorities.get(backend, -1)


def _score_recall_entity(value: str) -> tuple[int, int, str]:
    text = value.strip()
    words = text.split()
    legal_bonus = 1 if any(marker in text.casefold() for marker in ("ucc", "usc", "cfr", "trust", "court", "bank", "postal", "credit", "debtor", "secured")) else 0
    titleish_bonus = 1 if any(ch.isupper() for ch in text) and len(words) > 1 else 0
    return (legal_bonus + titleish_bonus, len(words), text.casefold())


def _score_recall_claim(value: str) -> tuple[int, int, str]:
    text = value.strip()
    lowered = text.casefold()
    legal_bonus = sum(1 for marker in ("shall", "must", "may", "holder", "debtor", "secured", "trust", "court", "jurisdiction", "service", "levy", "lien") if marker in lowered)
    return (legal_bonus, len(text), lowered)


def _prune_recall_decomposition(decomposition) -> dict:
    citations = _unique_preserve(decomposition.citations)[:96]
    section_titles = _unique_preserve(decomposition.section_titles)[:64]
    entities = sorted(_unique_preserve(decomposition.entities), key=_score_recall_entity, reverse=True)[:192]
    claims = sorted(_unique_preserve(decomposition.claims), key=_score_recall_claim, reverse=True)[:160]
    return {
        "chunk_target": decomposition.chunk_target,
        "chunk_overlap": decomposition.chunk_overlap,
        "section_titles": section_titles,
        "citations": citations,
        "entities": entities,
        "claims": claims,
    }


def _is_legal_citation_probe(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    return bool(re.search(r"(?i)(?:\b(?:u\.?s\.?c\.?|c\.?f\.?r\.?|u\.?c\.?c\.?))|§|\b\d+-\d+[A-Za-z0-9\-]*\b", candidate))


def _structured_graph_probes(query: str) -> dict[str, list[str]]:
    probes = _extract_legal_probes(query)
    citation_probes = _unique_preserve([probe for probe in probes if _is_legal_citation_probe(probe)])
    semantic_probes = _unique_preserve([query] + [probe for probe in probes if not _is_legal_citation_probe(probe) and len(probe.split()) > 1])
    return {
        "citation": citation_probes or [query],
        "section": citation_probes or [query],
        "claim": semantic_probes[:4] or [query],
        "entity": semantic_probes[:4] or [query],
    }


@dataclass
class MemoryContext:
    """Container for loaded memory context."""

    today_memories: list[dict]  # Full memories
    yesterday_summaries: list[dict]  # Summaries only
    historical_count: int  # Reference count only

    def to_context_string(self, max_tokens: int = 3000) -> str:
        """Convert to token-optimized context string."""
        sections = []

        # Today's memories (full content)
        if self.today_memories:
            sections.append(f"## Today's Memories ({len(self.today_memories)})")
            for mem in self.today_memories[:20]:  # Limit to 20
                content = mem.get("content", "")[:200]  # Truncate if needed
                line = f"- {mem.get('title', 'Untitled')}: {content}"
                sections.append(line)
                # Add related context if present
                if mem.get("related_context"):
                    for rel in mem["related_context"]:
                        sections.append(f"  → {rel['edge']}: {rel['title']} [{rel['layer']}]")
                if mem.get("entities"):
                    sections.append(f"  entities: {', '.join(mem['entities'])}")

        # Yesterday's summaries
        if self.yesterday_summaries:
            sections.append(f"\n## Yesterday ({len(self.yesterday_summaries)} memories)")
            for mem in self.yesterday_summaries[:10]:  # Limit to 10
                summary = mem.get("summary", "No summary")[:150]
                sections.append(f"- {mem.get('title', 'Untitled')}: {summary}")

        # Historical reference
        if self.historical_count > 0:
            sections.append("\n## Historical Memory")
            sections.append(f"- {self.historical_count} long-term memories available")
            sections.append("- Use 'search memory [query]' to recall specific details")

        return "\n".join(sections)


def _read_yaml_file(path: Path) -> dict | None:
    """Load a YAML file, return dict or None on missing/error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Could not load %s: %s", path.name, e)
        return None


def _load_skvector_config(config_dir: Path) -> dict | None:
    """Load skvector config: try skvector.yaml first, then skmemory.yaml inline,
    then skmemory.yaml flat keys (legacy migration path)."""
    # 1. Dedicated skvector.yaml
    path = config_dir / "skvector.yaml"
    if path.exists():
        cfg = _read_yaml_file(path)
        if cfg and cfg.get("enabled", False):
            return cfg

    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}

    # 2. Fallback: inline backends.skvector section in skmemory.yaml
    inline = skmem.get("backends", {}).get("skvector", {})
    if inline and inline.get("enabled", False):
        ext_cfg_path = inline.get("config")
        if ext_cfg_path:
            resolved = Path(ext_cfg_path).expanduser()
            if resolved.exists():
                ext = _read_yaml_file(resolved)
                if ext and ext.get("enabled", False):
                    return ext
        if inline.get("host") or inline.get("url"):
            return inline

    # 3. Legacy flat keys: skvector_url / skvector_key / ... in skmemory.yaml
    #    Enabled when backends_enabled list includes 'skvector' (or key is present).
    backends_enabled = skmem.get("backends_enabled", [])
    url = skmem.get("skvector_url")
    if url and ("skvector" in backends_enabled or skmem.get("skvector_key")):
        return {
            "enabled": True,
            "url": url,
            "api_key": skmem.get("skvector_key"),
            "collection": skmem.get("skvector_collection", "skmemory"),
            "embedding": {
                "provider": "sentence_transformers",
                "model": skmem.get("skvector_embedding_model", "all-MiniLM-L6-v2"),
            },
        }

    return None


def _load_skgraph_config(config_dir: Path) -> dict | None:
    """Load skgraph config: try skgraph.yaml first, then skmemory.yaml inline,
    then skmemory.yaml flat keys (legacy migration path)."""
    # 1. Dedicated skgraph.yaml
    path = config_dir / "skgraph.yaml"
    if path.exists():
        cfg = _read_yaml_file(path)
        if cfg and cfg.get("enabled", False):
            return cfg

    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}

    # 2. Fallback: inline backends.skgraph section in skmemory.yaml
    inline = skmem.get("backends", {}).get("skgraph", {})
    if inline and inline.get("enabled", False):
        ext_cfg_path = inline.get("config")
        if ext_cfg_path:
            resolved = Path(ext_cfg_path).expanduser()
            if resolved.exists():
                ext = _read_yaml_file(resolved)
                if ext and ext.get("enabled", False):
                    return ext
        if inline.get("host") or inline.get("url"):
            return inline

    # 3. Legacy flat keys: skgraph_url / skgraph_graph_name / ... in skmemory.yaml
    backends_enabled = skmem.get("backends_enabled", [])
    url = skmem.get("skgraph_url")
    if url and ("skgraph" in backends_enabled or skmem.get("skgraph_graph_name")):
        return {
            "enabled": True,
            "url": url,
            "graph_name": skmem.get("skgraph_graph_name", "skmemory"),
        }

    return None


# ---------------------------------------------------------------------------
# skcomms T9: realm-aware, consent-gated recall_collections namespacing.
#
# Collections live in the central pgvector store (skmem-pg).  This module only
# implements the NAMESPACING + CONSENT-GATING LOGIC — it never queries or
# writes skmem-pg.  Resolution rules (per configured collection name):
#
#   * bare ``legal-corpus``               -> ``<operator>.<realm>/legal-corpus``
#                                            (own-operator namespace, auto-prefixed)
#   * ``chef.skworld/legal-corpus``       -> left as-is (already qualified)
#   * ``peer:acme.world/secret-corpus``   -> FOREIGN reference.  Dropped (with a
#                                            logged warning) UNLESS a valid,
#                                            unexpired consent token grants read
#                                            on that exact ``<operator>.<realm>/
#                                            <collection>`` to this agent's fqid.
#
# Fail-CLOSED: a missing / empty / malformed consent file drops ALL foreign
# refs.  Without an operator/realm (no cluster.json) we cannot namespace, so
# bare names pass through untouched and foreign refs still fail closed.
# ---------------------------------------------------------------------------

# Consent-file schema (T10 will produce + sign these; T9 only reads them):
#   ${SKCOMMS_HOME:-~/.skcomms}/recall_collections_consent.json
#   {
#     "tokens": [
#       {
#         "collection":  "<operator>.<realm>/<name>",  # exact foreign collection
#         "granted_to":  "<fqid>",                     # must == this agent's fqid
#         "granted_by":  "<fqid>",                     # foreign operator's agent
#         "expires":     "<iso8601>",                  # must be in the future
#         "signature":   "<pgp armor>"                 # PGP-verified via skcomms.grants
#       }
#     ]
#   }
_PEER_PREFIX = "peer:"
_CONSENT_FILENAME = "recall_collections_consent.json"

# cluster.json search path — mirrors capauth.agent_identity so operator/realm
# resolution stays consistent across SK packages.
_CLUSTER_JSON_PATHS = (
    Path("/etc/skcapstone/cluster.json"),
    Path("~/.skcapstone/cluster.json").expanduser(),
)


def _skcomms_home() -> Path:
    """Resolve the skcomms home dir, honoring the SKCOMMS_HOME override."""
    override = os.environ.get("SKCOMMS_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.skcomms").expanduser()


def _read_cluster_config() -> dict | None:
    """Load cluster.json (operator/realm) from the standard search path.

    Returns the parsed dict, or None if no readable cluster.json exists.
    Mirrors capauth's search order; a local reader so skmemory has no hard
    dependency on capauth for namespacing.
    """
    for path in _CLUSTER_JSON_PATHS:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                return data if isinstance(data, dict) else None
        except Exception as e:  # malformed / unreadable — try next, then None
            logger.warning("Could not read cluster.json at %s: %s", path, e)
    return None


def _operator_realm() -> tuple[str | None, str | None]:
    """Return (operator, realm) from cluster.json, or (None, None)."""
    cluster = _read_cluster_config()
    if not isinstance(cluster, dict):
        return (None, None)
    operator = cluster.get("operator")
    realm = cluster.get("realm")
    return (operator or None, realm or None)


def _resolve_agent_fqid(agent_name: str | None = None) -> str | None:
    """Resolve the running agent's fqid (``<agent>@<operator>.<realm>``).

    Prefers capauth's canonical resolver; falls back to deriving it from
    SKAGENT (env) + cluster.json operator/realm.  Returns None when the
    realm is unknowable (so foreign refs fail closed).
    """
    # 1. Canonical resolver (graceful import — capauth is optional here).
    try:
        from capauth import resolve_agent_identity

        ident = resolve_agent_identity(agent_name)
        if getattr(ident, "fqid", None):
            return ident.fqid
    except Exception:
        pass

    # 2. Local fallback: SKAGENT + cluster.json.
    agent = (
        agent_name
        or os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
    )
    operator, realm = _operator_realm()
    if agent and operator and realm:
        return f"{agent}@{operator}.{realm}"
    return None


def _verify_consent_signature(token: dict) -> bool:
    """Verify the PGP ``signature`` armor on a consent token (T10 wiring).

    Delegates to :func:`skcomms.grants.verify_grant`, which checks the detached
    PGP signature over the token's canonical bytes against the granter's public
    key, TOFU-trusts that key for ``granted_by`` (rejecting fingerprint
    conflicts), and re-checks expiry.  The granter's public key is resolved by
    skcomms itself from its TOFU/peer store (``known_fingerprints.json`` cached
    pubkey, falling back to ``<SKCOMMS_HOME>/peers/<fqid>.asc``) keyed on
    ``granted_by`` — skmemory never touches a private keyring.

    sk-integration dual-mode: skcomms is an OPTIONAL dependency.  If it cannot
    be imported (standalone skmemory), we **fail closed** — foreign refs are
    rejected — and log a single clear reason.  skmemory never crashes when
    skcomms is absent.

    Returns:
        True iff skcomms is present AND the token's signature verifies, its
        granter key is TOFU-trusted, and it is unexpired.  False otherwise.
    """
    try:
        from skcomms.grants import verify_grant
    except ImportError as exc:
        logger.warning(
            "Consent signature verification unavailable (skcomms not "
            "importable: %s) — failing closed on foreign recall_collection.",
            exc,
        )
        return False

    try:
        result = verify_grant(token)
    except Exception as exc:  # defensive: never let a verifier bug open the gate
        logger.warning(
            "Consent signature verification raised (%s) — failing closed.", exc
        )
        return False

    if not result.valid:
        logger.warning(
            "Consent token for %r rejected by skcomms: %s",
            token.get("collection"),
            result.reason,
        )
    return bool(result.valid)


def _consent_grants_read(
    collection: str, agent_fqid: str | None, skcomms_home: Path
) -> bool:
    """Return True iff a valid consent token grants ``agent_fqid`` read on
    ``collection`` (an exact ``<operator>.<realm>/<name>`` string).

    Fail-CLOSED on every failure path: no fqid, missing / empty / malformed
    consent file, no matching token, expired token, wrong grantee, or a
    failed (future) signature verification.
    """
    if not agent_fqid:
        return False

    path = skcomms_home / _CONSENT_FILENAME
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        logger.warning("Malformed consent file %s — failing closed: %s", path, e)
        return False
    if not isinstance(data, dict):
        return False
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        return False

    now = datetime.now(timezone.utc)
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if token.get("collection") != collection:
            continue
        if token.get("granted_to") != agent_fqid:
            continue
        expires = token.get("expires")
        if not expires:
            continue
        try:
            exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if exp_dt <= now:
            continue
        if not _verify_consent_signature(token):  # PGP verify via skcomms (fail-closed)
            continue
        return True
    return False


def _load_recall_collections(config_dir: Path) -> list[str]:
    """Return the realm-aware, consent-gated recall_collections list.

    Reads ``recall_collections`` from skmemory.yaml and resolves each name:
      * bare name           -> auto-prefixed ``<operator>.<realm>/<name>``
      * already-qualified   -> left as-is
      * ``peer:<...>`` ref  -> kept only when a valid consent token exists
                               (fail-closed otherwise)

    See module docstring above for the full ruleset + consent schema.
    """
    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}
    raw = skmem.get("recall_collections", []) or []

    operator, realm = _operator_realm()
    own_prefix = f"{operator}.{realm}/" if operator and realm else None
    agent_fqid = _resolve_agent_fqid()
    skcomms_home = _skcomms_home()

    resolved: list[str] = []
    for entry in raw:
        name = str(entry).strip()
        if not name:
            continue

        if name.startswith(_PEER_PREFIX):
            # Foreign reference — consent-gated, fail-closed.
            foreign = name[len(_PEER_PREFIX):].strip()
            if not foreign:
                logger.warning("Dropping empty peer recall_collection reference.")
                continue
            if _consent_grants_read(foreign, agent_fqid, skcomms_home):
                resolved.append(foreign)
            else:
                logger.warning(
                    "Dropping foreign recall_collection %r — no valid consent "
                    "token granting read to %s (fail-closed).",
                    foreign,
                    agent_fqid or "<unknown-fqid>",
                )
            continue

        if "/" in name:
            # Already operator-qualified — trust as-is.
            resolved.append(name)
            continue

        # Bare name — auto-prefix with own operator namespace when possible.
        if own_prefix:
            resolved.append(own_prefix + name)
        else:
            # No realm to namespace with — pass through untouched.
            resolved.append(name)

    return resolved


def _load_recall_graphs(config_dir: Path) -> list[str]:
    """Return recall_graphs list from skmemory.yaml (for cross-graph search)."""
    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}
    return skmem.get("recall_graphs", [])


def _load_recall_source_roots(config_dir: Path) -> dict[str, list[str]]:
    """Return recall graph source roots keyed by graph name."""
    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}
    roots = skmem.get("recall_source_roots", {})
    return roots if isinstance(roots, dict) else {}


def _load_shared_corpora(config_dir: Path) -> list[dict[str, Any]]:
    """Return normalized shared corpus definitions from skmemory.yaml."""
    skmem = _read_yaml_file(config_dir / "skmemory.yaml") or {}
    corpora = skmem.get("shared_corpora", [])
    normalized: list[dict[str, Any]] = []
    if isinstance(corpora, list):
        for entry in corpora:
            if not isinstance(entry, dict):
                continue
            if not entry.get("enabled", True):
                continue
            vector_collection = str(entry.get("vector_collection", "")).strip()
            if not vector_collection:
                continue
            graph_name = str(entry.get("graph_name") or vector_collection).strip()
            normalized.append({
                "name": str(entry.get("name") or vector_collection).strip() or vector_collection,
                "vector_collection": vector_collection,
                "graph_name": graph_name,
                "source_roots": [str(value).strip() for value in entry.get("source_roots", []) if str(value).strip()],
                "projection_profile": str(entry.get("projection_profile") or "").strip() or None,
            })
    if normalized:
        return normalized

    recall_collections = [str(value).strip() for value in skmem.get("recall_collections", []) if str(value).strip()]
    recall_graphs = [str(value).strip() for value in skmem.get("recall_graphs", []) if str(value).strip()]
    recall_source_roots = skmem.get("recall_source_roots", {})
    for index, vector_collection in enumerate(recall_collections):
        graph_name = recall_graphs[index] if index < len(recall_graphs) else vector_collection
        roots = recall_source_roots.get(graph_name) or recall_source_roots.get(vector_collection) or []
        normalized.append({
            "name": vector_collection,
            "vector_collection": vector_collection,
            "graph_name": graph_name,
            "source_roots": [str(value).strip() for value in roots if str(value).strip()],
            "projection_profile": None,
        })
    return normalized


def _make_ollama_embed_fn(model: str, base_url: str):
    """Return an embedding function that calls the Ollama /api/embeddings endpoint."""
    import urllib.request

    def embed(text: str) -> list[float]:
        body = json.dumps({"model": model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return data.get("embedding", [])
        except Exception as e:
            logger.warning("Ollama embedding failed: %s", e)
            return []

    return embed


def _skvector_url(cfg: dict) -> str:
    """Resolve the Qdrant URL from config dict.  Accepts either a top-level
    ``url`` field or the ``host`` / ``port`` / ``https`` combination."""
    if "url" in cfg:
        return cfg["url"]
    host = cfg.get("host", "localhost")
    port = cfg.get("port", 6333)
    scheme = "https" if cfg.get("https", False) else "http"
    return f"{scheme}://{host}:{port}"


def _skgraph_url(cfg: dict) -> str:
    """Resolve the FalkorDB/Redis URL from config dict.

    Accepts:
    - Top-level ``url`` field (used as-is)
    - ``host`` / ``port`` / ``password`` combination (password is
      URL-decoded so YAML authors don't need to double-encode)
    """
    if "url" in cfg:
        return cfg["url"]
    host = cfg.get("host", "localhost")
    port = cfg.get("port", 6379)
    raw_password = cfg.get("password") or cfg.get("passwd")
    if raw_password:
        # Decode URL-encoded chars (e.g. %2B → +, %2F → /, %3D → =)
        password = unquote(str(raw_password))
        # Re-encode only the characters that break URL parsing
        safe_password = password.replace("@", "%40").replace(":", "%3A")
        return f"redis://:{safe_password}@{host}:{port}"
    return f"redis://{host}:{port}"


def _build_skvector_backend(skvector_cfg: dict) -> Any | None:
    """Instantiate SKVectorBackend from config dict.

    Accepts both ``url`` and ``host``/``port``/``https`` styles.
    Embedding provider ``ollama`` injects an Ollama embed_fn so
    sentence-transformers is not required.
    """
    try:
        from .backends.skvector_backend import SKVectorBackend

        url = _skvector_url(skvector_cfg)
        api_key = (
            skvector_cfg.get("api_key")
            or skvector_cfg.get("api-key")
            or skvector_cfg.get("apiKey")
        )
        collection = skvector_cfg.get("collection_name") or skvector_cfg.get("collection", "skmemory")
        embed_cfg = skvector_cfg.get("embedding", {})
        provider = embed_cfg.get("provider", "sentence_transformers")
        model = embed_cfg.get("model", "all-MiniLM-L6-v2")
        embed_fn = None

        if provider == "ollama":
            ollama_url = embed_cfg.get("url", "http://localhost:11434")
            embed_fn = _make_ollama_embed_fn(model, ollama_url)

        return SKVectorBackend(
            url=url,
            api_key=api_key,
            collection=collection,
            embed_fn=embed_fn,
        )
    except Exception as e:
        logger.warning("Could not build SKVectorBackend: %s", e)
        return None


def _build_skgraph_backend(skgraph_cfg: dict) -> Any | None:
    """Instantiate SKGraphBackend from config dict.

    Accepts both ``url`` and ``host``/``port``/``password`` styles.
    URL-encoded passwords (e.g. ``eiCn%2BMz0%3D``) are decoded before
    being embedded in the connection URL.
    """
    try:
        from .backends.skgraph_backend import SKGraphBackend

        url = _skgraph_url(skgraph_cfg)
        graph_name = skgraph_cfg.get("graph_name") or skgraph_cfg.get("graph", "skmemory")
        return SKGraphBackend(url=url, graph_name=graph_name)
    except Exception as e:
        logger.warning("Could not build SKGraphBackend: %s", e)
        return None


class LazyMemoryLoader:
    """Efficiently loads memories based on date tiers."""

    def __init__(self, agent_name: str | None = None):
        self.agent_name = agent_name
        self.paths = get_agent_paths(agent_name)
        self.today = datetime.now().date()
        self.db = SQLiteBackend(str(self.paths["base"] / "memory"))
        self._vector_backend = None
        self._recall_qdrant_backend = None
        self._graph_backend = None
        self._recall_collections: list[str] = []
        self._recall_graphs: list[str] = []
        self._shared_corpora: list[dict[str, Any]] = []
        self._recall_graph_backends: dict[str, Any] = {}
        self._recall_source_roots: dict[str, list[Path]] = {}
        self._backends_loaded = False

    def load_active_context(self) -> MemoryContext:
        """Load token-optimized context for current session.

        Returns:
            MemoryContext with today (full), yesterday (summaries), historical (count)
        """
        today = self._load_today()
        self._ensure_backends()
        today = self._enrich_with_graph_context(today)
        return MemoryContext(
            today_memories=today,
            yesterday_summaries=self._load_yesterday_summaries(),
            historical_count=self._count_historical(),
        )

    def _load_today(self) -> list[dict]:
        """Load today's memories with full content."""
        today_str = self.today.isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content, tags, emotional_signature
                FROM memories
                WHERE DATE(created_at) = ?
                  AND layer = 'short'
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (today_str,),
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "tags": json.loads(row[3]) if row[3] else [],
                    "emotional": json.loads(row[4]) if row[4] else {},
                }
                for row in cursor.fetchall()
            ]

        except Exception as e:
            logger.error(f"Failed to load today's memories: {e}")
            return []

    def _load_yesterday_summaries(self) -> list[dict]:
        """Load yesterday's memories as summaries only."""
        yesterday = (self.today - timedelta(days=1)).isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, summary, tags
                FROM memories
                WHERE DATE(created_at) = ?
                  AND layer IN ('short', 'medium')
                ORDER BY importance DESC
                LIMIT 20
                """,
                (yesterday,),
            )
            memories = []
            for row in cursor.fetchall():
                mem = {
                    "id": row[0],
                    "title": row[1],
                    "summary": row[2] or self._generate_summary(row[1]),
                    "tags": json.loads(row[3]) if row[3] else [],
                }
                memories.append(mem)
            return memories
        except Exception as e:
            logger.error(f"Failed to load yesterday's summaries: {e}")
            return []

    def _count_historical(self) -> int:
        """Count older memories (not loaded into context)."""
        yesterday = (self.today - timedelta(days=1)).isoformat()
        try:
            cursor = self.db._conn.execute(
                """
                SELECT COUNT(*) FROM memories
                WHERE DATE(created_at) < ?
                """,
                (yesterday,),
            )
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to count historical memories: {e}")
            return 0

    def _generate_summary(self, content: str, sentences: int = 2) -> str:
        """Generate a brief summary (fallback if no summary stored)."""
        # Simple truncation-based summary
        words = content.split()[:30]  # First 30 words
        return " ".join(words) + "..." if len(words) >= 30 else content

    def _ensure_backends(self) -> None:
        """Lazy-load vector and graph backends from agent config (once).

        Prefers ChromaDB (local, embedded) over Qdrant. Falls back to
        Qdrant if ChromaDB is unavailable or not installed.
        When ChromaDB is primary, Qdrant is loaded separately as
        _recall_qdrant_backend for shared recall_collections queries.
        """
        if self._backends_loaded:
            return
        self._backends_loaded = True
        config_dir = self.paths["config"]

        # Always load skvector config — needed for recall_collections even when Chroma is primary
        skvector_cfg = _load_skvector_config(config_dir)

        # Try ChromaDB first (local, zero-config)
        chroma_ok = False
        try:
            from .backends.chroma_backend import SKChromaBackend
            persist_dir = str(self.paths["base"] / "memory" / "chroma")
            state_path = self.paths["base"] / "memory" / "chroma-state.json"
            self._vector_backend = SKChromaBackend(
                persist_dir=persist_dir,
                state_path=state_path,
            )
            chroma_ok = True
        except Exception as e:
            logger.warning("context_loader.py: %s", e)
            pass

        if not chroma_ok:
            # Fall back to Qdrant as primary vector backend
            if skvector_cfg:
                self._vector_backend = _build_skvector_backend(skvector_cfg)
        elif skvector_cfg:
            # ChromaDB is primary — but load Qdrant separately for recall_collections
            self._recall_qdrant_backend = _build_skvector_backend(skvector_cfg)

        skgraph_cfg = _load_skgraph_config(config_dir)
        if skgraph_cfg:
            self._graph_backend = _build_skgraph_backend(skgraph_cfg)

        shared_corpora = _load_shared_corpora(config_dir)

        # Resolve shared corpus names through env aliasing
        env = (skvector_cfg or {}).get("env", "prod")
        if env != "prod":
            for corpus in shared_corpora:
                vector_collection = corpus["vector_collection"]
                graph_name = corpus["graph_name"]
                corpus["vector_collection"] = f"{vector_collection}-{env}" if not vector_collection.endswith(f"-{env}") else vector_collection
                corpus["graph_name"] = f"{graph_name}-{env}" if not graph_name.endswith(f"-{env}") else graph_name

        self._shared_corpora = shared_corpora
        self._recall_collections = [corpus["vector_collection"] for corpus in shared_corpora]
        self._recall_graphs = [corpus["graph_name"] for corpus in shared_corpora]

        for corpus in shared_corpora:
            graph_name = corpus["graph_name"]
            raw_roots = corpus.get("source_roots", [])
            roots = [Path(str(value)).expanduser() for value in raw_roots if str(value).strip()]
            self._recall_source_roots[graph_name] = roots or _default_recall_source_roots(graph_name)

        if skgraph_cfg:
            primary_graph_name = skgraph_cfg.get("graph_name") or skgraph_cfg.get("graph", "skmemory")
            for corpus in shared_corpora:
                graph_name = corpus["graph_name"]
                if graph_name == primary_graph_name:
                    continue
                recall_graph_cfg = dict(skgraph_cfg)
                recall_graph_cfg["graph_name"] = graph_name
                backend = _build_skgraph_backend(recall_graph_cfg)
                if backend is not None:
                    self._recall_graph_backends[graph_name] = backend

    def _append_memory_result(self, results: list[dict], seen_ids: set[str], mem: Any, source_backend: str, score: float | None = None) -> None:
        if mem.id in seen_ids:
            return
        seen_ids.add(mem.id)
        metadata = getattr(mem, "metadata", {}) or {}
        results.append({
            "id": mem.id,
            "title": mem.title,
            "content": mem.content,
            "summary": getattr(mem, "summary", None),
            "tags": mem.tags,
            "layer": mem.layer.value if hasattr(mem.layer, "value") else str(mem.layer),
            "created_at": mem.created_at,
            "source_backend": source_backend,
            "source_ref": getattr(mem, "source_ref", ""),
            "metadata": metadata,
            "authority_tier": metadata.get("authority_tier"),
            "vector_score": score,
        })


    def _append_graph_result_set(self, results: list[dict], seen_ids: set[str], hits: list[dict], source_backend: str) -> None:
        from .retrieval import AUTHORITY_WEIGHTS, prepare_metadata

        for hit in hits:
            row = dict(hit)
            matched_values = row.get("matched_values")
            if matched_values is None and row.get("matched_value") is not None:
                matched_values = [row.get("matched_value")]
            matched_values = _unique_preserve([_normalize_legal_match_value(str(value)) for value in (matched_values or []) if str(value).strip()])
            if matched_values:
                row["matched_values"] = matched_values
                row["matched_value"] = matched_values[0]
            metadata = prepare_metadata(
                title=row.get("title", ""),
                source=source_backend,
                source_ref=str(row.get("source_ref", "")),
                tags=row.get("tags") or [],
                metadata=dict(row.get("metadata") or {}),
            )
            row["metadata"] = metadata
            row["authority_tier"] = metadata.get("authority_tier")
            row["graph_match_score"] = min(
                0.25,
                0.04 * float(row.get("match_count", 1) or 1)
                + 0.02 * float(row.get("chunk_match_count", 0) or 0)
                + 0.03 * len(matched_values),
            )
            row["source_backend"] = source_backend

            existing = next((item for item in results if item.get("id") == row.get("id")), None)
            if existing is not None:
                existing_values = list(existing.get("matched_values") or ([] if existing.get("matched_value") is None else [existing.get("matched_value")]))
                merged_values = _unique_preserve(existing_values + matched_values)
                if merged_values:
                    existing["matched_values"] = merged_values
                    existing["matched_value"] = merged_values[0]
                existing["graph_match_score"] = max(float(existing.get("graph_match_score", 0.0) or 0.0), float(row.get("graph_match_score", 0.0) or 0.0))
                existing["match_count"] = max(int(existing.get("match_count", 0) or 0), int(row.get("match_count", 0) or 0))
                existing["chunk_match_count"] = max(int(existing.get("chunk_match_count", 0) or 0), int(row.get("chunk_match_count", 0) or 0))
                existing_backends = list(existing.get("source_backends") or [existing.get("source_backend", "")])
                if source_backend not in existing_backends:
                    existing_backends.append(source_backend)
                existing["source_backends"] = [backend for backend in existing_backends if backend]
                current_priority = _graph_backend_priority(existing.get("source_backend", ""))
                new_priority = _graph_backend_priority(source_backend)
                if new_priority > current_priority:
                    existing["source_backend"] = source_backend
                current_tier = existing.get("authority_tier", "memory")
                new_tier = row.get("authority_tier", "memory")
                if AUTHORITY_WEIGHTS.get(new_tier, 0.0) > AUTHORITY_WEIGHTS.get(current_tier, 0.0):
                    existing["authority_tier"] = new_tier
                    existing["metadata"] = metadata
                continue

            seen_ids.add(row["id"])
            row["source_backends"] = [source_backend]
            results.append(row)

    def _append_graph_hits(self, results: list[dict], seen_ids: set[str], graph_backend: Any, graph_name: str, query: str, max_results: int) -> None:
        probe_map = _structured_graph_probes(query)
        self._append_graph_result_set(results, seen_ids, graph_backend.search(query, limit=max_results), f"skgraph:{graph_name}")
        tags = [w for w in query.split() if len(w) > 2]
        if tags:
            self._append_graph_result_set(results, seen_ids, graph_backend.search_by_tags(tags, limit=max_results), f"skgraph_tags:{graph_name}")
        for label, method_name in (("entity", "search_by_entity"), ("citation", "search_by_citation"), ("claim", "search_by_claim"), ("section", "search_by_section")):
            method = getattr(graph_backend, method_name, None)
            if callable(method):
                for probe in probe_map.get(label, [query]):
                    self._append_graph_result_set(results, seen_ids, method(probe, limit=max_results), f"skgraph_{label}:{graph_name}")

    def _collapse_recall_points(self, scored_points: list[Any]) -> list[Any]:
        collapsed: dict[str, Any] = {}
        for scored_point in scored_points:
            payload = scored_point.payload or {}
            key = str(payload.get("parent_doc") or payload.get("file_path") or payload.get("filename") or payload.get("id") or getattr(scored_point, "id", ""))
            current = collapsed.get(key)
            if current is None or float(getattr(scored_point, "score", 0.0) or 0.0) > float(getattr(current, "score", 0.0) or 0.0):
                collapsed[key] = scored_point
        return sorted(collapsed.values(), key=lambda item: float(getattr(item, "score", 0.0) or 0.0), reverse=True)

    def _resolve_recall_source_path(self, graph_name: str, payload: dict) -> Path | None:
        source_ref = payload.get("parent_doc") or payload.get("file_path")
        if not source_ref:
            return None
        candidate = Path(str(source_ref)).expanduser()
        if candidate.is_absolute() and candidate.exists():
            return candidate
        for root in self._recall_source_roots.get(graph_name, []):
            path = root / str(source_ref)
            if path.exists():
                return path
        return None

    def _build_recall_graph_memory(self, graph_name: str, payload: dict, source_path: Path):
        source_ref = str(payload.get("parent_doc") or payload.get("file_path") or source_path.name)
        memory_dir = self.paths["base"] / "memory"
        fingerprint = compute_source_fingerprint(source_path, payload)
        cache_doc = load_cache_document(memory_dir, graph_name, source_ref)
        if cache_doc and cache_doc.get("fingerprint") == fingerprint:
            return memory_from_cache_document(cache_doc, target_graph_name=graph_name)
        cache_doc = build_cache_document(
            graph_name=graph_name,
            source_ref=source_ref,
            source_path=source_path,
            payload=payload,
        )
        write_cache_document(memory_dir, graph_name, source_ref, cache_doc)
        return memory_from_cache_document(cache_doc, target_graph_name=graph_name)

    def _recall_graph_state_path(self) -> Path:
        return self.paths["base"] / "memory" / "recall-graph-state.json"

    def _load_recall_graph_state(self) -> dict[str, dict[str, str]]:
        path = self._recall_graph_state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_recall_graph_state(self, state: dict[str, dict[str, str]]) -> None:
        path = self._recall_graph_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))

    def _fingerprint_recall_source(self, payload: dict, source_path: Path | None) -> str:
        return compute_source_fingerprint(source_path, payload)

    def sync_recall_graphs(self, batch_size: int = 256) -> dict[str, dict]:
        self._ensure_backends()
        stats: dict[str, dict] = {}
        recall_backend = self._recall_qdrant_backend or (self._vector_backend if self._vector_backend is not None and hasattr(getattr(self._vector_backend, "_client", None), "scroll") else None)
        if recall_backend is None or not self._recall_graph_backends:
            return stats
        if not recall_backend._ensure_initialized():
            return stats
        state = self._load_recall_graph_state()
        dirty = False
        for graph_name, graph_backend in self._recall_graph_backends.items():
            graph_stats = {"source_collection": graph_name, "graph": graph_name, "indexed": 0, "errors": 0, "rehydrated": 0, "collapsed": 0, "skipped": 0}
            stats[graph_name] = graph_stats
            if not graph_backend._ensure_initialized():
                graph_stats["errors"] += 1
                continue
            next_offset = None
            seen_sources: set[str] = set()
            graph_state = state.setdefault(graph_name, {})
            while True:
                try:
                    points, next_offset = recall_backend._client.scroll(collection_name=graph_name, offset=next_offset, limit=batch_size, with_payload=True, with_vectors=False)
                except Exception as e:
                    logger.warning("Recall graph scroll failed for %s: %s", graph_name, e)
                    graph_stats["errors"] += 1
                    break
                if not points:
                    break
                for point in points:
                    payload = point.payload or {}
                    source_key = str(payload.get("parent_doc") or payload.get("file_path") or payload.get("filename") or payload.get("id") or getattr(point, "id", ""))
                    if source_key in seen_sources:
                        graph_stats["collapsed"] += 1
                        continue
                    seen_sources.add(source_key)
                    try:
                        source_path = self._resolve_recall_source_path(graph_name, payload)
                        fingerprint = self._fingerprint_recall_source(payload, source_path)
                        if graph_state.get(source_key) == fingerprint:
                            graph_stats["skipped"] += 1
                            continue
                        if source_path is not None:
                            memory = self._build_recall_graph_memory(graph_name, payload, source_path)
                            graph_stats["rehydrated"] += 1
                        else:
                            memory = recall_backend._memory_from_payload(payload)
                        if graph_backend.index_memory(memory):
                            graph_stats["indexed"] += 1
                            graph_state[source_key] = fingerprint
                            dirty = True
                        else:
                            graph_stats["errors"] += 1
                    except Exception as e:
                        logger.warning("Recall graph index failed for %s: %s", graph_name, e)
                        graph_stats["errors"] += 1
                if next_offset is None:
                    break
        if dirty:
            self._save_recall_graph_state(state)
        return stats

    def deep_search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search ALL memory tiers including vector and graph backends.

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            List of full memory details
        """
        self._ensure_backends()
        seen_ids: set[str] = set()
        results = []

        # 1. SQLite full-text search (always available)
        for r in self._search_sqlite(query):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                r.setdefault("source_backend", "sqlite")
                results.append(r)

        # 2. SKVector semantic search (primary collection + recall_collections)
        if self._vector_backend is not None:
            try:
                vector_hits = self._vector_backend.search_text(query, limit=max_results)
                for mem in vector_hits:
                    self._append_memory_result(results, seen_ids, mem, "skvector")
            except Exception as e:
                logger.warning("SKVector deep_search failed: %s", e)

            # Also search recall_collections (cross-agent/cross-project indexes)
            # Uses the Qdrant backend (shared collections like hammertime-v3, opus-memory).
            # When ChromaDB is primary, _recall_qdrant_backend holds the Qdrant client.
            _recall_backend = self._recall_qdrant_backend or (
                self._vector_backend
                if self._vector_backend is not None and hasattr(self._vector_backend, "_client")
                and hasattr(getattr(self._vector_backend, "_client", None), "query_points")
                else None
            )
            if self._recall_collections and _recall_backend is not None and _recall_backend._ensure_initialized():
                embedding = _recall_backend._embed(query)
                if embedding:
                    for recall_col in self._recall_collections:
                        try:
                            scored_points = _recall_backend._client.query_points(
                                collection_name=recall_col,
                                query=embedding,
                                limit=max_results * 4,
                            ).points
                            for sp in self._collapse_recall_points(scored_points)[:max_results]:
                                mem = _recall_backend._memory_from_payload(sp.payload or {})
                                self._append_memory_result(results, seen_ids, mem, f"skvector:{recall_col}", score=float(getattr(sp, "score", 0.0) or 0.0))
                        except Exception as e:
                            logger.warning("SKVector recall_collection '%s' failed: %s", recall_col, e)

        # 3. SKGraph retrieval (primary graph + recall graphs)
        if self._graph_backend is not None:
            try:
                graph_name = getattr(self._graph_backend, "graph_name", "skmemory")
                self._append_graph_hits(results, seen_ids, self._graph_backend, graph_name, query, max_results)
            except Exception as e:
                logger.warning("SKGraph deep_search failed: %s", e)

        for graph_name, graph_backend in self._recall_graph_backends.items():
            try:
                self._append_graph_hits(results, seen_ids, graph_backend, graph_name, query, max_results)
            except Exception as e:
                logger.warning("Recall SKGraph deep_search failed for %s: %s", graph_name, e)

        # Compute fusion scores and sort
        query_terms = [w.lower() for w in query.split() if len(w) > 2]
        for r in results:
            r["_fusion_score"] = self._compute_fusion_score(r, query, query_terms)

        # Sort by fusion score descending
        results = sorted(results, key=lambda x: x.get("_fusion_score", 0), reverse=True)
        return results[:max_results]

    def _compute_fusion_score(self, result: dict, query: str, query_terms: list[str]) -> float:
        """Compute hybrid fusion score combining text match, authority, and recency."""
        import math
        from datetime import datetime, timezone

        # 1. Text overlap score (BM25-ish: title > content)
        title = result.get("title", "").lower()
        content = (result.get("content", "") or result.get("content_preview", "")).lower()
        matched_values = [str(value).lower() for value in (result.get("matched_values") or ([] if result.get("matched_value") is None else [result.get("matched_value")]))]
        title_hits = sum(1 for t in query_terms if t in title)
        content_hits = sum(1 for t in query_terms if t in content)
        matched_hits = sum(1 for t in query_terms if any(t in value for value in matched_values))
        text_score = min(1.0, (title_hits * 0.4 + content_hits * 0.1 + matched_hits * 0.25) / max(1, len(query_terms)))

        # 2. Authority weight
        from .retrieval import AUTHORITY_WEIGHTS
        tier = result.get("authority_tier", "memory")
        authority_score = AUTHORITY_WEIGHTS.get(tier, 0.35)

        # 3. Time decay (half-life: 7d short, 30d mid, 365d long)
        half_life = {
            "short-term": 7, "short": 7,
            "mid-term": 30, "mid": 30,
            "long-term": 365, "long": 365,
        }.get(result.get("layer", "short-term"), 30)
        try:
            created = result.get("created_at", "")
            if created:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - dt).days
                decay = math.pow(0.5, age_days / half_life)
            else:
                decay = 1.0
        except Exception as e:
            logger.warning("context_loader.py: %s", e)
            decay = 1.0

        # 4. Backend bonus (vector results carry semantic signal)
        backend = result.get("source_backend", "sqlite").split(":")[0]
        if backend.startswith("skvector"):
            backend_bonus = 0.15
        elif backend == "skgraph_citation":
            backend_bonus = 0.14
        elif backend == "skgraph_claim":
            backend_bonus = 0.12
        elif backend == "skgraph_entity":
            backend_bonus = 0.10
        elif backend == "skgraph_section":
            backend_bonus = 0.09
        elif backend.startswith("skgraph"):
            backend_bonus = 0.06
        else:
            backend_bonus = 0.0

        vector_signal = min(0.2, float(result.get("vector_score", 0.0) or 0.0) * 0.2)
        graph_signal = min(0.25, float(result.get("graph_match_score", 0.0) or 0.0))

        # Weighted fusion
        return (0.35 * text_score + 0.30 * authority_score + 0.20 * decay + 0.15) + backend_bonus + vector_signal + graph_signal

    def _enrich_with_graph_context(self, memories: list[dict]) -> list[dict]:
        """Add graph neighbourhood to top memories for richer context."""
        if self._graph_backend is None:
            return memories
        for mem in memories[:5]:  # only top 5 to avoid token bloat
            try:
                graph_ctx = self._graph_backend.get_context_graph(mem["id"], depth=1)
                if graph_ctx.get("related"):
                    mem["related_context"] = [
                        {"title": r["title"], "layer": r["layer"], "edge": r["edge_type"]}
                        for r in graph_ctx["related"][:3]
                    ]
                if graph_ctx.get("entities"):
                    mem["entities"] = graph_ctx["entities"][:5]
            except Exception as e:
                logger.warning("context_loader.py: %s", e)
                pass
        return memories

    def sync_backends(self) -> dict:
        """Sync all flat-file memories to vector and graph backends.

        Returns dict with stats: indexed, skipped, removed, errors per backend.
        """
        self._ensure_backends()
        stats = {}

        mem_dir = self.paths["base"] / "memory"
        if self._vector_backend is not None:
            result = self._vector_backend.sync_all(mem_dir, self.agent_name or "default")
            stats["skvector"] = result

        if self._graph_backend is not None:
            stats["skgraph"] = self._graph_backend.sync_all(mem_dir, self.agent_name or "default")

        recall_graph_stats = self.sync_recall_graphs()
        if recall_graph_stats:
            stats["recall_graphs"] = recall_graph_stats

        return stats

    def _search_sqlite(self, query: str) -> list[dict]:
        """Search SQLite for memories matching query."""
        try:
            pattern = f"%{query}%"
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content_preview, summary, tags, layer, created_at
                FROM memories
                WHERE title LIKE ? OR content_preview LIKE ? OR tags LIKE ?
                ORDER BY
                    CASE
                        WHEN title LIKE ? THEN 3
                        WHEN content_preview LIKE ? THEN 2
                        ELSE 1
                    END DESC,
                    created_at DESC
                LIMIT 50
                """,
                (pattern, pattern, pattern, pattern, pattern),
            )
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "summary": row[3],
                    "tags": (json.loads(row[4]) if row[4] and row[4].startswith("[") else []),
                    "layer": row[5],
                    "created_at": row[6],
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logger.error(f"Failed to search SQLite: {e}")
            return []

    def get_memory_by_id(self, memory_id: str) -> dict | None:
        """Load full memory details by ID (for deep recall).

        Args:
            memory_id: UUID of the memory

        Returns:
            Full memory dict or None
        """
        try:
            cursor = self.db._conn.execute(
                """
                SELECT id, title, content, summary, tags,
                       emotional_signature, layer, created_at
                FROM memories
                WHERE id = ?
                """,
                (memory_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "title": row[1],
                    "content": row[2],
                    "summary": row[3],
                    "tags": json.loads(row[4]) if row[4] else [],
                    "emotional": json.loads(row[5]) if row[5] else {},
                    "layer": row[6],
                    "created_at": row[7],
                }
        except Exception as e:
            logger.error(f"Failed to get memory {memory_id}: {e}")
        return None

    def promote_memory(self, memory_id: str, to_layer: str) -> bool:
        """Promote memory to different tier and generate summary.

        Args:
            memory_id: Memory to promote
            to_layer: Target layer ('short', 'medium', 'long')

        Returns:
            True if successful
        """
        try:
            # Get memory content
            memory = self.get_memory_by_id(memory_id)
            if not memory:
                return False

            # Generate summary if promoting to medium/long
            if to_layer in ("medium", "long") and not memory.get("summary"):
                summary = self._generate_summary(memory["content"], 2)

                # Update in database
                self.db._conn.execute(
                    """
                    UPDATE memories
                    SET layer = ?, summary = ?
                    WHERE id = ?
                    """,
                    (to_layer, summary, memory_id),
                )
                self.db._conn.commit()

                # Also move flat file
                self._move_flat_file(memory_id, to_layer)

                logger.info(f"Promoted memory {memory_id} to {to_layer}")
                return True

        except Exception as e:
            logger.error(f"Failed to promote memory {memory_id}: {e}")

        return False

    def _move_flat_file(self, memory_id: str, to_layer: str):
        """Move memory flat file to appropriate tier directory."""
        # Find current location
        for layer in ["short", "medium", "long"]:
            src = self.paths["memory_" + layer] / f"{memory_id}.json"
            if src.exists():
                dst = self.paths["memory_" + to_layer] / f"{memory_id}.json"
                src.rename(dst)
                logger.debug(f"Moved {src} -> {dst}")
                break


def get_context_for_session(agent_name: str | None = None) -> str:
    """Convenience function: get token-optimized context.

    Usage:
        context = get_context_for_session("lumina")
        # Returns formatted string with today's + yesterday's summaries
    """
    loader = LazyMemoryLoader(agent_name)
    context = loader.load_active_context()
    return context.to_context_string()
