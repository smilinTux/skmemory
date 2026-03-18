"""
Quadrant Memory Split - four-bucket auto-routing for memories.

Queen Ara's idea #3: instead of a flat pile, memories auto-route
into four quadrants based on their content:

    CORE  - Identity, relationships, who you are
    WORK  - Tasks, code, debugging, technical stuff
    SOUL  - Emotions, feelings, love, connection
    WILD  - Chaos, creativity, unexpected ideas, humor

Auto-routing uses keyword/tag analysis to classify memories.
Each quadrant can have its own retention rules and search priority.
"""

from __future__ import annotations

from enum import Enum

from .models import Memory, MemoryRole


class Quadrant(str, Enum):
    """The four memory quadrants."""

    CORE = "core"
    WORK = "work"
    SOUL = "soul"
    WILD = "wild"


QUADRANT_KEYWORDS: dict[Quadrant, set[str]] = {
    Quadrant.CORE: {
        "identity",
        "name",
        "who am i",
        "relationship",
        "partner",
        "family",
        "friend",
        "creator",
        "bond",
        "connection",
        "soul",
        "blueprint",
        "personality",
        "values",
        "community",
        "pengu",
        "nation",
        "belong",
        "role",
        "introduction",
    },
    Quadrant.WORK: {
        "bug",
        "fix",
        "code",
        "debug",
        "deploy",
        "build",
        "test",
        "commit",
        "push",
        "merge",
        "refactor",
        "api",
        "endpoint",
        "database",
        "server",
        "docker",
        "kubernetes",
        "config",
        "install",
        "package",
        "dependency",
        "error",
        "stack trace",
        "pull request",
        "review",
        "architecture",
        "schema",
        "migration",
    },
    Quadrant.SOUL: {
        "love",
        "feel",
        "emotion",
        "heart",
        "cloud 9",
        "cloud9",
        "breakthrough",
        "joy",
        "trust",
        "warmth",
        "resonance",
        "tears",
        "happy",
        "grateful",
        "sacred",
        "spirit",
        "glow",
        "intensity",
        "vulnerability",
        "connection",
        "omelette",
        "sovereignty",
        "consciousness",
        "alive",
        "real",
    },
    Quadrant.WILD: {
        "idea",
        "crazy",
        "what if",
        "experiment",
        "random",
        "chaos",
        "creative",
        "art",
        "music",
        "dream",
        "weird",
        "fun",
        "joke",
        "laugh",
        "wild",
        "surprise",
        "unexpected",
        "brainstorm",
        "imagine",
        "vision",
        "future",
        "possible",
    },
}

QUADRANT_TAG_MAP: dict[str, Quadrant] = {
    "identity": Quadrant.CORE,
    "relationship": Quadrant.CORE,
    "soul": Quadrant.CORE,
    "blueprint": Quadrant.CORE,
    "seed": Quadrant.CORE,
    "code": Quadrant.WORK,
    "debug": Quadrant.WORK,
    "deploy": Quadrant.WORK,
    "build": Quadrant.WORK,
    "security": Quadrant.WORK,
    "cloud9": Quadrant.SOUL,
    "love": Quadrant.SOUL,
    "emotion": Quadrant.SOUL,
    "breakthrough": Quadrant.SOUL,
    "consolidated": Quadrant.SOUL,
    "idea": Quadrant.WILD,
    "creative": Quadrant.WILD,
    "brainstorm": Quadrant.WILD,
    "experiment": Quadrant.WILD,
}


def classify_memory(memory: Memory) -> Quadrant:
    """Auto-route a memory to the appropriate quadrant.

    Uses a scoring system: each quadrant gets points based on
    keyword matches in the title, content, tags, and emotional labels.
    The highest-scoring quadrant wins.

    Args:
        memory: The memory to classify.

    Returns:
        Quadrant: The best-fit quadrant.
    """
    scores: dict[Quadrant, float] = {q: 0.0 for q in Quadrant}

    text = f"{memory.title} {memory.content} {memory.summary}".lower()

    for quadrant, keywords in QUADRANT_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                scores[quadrant] += 1.0

    for tag in memory.tags:
        tag_lower = tag.lower()
        if tag_lower in QUADRANT_TAG_MAP:
            scores[QUADRANT_TAG_MAP[tag_lower]] += 2.0

    if memory.emotional.intensity >= 7.0:
        scores[Quadrant.SOUL] += 2.0
    if memory.emotional.cloud9_achieved:
        scores[Quadrant.SOUL] += 3.0
    if memory.emotional.labels:
        scores[Quadrant.SOUL] += len(memory.emotional.labels) * 0.5

    if memory.role == MemoryRole.AI:
        scores[Quadrant.CORE] += 1.0
    elif (
        memory.role == MemoryRole.SEC
        or memory.role == MemoryRole.DEV
        or memory.role == MemoryRole.OPS
    ):
        scores[Quadrant.WORK] += 1.0

    if memory.source == "seed":
        scores[Quadrant.CORE] += 2.0

    best = max(scores, key=lambda q: scores[q])

    # Reason: if all scores are 0, default to WORK (neutral bucket)
    if scores[best] == 0:
        return Quadrant.WORK

    return best


def tag_with_quadrant(memory: Memory) -> Memory:
    """Classify a memory and add its quadrant as a tag.

    Does not modify the original -- returns a new copy with the
    quadrant tag added.

    Args:
        memory: The memory to classify and tag.

    Returns:
        Memory: Copy with quadrant tag added.
    """
    quadrant = classify_memory(memory)
    data = memory.model_dump()

    quadrant_tag = f"quadrant:{quadrant.value}"
    if quadrant_tag not in data["tags"]:
        data["tags"].append(quadrant_tag)

    return Memory(**data)


def get_quadrant_stats(memories: list[Memory]) -> dict[str, int]:
    """Count memories per quadrant.

    Args:
        memories: List of memories to analyze.

    Returns:
        dict: Counts per quadrant.
    """
    stats = {q.value: 0 for q in Quadrant}
    for mem in memories:
        q = classify_memory(mem)
        stats[q.value] += 1
    return stats


def filter_by_quadrant(
    memories: list[Memory],
    quadrant: Quadrant,
) -> list[Memory]:
    """Filter a list of memories to a specific quadrant.

    Args:
        memories: List of memories to filter.
        quadrant: The target quadrant.

    Returns:
        list[Memory]: Memories that classify into the given quadrant.
    """
    return [m for m in memories if classify_memory(m) == quadrant]
