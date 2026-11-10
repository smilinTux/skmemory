"""Tests for the per-chunk duplicate guard in MemoryStore.ingest_document."""

from skmemory.backends.sqlite_backend import SQLiteBackend
from skmemory.store import MemoryStore


def _long_doc(word: str) -> str:
    """Build an invented multi-paragraph document long enough to decompose."""
    paras = [
        f"Paragraph {i} about {word}. " + " ".join(f"{word}{i}x{j}" for j in range(60))
        for i in range(12)
    ]
    return "\n\n".join(paras)


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(primary=SQLiteBackend(str(tmp_path)), use_sqlite=True)


def _count(store: MemoryStore) -> int:
    return len(store.primary.list_memories(limit=100000))


def test_reimport_of_same_document_returns_existing_parent(tmp_path) -> None:
    store = _store(tmp_path)
    content = _long_doc("alpha")
    first = store.ingest_document(title="Day log", content=content, source="test")
    after_first = _count(store)
    second = store.ingest_document(title="Day log", content=content, source="test")

    assert second.id == first.id
    assert _count(store) == after_first


def test_different_document_is_still_written(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.ingest_document(title="A", content=_long_doc("alpha"), source="test")
    after_first = _count(store)
    second = store.ingest_document(title="B", content=_long_doc("beta"), source="test")

    assert second.id != first.id
    assert _count(store) > after_first


def test_partly_changed_document_is_written_again(tmp_path) -> None:
    store = _store(tmp_path)
    content = _long_doc("alpha")
    first = store.ingest_document(title="Day log", content=content, source="test")
    after_first = _count(store)
    changed = content + "\n\nA brand new closing paragraph with gamma delta epsilon words."
    second = store.ingest_document(title="Day log", content=changed, source="test")

    assert second.id != first.id
    assert _count(store) > after_first
