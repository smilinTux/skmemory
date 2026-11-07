"""The one formula for the text a memory is embedded from.

Two code paths used to build embedding input from the same memory with two
different formulas, so the rows in skmem-pg were a mix of both:

* ``PGVectorBackend.save`` embedded ``title + content + summary`` cut at 1400
  characters.
* ``reconcile`` embedded ``(content or title)`` cut at 1100 characters, which
  drops the title (the field a self-retrieval or title-shaped query hits) and
  uses a shorter cut.

Every embed of a stored memory goes through :func:`embed_text` so a vector
means the same thing no matter which path wrote it. This module has no
imports so both callers can use it without a cycle.
"""

from __future__ import annotations

#: Characters of embed text sent to the model. mxbai-embed-large caps at 512
#: tokens and the Ollama build 400s on overflow; ~1400 chars clears that for
#: typical text and the backend halves-and-retries for denser text.
EMBED_TEXT_MAX_CHARS = 1400


def embed_text(title: str | None, content: str | None, summary: str | None = None) -> str:
    """``title\\ncontent\\nsummary``, stripped, cut at :data:`EMBED_TEXT_MAX_CHARS`."""
    joined = f"{title or ''}\n{content or ''}\n{summary or ''}".strip()
    return joined[:EMBED_TEXT_MAX_CHARS]


def legacy_reconcile_text(content: str | None, title: str | None) -> str:
    """What ``reconcile`` embedded before the shared formula (for reembed only)."""
    return (content or title or " ")[:1100]
