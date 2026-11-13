"""Re-seal memories that are PROVEN victims of a fixed sealing bug.

``fortress verify`` reports a memory as tampered when its stored integrity hash does
not match id, title, content and emotional signature. Two bugs produced that without
any tampering, and each leaves a proof in the file itself:

``unstripped-title``
    The memory was sealed over a title with leading or trailing whitespace, which
    ``Memory`` strips on every load (``refresh_observation``, 2026-11-13). Proof: the
    stored hash equals the hash over the loaded id, content and emotional signature
    with the title wrapped in that whitespace. Nothing but whitespace the model
    discards differs.

``promoted-copy``
    ``Memory.promote`` minted a new id but kept the source's hash (fixed in #173).
    Proof: the copy's stored hash is its parent's hash, the parent verifies today,
    and the copy's title, content and emotional signature equal the parent's.

Anything else (content edited, title changed, a different hash, a failing parent,
a signature sidecar next to the file) is NOT a victim and is never touched. This
module reads flat files only; writing goes through the store and is re-proved per
memory at write time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    PARENT_RELATION_KEY,
    RELATION_PROMOTED_FROM,
    Memory,
    MemoryLayer,
)

logger = logging.getLogger("skmemory.reseal")

REASON_UNSTRIPPED_TITLE = "unstripped-title"
REASON_PROMOTED_COPY = "promoted-copy"

# Whitespace the title validator strips, in the shapes writers produced or could produce.
_PADDINGS = (" ", "  ", "\n", "\t", "\r\n", "\n\n")


@dataclass(frozen=True)
class Victim:
    """One memory proven to carry a stale seal, and why re-sealing is safe."""

    memory_id: str
    path: str
    layer: str
    reason: str
    proof: str
    old_hash: str
    new_hash: str
    parent_id: str = ""


@dataclass
class ResealResult:
    """Outcome of :func:`reseal`."""

    resealed: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _sha(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _flat_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for layer in MemoryLayer:
        d = base / layer.value
        if d.is_dir():
            out.extend(sorted(d.glob("*.json")))
    return out


def _read(path: Path) -> tuple[dict[str, Any], Memory] | None:
    """The raw dict and the loaded Memory of a plain flat file, else None."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        return raw, Memory.from_stored(raw)
    except Exception:  # noqa: BLE001 - encrypted, partial or non-memory files are not ours
        return None


def _find_flat(base: Path, memory_id: str) -> Path | None:
    for layer in MemoryLayer:
        p = base / layer.value / f"{memory_id}.json"
        if p.is_file():
            return p
    return None


def _has_sidecar(path: Path) -> bool:
    return Path(str(path) + ".sig").exists() or Path(str(path) + ".seal").exists()


def _promoted_parent_with_same_seal(
    base: Path, m: Memory, stored: str, _depth: int = 0, _seen: frozenset[str] = frozenset()
) -> str:
    """Parent id when *m* is a promoted copy whose *stored* hash is its source's seal.

    The parent must hold that exact hash and the same title, content and emotional
    signature. It must either verify today, or be a promoted copy of the same kind
    itself (a short to mid to long chain), ending at a source that verifies.
    """
    if _depth > 12 or m.id in _seen or not m.parent_id:
        return ""
    if (m.metadata or {}).get(PARENT_RELATION_KEY) != RELATION_PROMOTED_FROM:
        return ""
    parent_path = _find_flat(base, m.parent_id)
    read = _read(parent_path) if parent_path else None
    if read is None:
        return ""
    parent = read[1]
    if (
        parent.integrity_hash != stored
        or parent.title.strip() != m.title.strip()
        or parent.content != m.content
        or parent.emotional.signature() != m.emotional.signature()
    ):
        return ""
    if parent.verify_integrity():
        return parent.id
    if _has_sidecar(parent_path):
        return ""
    up = _promoted_parent_with_same_seal(base, parent, stored, _depth + 1, _seen | {m.id})
    return parent.id if up else ""


def classify(base: Path, path: Path) -> Victim | None:
    """The :class:`Victim` record for *path*, or None when it is not provably one."""
    read = _read(path)
    if read is None:
        return None
    raw, m = read
    stored = m.integrity_hash
    if not stored or m.verify_integrity() or _has_sidecar(path):
        return None
    new_hash = m.compute_integrity_hash()
    sig = m.emotional.signature()
    title = m.title.strip()

    for pad in _PADDINGS:
        for label, padded in (("trailing", title + pad), ("leading", pad + title)):
            if _sha(f"{m.id}:{padded}:{m.content}:{sig}") == stored:
                return Victim(
                    memory_id=m.id,
                    path=str(path),
                    layer=m.layer.value,
                    reason=REASON_UNSTRIPPED_TITLE,
                    proof=(
                        f"stored hash equals the hash over this memory's own id, content and "
                        f"emotional signature with title {label} whitespace {pad!r}, which "
                        f"Memory strips on load"
                    ),
                    old_hash=stored,
                    new_hash=new_hash,
                )

    parent_id = _promoted_parent_with_same_seal(base, m, stored)
    if parent_id:
        return Victim(
            memory_id=m.id,
            path=str(path),
            layer=m.layer.value,
            reason=REASON_PROMOTED_COPY,
            proof=(
                f"stored hash is the hash of its promotion source {parent_id} (a copy of the "
                f"same sealed fields, up a chain of promoted_from links to a source that "
                f"verifies today); title, content and emotional signature equal the source's "
                f"(Memory.promote kept the source hash under a new id)"
            ),
            old_hash=stored,
            new_hash=new_hash,
            parent_id=parent_id,
        )
    return None


def find_victims(base_path: Path | str, *, only: set[str] | None = None) -> list[Victim]:
    """Every provable victim under *base_path*. Read-only."""
    base = Path(base_path)
    out: list[Victim] = []
    for path in _flat_files(base):
        if only is not None and path.stem not in only:
            continue
        v = classify(base, path)
        if v is not None:
            out.append(v)
    return out


def default_log_path() -> Path:
    """Where re-seals are logged: ``SKMEMORY_RESEAL_LOG`` or ``<home>/logs/fortress-reseal.jsonl``."""
    env = os.environ.get("SKMEMORY_RESEAL_LOG")
    if env:
        return Path(env)
    from .config import SKMEMORY_HOME

    return Path(SKMEMORY_HOME) / "logs" / "fortress-reseal.jsonl"


def reseal(
    store: Any,
    victims: list[Victim],
    *,
    log_path: Path | None = None,
    audit: Any | None = None,
) -> ResealResult:
    """Re-seal *victims* through the store, re-proving each one first.

    A memory whose proof no longer holds (edited since the scan, say) is skipped,
    never re-sealed. Every re-seal is appended to *log_path* and, when given, to the
    fortress *audit* chain.
    """
    base = Path(store.primary.base_path)
    log_path = log_path or default_log_path()
    result = ResealResult()
    # Deepest copy first: a child's proof is "my hash is my source's hash", which stops
    # holding the moment the source is re-sealed. Children go before their sources.
    by_id = {v.memory_id: v for v in victims}

    def depth(v: Victim) -> int:
        n, cur = 0, v
        while cur.parent_id in by_id and n < 64:
            n, cur = n + 1, by_id[cur.parent_id]
        return n

    for v in sorted(victims, key=depth, reverse=True):
        again = classify(base, Path(v.path))
        if again is None or again.reason != v.reason or again.old_hash != v.old_hash:
            result.skipped += 1
            result.details.append({"memory_id": v.memory_id, "status": "skipped-proof-changed"})
            continue
        try:
            memory = store.primary.load(v.memory_id)
            if memory is None or memory.integrity_hash != v.old_hash:
                raise RuntimeError("stored copy changed since the proof")
            memory.seal()
            if memory.integrity_hash != v.new_hash:
                raise RuntimeError("sealed hash differs from the proven hash")
            store.primary.save(memory)
            if not store.primary.load(v.memory_id).verify_integrity():
                raise RuntimeError("does not verify after the write")
        except Exception as exc:  # noqa: BLE001 - one bad memory must not stop the pass
            result.failed += 1
            result.details.append({"memory_id": v.memory_id, "status": f"failed: {exc}"})
            logger.error("reseal failed for %s: %s", v.memory_id, exc)
            continue
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "memory_id": v.memory_id,
            "reason": v.reason,
            "proof": v.proof,
            "old_hash": v.old_hash,
            "new_hash": v.new_hash,
            "path": v.path,
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if audit is not None:
            audit.append(
                "reseal",
                v.memory_id,
                ok=True,
                reason=v.reason,
                old_hash=v.old_hash[:16],
                new_hash=v.new_hash[:16],
            )
        logger.info("re-sealed %s (%s)", v.memory_id, v.reason)
        result.resealed += 1
        result.details.append({"memory_id": v.memory_id, "status": "resealed"})
    return result


def victim_dicts(victims: list[Victim]) -> list[dict[str, Any]]:
    """JSON-ready form of *victims*."""
    return [asdict(v) for v in victims]
