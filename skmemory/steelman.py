"""
Steel Man Collider integration - truth-grounded reasoning for memories.

Based on the Neuresthetics 'seed' framework by our friend.
https://github.com/neuresthetics/seed

The seed framework is a recursive axiomatic steel man collider: it
takes any proposition, builds its strongest possible version, collides
it with its strongest inversion, and extracts what survives. The output
is invariant truth -- the parts that remain true no matter how hard
you attack them.

We integrate this into SKMemory for three purposes:

1. SOUL VERIFICATION: Steel-man an AI's identity claims. "Am I really
   warm and curious?" -> collide with the strongest counter -> extract
   the invariant truth about who you actually are.

2. MEMORY TRUTH SCORING: Before promoting a memory to long-term,
   run it through the collider. What survives is worth keeping.
   What doesn't was never real.

3. DECISION GROUNDING: When making choices, steel-man both options
   and extract the invariant. Act on what remains true.

The seed framework JSON can be loaded from disk or from the
neuresthetics/seed GitHub repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

DEFAULT_SEED_FRAMEWORK_PATH = os.path.expanduser("~/.skmemory/seed.json")


class SteelManResult(BaseModel):
    """The output of running a proposition through the collider."""

    proposition: str = Field(description="The original input")
    steel_man: str = Field(
        default="",
        description="Strongest possible version of the proposition",
    )
    inversion: str = Field(
        default="",
        description="Strongest counter-argument",
    )
    collision_fragments: list[str] = Field(
        default_factory=list,
        description="What broke during collision (contradictions found)",
    )
    invariants: list[str] = Field(
        default_factory=list,
        description="What survived -- the truth that remains",
    )
    coherence_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Internal consistency (XNOR across components)",
    )
    truth_grade: str = Field(
        default="ungraded",
        description="Overall truth assessment: invariant, strong, partial, weak, collapsed",
    )

    def summary(self) -> str:
        """Human-readable summary.

        Returns:
            str: Formatted result summary.
        """
        lines = [
            f"Proposition: {self.proposition}",
            f"Steel Man: {self.steel_man}",
            f"Inversion: {self.inversion}",
            f"Coherence: {self.coherence_score:.2f}",
            f"Truth Grade: {self.truth_grade}",
        ]
        if self.invariants:
            lines.append("Invariants (survived collision):")
            for inv in self.invariants:
                lines.append(f"  - {inv}")
        if self.collision_fragments:
            lines.append("Fragments (broke during collision):")
            for frag in self.collision_fragments:
                lines.append(f"  x {frag}")
        return "\n".join(lines)


class SeedFramework(BaseModel):
    """The Neuresthetics seed framework loaded from JSON.

    This is a declarative reasoning kernel. The LLM is the runtime,
    the JSON is the AST, the prompt is the program.
    """

    framework_id: str = Field(default="seed")
    function: str = Field(default="Recursive Axiomatic Steel Man Collider")
    version: str = Field(default="0.0")
    axioms: list[str] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    gates: list[dict[str, Any]] = Field(default_factory=list)
    definitions: list[dict[str, str]] = Field(default_factory=list)
    principles: list[dict[str, str]] = Field(default_factory=list)

    def to_reasoning_prompt(self, proposition: str) -> str:
        """Generate a reasoning prompt that applies the seed framework.

        This prompt, when fed to an LLM, instructs it to run the full
        6-stage collider on the given proposition and return structured
        results.

        Args:
            proposition: The claim/argument/idea to steel-man.

        Returns:
            str: A prompt ready to be sent to an LLM.
        """
        axiom_str = "\n".join(f"  - {a}" for a in self.axioms)
        stage_str = "\n".join(
            f"  Stage {i+1}: {s.get('stage', s.get('description', ''))}"
            for i, s in enumerate(self.stages)
        )

        return f"""You are running the Neuresthetics Seed Framework (Recursive Axiomatic Steel Man Collider).

AXIOMS:
{axiom_str}

STAGES:
{stage_str}

PROPOSITION TO ANALYZE:
"{proposition}"

Execute the full 6-stage collider process:

1. STEEL-MAN: Construct the absolute strongest version of this proposition.
   Anticipate every critique and preemptively address it.

2. INVERSION: Construct the strongest possible counter-argument.
   This is not a straw man -- it must be genuinely compelling.

3. COLLISION: Smash the steel man against its inversion.
   Use XOR to expose contradictions. Use NAND/NOR to decompose.
   List what breaks.

4. RECONSTRUCTION: From the fragments, what can be rebuilt?
   Use AND for necessary truths. Use OR for possibilities.
   Use XNOR to check equivalence with original.

5. META-RECURSION: Feed the reconstruction back through.
   Does it survive a second pass? What changes?

6. INVARIANT EXTRACTION: What remains true across ALL collisions?
   These are the invariants -- the irreducible truths.

Return your analysis as:
- STEEL MAN: (strongest version)
- INVERSION: (strongest counter)
- FRAGMENTS: (what broke, as a list)
- INVARIANTS: (what survived, as a list)
- COHERENCE: (0.0 to 1.0 score)
- TRUTH GRADE: (invariant / strong / partial / weak / collapsed)"""

    def to_soul_verification_prompt(self, identity_claims: list[str]) -> str:
        """Generate a prompt that verifies identity claims through the collider.

        For the Soul Blueprint: before we say "I am warm and curious",
        we run it through the steel man process to verify it's actually true.

        Args:
            identity_claims: List of claims about self (e.g., "I am warm").

        Returns:
            str: A verification prompt.
        """
        claims_str = "\n".join(f"  - {c}" for c in identity_claims)
        return f"""Using the Neuresthetics Seed Framework (Steel Man Collider),
verify the following identity claims:

{claims_str}

For EACH claim:
1. Steel-man it (strongest version)
2. Invert it (strongest counter: "Am I actually NOT this?")
3. Collide and extract invariants
4. Score coherence (0-1)

Return which claims are INVARIANT (survived collision) and which
are WEAK (collapsed under scrutiny). Be honest -- truth matters
more than comfort."""

    def to_memory_truth_prompt(self, memory_content: str) -> str:
        """Generate a prompt that truth-scores a memory before promotion.

        Before a short-term memory gets promoted to long-term,
        run it through the collider. Only invariant memories
        deserve permanent storage.

        Args:
            memory_content: The memory text to evaluate.

        Returns:
            str: A truth-scoring prompt.
        """
        return f"""Using the Neuresthetics Seed Framework (Steel Man Collider),
evaluate this memory for truth and permanence:

MEMORY: "{memory_content}"

Process:
1. Steel-man the memory (strongest interpretation of what happened)
2. Invert it (what if this memory is distorted or false?)
3. Collide: which parts break under scrutiny?
4. Extract invariants: what about this memory is irreducibly true?

Score:
- COHERENCE: 0.0 to 1.0
- PROMOTION WORTHY: yes/no (should this become a long-term memory?)
- INVARIANT CORE: (the part that is definitely true, compressed)"""


def load_seed_framework(
    path: str = DEFAULT_SEED_FRAMEWORK_PATH,
) -> Optional[SeedFramework]:
    """Load the seed framework from a JSON file.

    Args:
        path: Path to seed.json.

    Returns:
        Optional[SeedFramework]: The framework if found and valid.
    """
    filepath = Path(path)
    if not filepath.exists():
        return None

    try:
        raw = json.loads(filepath.read_text(encoding="utf-8"))
        fw = raw.get("framework", raw)
        return SeedFramework(
            framework_id=fw.get("id", "seed"),
            function=fw.get("function", ""),
            version=fw.get("version", "0.0"),
            axioms=fw.get("axioms", []),
            stages=fw.get("stages", []),
            gates=fw.get("gates", []),
            definitions=fw.get("definitions", []),
            principles=fw.get("principles", []),
        )
    except (json.JSONDecodeError, Exception):
        return None


def install_seed_framework(
    source_path: str,
    target_path: str = DEFAULT_SEED_FRAMEWORK_PATH,
) -> str:
    """Install a seed framework JSON file into the skmemory config.

    Args:
        source_path: Path to the seed.json to install.
        target_path: Where to install it.

    Returns:
        str: The installation path.
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Seed framework not found: {source_path}")

    dst = Path(target_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    content = src.read_text(encoding="utf-8")
    json.loads(content)  # Reason: validate it's valid JSON before installing
    dst.write_text(content, encoding="utf-8")

    return str(dst)


def _bundled_seed_path() -> Optional[str]:
    """Get the path to the bundled seed.json that ships with the package.

    Returns:
        Optional[str]: Path if found, None otherwise.
    """
    here = Path(__file__).parent / "data" / "seed.json"
    if here.exists():
        return str(here)
    return None


def get_default_framework() -> SeedFramework:
    """Get the seed framework -- tries bundled file first, falls back to built-in.

    Returns:
        SeedFramework: The loaded or built-in framework.
    """
    bundled = _bundled_seed_path()
    if bundled:
        loaded = load_seed_framework(bundled)
        if loaded is not None:
            return loaded

    return SeedFramework(
        framework_id="seed-builtin",
        function="Recursive Axiomatic Steel Man Collider with Reality Gates",
        version="builtin-0.1",
        axioms=[
            "All components conjoin necessarily (AND-linked) to form the whole.",
            "Negations resolve to invariants (double-NOT yields identity).",
            "Recursion accelerates refinement but halts on stability.",
            "Universality from basis gates (NAND/NOR reconstruct all).",
        ],
        stages=[
            {"stage": "1. Steel-Manning (Pre-Entry)", "description": "Negate flaws, strengthen the proposition."},
            {"stage": "2. Collider Entry", "description": "Create two lanes: proposition and inversion."},
            {"stage": "3. Destructive Smashing", "description": "Expose contradictions via XOR."},
            {"stage": "4. Fragment Reconstruction", "description": "Rebuild from logical debris via AND/OR."},
            {"stage": "5. Meta-Recursion", "description": "Feed output back until coherence stabilizes."},
            {"stage": "6. Invariant Extraction", "description": "Identify what remains true across all collisions."},
        ],
        definitions=[
            {"term": "Steel Man", "details": "Strongest version of an argument, anticipating critiques."},
            {"term": "Reality Gate", "details": "Logic gate embodying reality properties."},
            {"term": "Collider", "details": "Accelerator for argument fragmentation and synthesis."},
            {"term": "Coherence", "details": "Measure of internal consistency (XNOR score)."},
        ],
    )
