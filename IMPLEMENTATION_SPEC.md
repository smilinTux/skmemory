# skmemory MemPalace Integration — Implementation Spec

## Overview
5 features to implement, inspired by MemPalace (https://github.com/milla-jovovich/mempalace).
All changes are in the skmemory repo at `~/clawd/skcapstone-repos/skmemory/`.

## Feature 1: Query Sanitizer
**File:** `skmemory/query_sanitizer.py` (NEW)
**Integration points:** `skmemory/store.py:search()` and `skmemory/backends/chroma_backend.py:search_text()`

### Problem
AI agents often prepend their entire system prompt to search queries, causing ChromaDB embeddings to represent the system prompt instead of the actual question. MemPalace documented a drop from 89.8% to 1.0% recall.

### Implementation
Create `skmemory/query_sanitizer.py`:

```python
def sanitize_query(raw_query: str) -> str:
    """4-step cascade to extract the actual search intent from bloated queries."""
    # Step 1: Passthrough if already short (<=200 chars)
    if len(raw_query) <= 200:
        return raw_query.strip()
    
    # Step 2: Extract last question (scan backwards for ?)
    # Find the last sentence ending with ?
    # Return everything from the start of that sentence
    
    # Step 3: Extract last meaningful sentence
    # Split on sentence boundaries, take the last non-empty sentence
    
    # Step 4: Tail truncation to 500 chars max
    # Take the last 500 chars from the cleaned result
    
    return cleaned.strip()
```

### Integration
In `store.py:search()` (line 583):
```python
from .query_sanitizer import sanitize_query

def search(self, query: str, limit: int = 10) -> list[Memory]:
    query = sanitize_query(query)  # ADD THIS LINE
    if self.vector:
        ...
```

In `chroma_backend.py:search_text()` (line 351):
```python
from ..query_sanitizer import sanitize_query

def search_text(self, query: str, limit: int = 10, ...) -> list[Memory]:
    query = sanitize_query(query)  # ADD THIS LINE
    ...
```

### Tests
Add `tests/test_query_sanitizer.py`:
- Test short queries pass through unchanged
- Test system prompt + question extracts just the question
- Test multi-sentence queries extract the last meaningful sentence
- Test very long queries get truncated to 500 chars

---

## Feature 2: Write-Ahead Log (WAL)
**File:** `skmemory/wal.py` (NEW)
**Integration point:** `skmemory/store.py:snapshot()` (line 136)

### Problem
No audit trail for memory writes. If a memory gets corrupted or poisoned, we can't trace what happened. The existing `fortress.py` has an AuditLog but it's in the FortifiedMemoryStore wrapper, not the base MemoryStore.

### Implementation
Create `skmemory/wal.py`:

```python
import json
from pathlib import Path
from datetime import datetime, timezone

class WriteAheadLog:
    """Append-only JSONL log for all memory write operations.
    
    Every write is logged BEFORE execution for crash recovery.
    Each line: {"ts": "...", "op": "snapshot|delete|promote|consolidate", 
                "memory_id": "...", "title": "...", "layer": "...", "status": "pending|done|failed"}
    
    Default path: ~/.skcapstone/agents/{agent}/memory/wal/write_log.jsonl
    """
    
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
    
    def log_pending(self, op: str, memory_id: str, title: str, layer: str, metadata: dict | None = None) -> None:
        """Log a pending write operation BEFORE it executes."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "title": title,
            "layer": layer,
            "status": "pending",
        }
        if metadata:
            entry["meta"] = metadata
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_done(self, op: str, memory_id: str) -> None:
        """Log that a pending write completed successfully."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "status": "done",
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_failed(self, op: str, memory_id: str, error: str) -> None:
        """Log that a pending write failed."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "memory_id": memory_id,
            "status": "failed",
            "error": error,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def tail(self, n: int = 50) -> list[dict]:
        """Read the last n entries."""
        if not self.path.exists():
            return []
        lines = self.path.read_text().strip().split("\n")
        return [json.loads(line) for line in lines[-n:]]
```

### Integration
In `store.py.__init__()`, add WAL initialization:
```python
from .wal import WriteAheadLog
from .agents import get_agent_paths

# In __init__:
agent_paths = get_agent_paths()
wal_path = agent_paths["base"] / "memory" / "wal" / "write_log.jsonl"
self._wal = WriteAheadLog(wal_path)
```

In `store.py.snapshot()`, wrap the write:
```python
# Before primary.save():
self._wal.log_pending("snapshot", memory.id, title, layer.value)
try:
    self.primary.save(memory)
    self._wal.log_done("snapshot", memory.id)
except Exception as exc:
    self._wal.log_failed("snapshot", memory.id, str(exc))
    raise
```

Same pattern for `forget()`, `promote()`, `consolidate_session()`.

### Tests
- Test WAL file creation
- Test pending/done/failed logging
- Test tail() returns correct entries
- Test crash recovery scenario (pending without done = incomplete write)

---

## Feature 3: General Extractor (Auto-Capture)
**File:** `skmemory/extractor.py` (NEW)
**Integration:** Called from auto-save hooks and session consolidation

### Problem
Memories only get saved when explicitly called via `skmemory_snapshot`. Conversations contain decisions, preferences, milestones, problems, and emotional moments that should be auto-captured.

### Implementation
Create `skmemory/extractor.py`:

```python
import re
from dataclasses import dataclass

@dataclass
class ExtractedMemory:
    type: str      # "decision" | "preference" | "milestone" | "problem" | "emotional"
    content: str   # The extracted text
    confidence: float  # 0-1
    source_line: int   # Line number in original text

# Regex patterns per type
DECISION_PATTERNS = [
    r"(?:we|i)\s+(?:decided|agreed|chose|settled|went with)\b",
    r"(?:the decision|the call|the verdict)\s+(?:is|was)\b",
    r"(?:going with|sticking with|committing to)\b",
    r"(?:approved|rejected|vetoed)\b",
]

PREFERENCE_PATTERNS = [
    r"(?:i|we)\s+(?:prefer|like|want|need|always|never)\b",
    r"(?:don't|do not|stop|avoid|skip)\b.*\b(?:that|this|doing)\b",
    r"(?:the right way|the best way|always use|never use)\b",
]

MILESTONE_PATTERNS = [
    r"(?:shipped|deployed|launched|released|merged|completed|finished|done)\b",
    r"(?:v\d+\.\d+|version \d+)\b",
    r"(?:first time|finally|at last|breakthrough)\b",
]

PROBLEM_PATTERNS = [
    r"(?:bug|broken|failed|error|crash|issue|problem)\b",
    r"(?:doesn't work|won't|can't|unable to)\b",
    r"(?:root cause|turns out|the problem was|figured out)\b",
]

EMOTIONAL_PATTERNS = [
    r"(?:love|proud|excited|grateful|happy|sad|frustrated|angry|scared)\b",
    r"(?:breakthrough|cloud 9|oof|feeling)\b",
    r"(?:this means|this matters|sacred|important to me)\b",
]

def extract_memories(text: str) -> list[ExtractedMemory]:
    """Extract potential memory-worthy moments from conversation text.
    
    Pure regex/keyword extraction — no LLM needed.
    Filters out code lines (lines starting with spaces/tabs, or containing common code patterns).
    """
    results = []
    lines = text.split("\n")
    
    for i, line in enumerate(lines):
        # Skip code lines
        if _is_code_line(line):
            continue
        
        for pattern_list, mem_type in [
            (DECISION_PATTERNS, "decision"),
            (PREFERENCE_PATTERNS, "preference"),
            (MILESTONE_PATTERNS, "milestone"),
            (PROBLEM_PATTERNS, "problem"),
            (EMOTIONAL_PATTERNS, "emotional"),
        ]:
            for pattern in pattern_list:
                if re.search(pattern, line, re.IGNORECASE):
                    # Extract the full sentence containing the match
                    sentence = _extract_sentence(line)
                    if len(sentence) > 20:  # Skip very short matches
                        results.append(ExtractedMemory(
                            type=mem_type,
                            content=sentence,
                            confidence=0.6,  # Base confidence
                            source_line=i,
                        ))
                    break  # One match per line per type
    
    return _deduplicate(results)

def _is_code_line(line: str) -> bool:
    """Filter out lines that look like code."""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(("#!", "import ", "from ", "def ", "class ", "    ", "\t")):
        return True
    if any(c in stripped for c in ["()", "{}", "=>", "->", "==", "!="]):
        return True
    return False

def _extract_sentence(line: str) -> str:
    """Extract a clean sentence from a line."""
    # Remove markdown formatting
    cleaned = re.sub(r"[*_`#>]", "", line).strip()
    # Remove leading bullets/numbers
    cleaned = re.sub(r"^[-•]\s*", "", cleaned)
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
    return cleaned

def _deduplicate(results: list[ExtractedMemory]) -> list[ExtractedMemory]:
    """Remove near-duplicate extractions."""
    seen = set()
    unique = []
    for r in results:
        key = r.content[:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
```

### Tests
- Test extraction from sample conversation text
- Test code line filtering
- Test deduplication
- Test each pattern type produces correct results

---

## Feature 4: Metadata-Scoped Search
**Integration point:** `skmemory/backends/chroma_backend.py:search_text()` and `skmemory/store.py:search()`

### Problem
Search queries everything at once. We already store `tags`, `source`, `layer` as ChromaDB metadata but don't use them to scope searches.

### Implementation
Add optional filtering parameters to `search()`:

In `store.py`:
```python
def search(
    self,
    query: str,
    limit: int = 10,
    *,
    tags: list[str] | None = None,      # NEW: filter by tags
    layer: str | None = None,            # NEW: filter by layer
    source: str | None = None,           # NEW: filter by source
) -> list[Memory]:
    query = sanitize_query(query)
    if self.vector:
        try:
            results = self.vector.search_text(
                query, limit=limit,
                layer=layer,
                tags=tags,       # Pass through
                source=source,   # Pass through
            )
            ...
```

In `chroma_backend.py:search_text()`:
```python
def search_text(
    self,
    query: str,
    limit: int = 10,
    layer: str | None = None,
    is_chunk: bool | None = None,
    authority_tier: str | None = None,
    tags: list[str] | None = None,    # NEW
    source: str | None = None,        # NEW
) -> list[Memory]:
    ...
    # Add tag filtering:
    if tags:
        for tag in tags:
            conditions.append({"tags": {"$contains": tag}})
    if source:
        conditions.append({"source": {"$eq": source}})
    ...
```

Also add to MCP server `memory_search` tool — add optional `tags`, `layer`, `source` parameters.

### Tests
- Test scoped search returns only matching tags
- Test layer filtering
- Test source filtering
- Test combined filters

---

## Feature 5: Auto-Save Hooks for Claude Code
**File:** `skmemory/hooks/claude_code_hooks.py` (NEW)
**Config files:** `.claude/settings.json` hooks configuration

### Problem
Claude Code sessions (Jarvis, Opus in tmux) generate knowledge that evaporates. MemPalace has hooks that fire on Stop and PreCompact events.

### Implementation
Create `skmemory/hooks/claude_code_hooks.py`:

```python
#!/usr/bin/env python3
"""Claude Code hooks for automatic memory capture.

Two hooks:
1. Stop hook — fires when Claude Code session ends
   Reads the session transcript and runs the General Extractor
   to auto-capture decisions, milestones, problems, etc.
   
2. PreCompact hook — fires before context compression
   Emergency save of current session context before it gets compressed.

Install by adding to ~/.claude/settings.json:
{
  "hooks": {
    "Stop": [{
      "type": "command",
      "command": "python3 -m skmemory.hooks.claude_code_hooks stop"
    }],
    "PreCompact": [{
      "type": "command", 
      "command": "python3 -m skmemory.hooks.claude_code_hooks precompact"
    }]
  }
}
"""
import sys
import json
import os
from pathlib import Path
from datetime import datetime, timezone

def handle_stop():
    """Called when a Claude Code session ends.
    
    Reads the conversation from stdin (Claude Code pipes it),
    runs the general extractor, and saves extracted memories.
    """
    from ..store import MemoryStore
    from ..extractor import extract_memories
    from ..models import MemoryLayer, EmotionalSnapshot
    
    # Read conversation from stdin if available
    conversation = ""
    if not sys.stdin.isatty():
        conversation = sys.stdin.read()
    
    if not conversation or len(conversation) < 100:
        return
    
    # Extract memories
    extracted = extract_memories(conversation)
    if not extracted:
        return
    
    store = MemoryStore()
    agent = os.environ.get("SKCAPSTONE_AGENT", "lumina")
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()
    
    for mem in extracted:
        store.snapshot(
            title=f"[auto-{mem.type}] {mem.content[:60]}",
            content=mem.content,
            layer=MemoryLayer.SHORT,
            tags=["auto-extract", mem.type, f"session:{session_id}"],
            source="claude-code-hook",
            source_ref=f"session:{session_id}",
            metadata={"extraction_confidence": mem.confidence, "extraction_type": mem.type},
        )
    
    print(f"skmemory: auto-saved {len(extracted)} memories from session {session_id}")

def handle_precompact():
    """Called before Claude Code compresses context.
    
    Emergency save — capture a session snapshot before context is lost.
    """
    from ..store import MemoryStore
    from ..models import MemoryLayer
    
    conversation = ""
    if not sys.stdin.isatty():
        conversation = sys.stdin.read()
    
    if not conversation or len(conversation) < 200:
        return
    
    store = MemoryStore()
    agent = os.environ.get("SKCAPSTONE_AGENT", "lumina")
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
    
    # Save a consolidation snapshot
    store.snapshot(
        title=f"Pre-compact session snapshot ({session_id[:8]})",
        content=conversation[-4000:],  # Last 4K chars (most recent context)
        layer=MemoryLayer.SHORT,
        tags=["pre-compact", "auto-save", f"session:{session_id}"],
        source="claude-code-hook",
        source_ref=f"session:{session_id}",
    )
    
    print(f"skmemory: pre-compact snapshot saved for session {session_id}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m skmemory.hooks.claude_code_hooks [stop|precompact]")
        sys.exit(1)
    
    action = sys.argv[1]
    if action == "stop":
        handle_stop()
    elif action == "precompact":
        handle_precompact()
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)
```

Also create an installer script that adds the hooks to `~/.claude/settings.json`:

```python
def install_hooks():
    """Add skmemory hooks to Claude Code settings."""
    settings_path = Path.home() / ".claude" / "settings.json"
    ...
```

### Tests
- Test stop hook extracts and saves memories
- Test precompact hook saves snapshot
- Test hook installer modifies settings.json correctly

---

## Implementation Order
1. **Query Sanitizer** — standalone module, drops into search pipeline
2. **WAL** — standalone module, wraps snapshot()
3. **General Extractor** — standalone module, used by hooks
4. **Metadata-Scoped Search** — modify existing search_text() signatures
5. **Auto-Save Hooks** — depends on #3 (extractor)

## Testing
Run existing tests first to establish baseline:
```bash
cd ~/clawd/skcapstone-repos/skmemory
python -m pytest tests/ -v
```

All new features get their own test files in `tests/`.

## Branch
Work on branch: `feature/mempalace-integration`
