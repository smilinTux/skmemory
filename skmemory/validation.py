"""Schema-validated writes for SKMemory.

Provides a *pluggable* pre-write hook mechanism so that malformed memories
are rejected with a clear error *before* they hit the store.

The flat JSON files are the source of truth, so validation is deliberately
robust-but-lenient: it re-validates a Memory against the canonical pydantic
schema (:class:`skmemory.models.Memory`) rather than inventing a competing
schema. Optional fields keep their declared defaults and are not tightened.

A pre-write hook is any callable ``(Memory) -> None`` that raises to reject
the write. Register your own via
:meth:`skmemory.store.MemoryStore.register_pre_write_hook`.
"""

from __future__ import annotations

from typing import Callable

from pydantic import ValidationError

from .models import Memory

# A pre-write hook inspects a Memory and raises to reject the write.
PreWriteHook = Callable[[Memory], None]


class SchemaValidationError(ValueError):
    """Raised when a memory fails schema validation at the write boundary.

    Subclasses :class:`ValueError` so existing ``except ValueError`` callers
    (and the store's WAL failure path) keep working.
    """


def schema_validator(memory: Memory) -> None:
    """Validate a memory against the canonical :class:`Memory` schema.

    This is the default pre-write hook. It round-trips the memory through
    ``model_dump`` → ``model_validate`` so that any field mutated *after*
    construction (e.g. via ``model_construct`` or direct attribute
    assignment that bypassed validation) is caught before persistence.

    Args:
        memory: The memory about to be written.

    Raises:
        SchemaValidationError: If the memory is not a ``Memory`` instance or
            fails re-validation against the schema. The message names the
            offending fields so the caller knows exactly what to fix.
    """
    if not isinstance(memory, Memory):
        raise SchemaValidationError(
            f"pre-write rejected: expected a Memory instance, got "
            f"{type(memory).__name__}"
        )

    try:
        # Round-trip through the JSON representation the flat files store,
        # re-running every field/enum validator on the current values.
        Memory.model_validate(memory.model_dump(mode="json"))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        ident = getattr(memory, "id", "<unknown>")
        raise SchemaValidationError(
            f"pre-write rejected memory {ident!s}: malformed against Memory "
            f"schema — {problems}"
        ) from exc


#: The hooks a fresh :class:`MemoryStore` installs by default.
def default_pre_write_hooks() -> list[PreWriteHook]:
    """Return a fresh list of the default pre-write hooks.

    A new list per call so each store owns its own mutable hook chain.
    """
    return [schema_validator]


def run_pre_write_hooks(memory: Memory, hooks: list[PreWriteHook]) -> None:
    """Run every pre-write hook against *memory* in registration order.

    The first hook to raise aborts the write; its exception propagates
    unchanged so the caller sees the specific rejection reason.

    Args:
        memory: The memory about to be written.
        hooks: Ordered list of pre-write hooks to run.
    """
    for hook in hooks:
        hook(memory)
