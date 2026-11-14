"""Tamper with a stored memory the way a real tamperer does: behind the store's back.

``backend.save`` is the API, and it keeps seals true for in-process edits, so a test
that wants a "tampered" memory has to change the file itself (Syncthing, an editor,
a script), never call ``save``.
"""

from __future__ import annotations

import json

from skmemory.atomic_io import atomic_write_bytes, atomic_write_text


def tamper_on_disk(backend, memory_id: str, **changes) -> None:
    """Rewrite the stored file with *changes*, leaving its integrity hash as it was."""
    path = backend._find_file(memory_id)
    memory = backend.load(memory_id)
    assert path is not None and memory is not None
    data = memory.model_dump()
    data.update(changes)
    text = json.dumps(data, indent=2, default=str)
    vault = getattr(backend, "_vault", None)
    if vault is not None:
        atomic_write_bytes(path, vault.encrypt(text.encode("utf-8")))
    else:
        atomic_write_text(path, text)
