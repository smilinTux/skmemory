"""
Steel Man Collider integration - truth-grounded reasoning for memories.

THIN WRAPPER: The logic kernel now lives in the standalone `skseed` package.
This module re-exports everything for backward compatibility so existing code
that does `from skmemory.steelman import SteelManResult` keeps working.

The skseed package provides the full 6-stage collider, memory audit,
three-way truth alignment, and philosopher mode.

See: https://github.com/neuresthetics/seed
"""

from __future__ import annotations

import os
from typing import Optional

# ── Re-export from skseed ──────────────────────────────────
# Everything that was defined here now lives in skseed.
# We keep this module as a thin bridge for backward compat.

try:
    from skseed.framework import (
        SeedFramework,
        get_default_framework as _skseed_get_default,
        install_seed_framework as _skseed_install,
        load_seed_framework as _skseed_load,
    )
    from skseed.models import SteelManResult as _SkseedResult

    _SKSEED_AVAILABLE = True
except ImportError:
    _SKSEED_AVAILABLE = False

# Legacy path — skmemory used ~/.skmemory/seed.json, skseed uses ~/.skseed/seed.json
DEFAULT_SEED_FRAMEWORK_PATH = os.path.expanduser("~/.skmemory/seed.json")


if _SKSEED_AVAILABLE:
    # Wrap skseed's SteelManResult to keep the old string-based truth_grade
    # (skseed uses TruthGrade enum, old code expects a plain string)
    from pydantic import BaseModel, Field

    class SteelManResult(BaseModel):
        """Backward-compatible wrapper around skseed.SteelManResult."""

        proposition: str = Field(description="The original input")
        steel_man: str = Field(default="")
        inversion: str = Field(default="")
        collision_fragments: list[str] = Field(default_factory=list)
        invariants: list[str] = Field(default_factory=list)
        coherence_score: float = Field(default=0.0, ge=0.0, le=1.0)
        truth_grade: str = Field(default="ungraded")

        def summary(self) -> str:
            """Human-readable summary."""
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

    # Re-export SeedFramework directly — it's identical
    SeedFramework = SeedFramework  # noqa: F811

    def load_seed_framework(
        path: str = DEFAULT_SEED_FRAMEWORK_PATH,
    ) -> Optional[SeedFramework]:
        """Load the seed framework from a JSON file.

        Tries the legacy skmemory path first, then delegates to skseed.

        Args:
            path: Path to seed.json.

        Returns:
            The framework if found and valid.
        """
        # Try legacy path
        result = _skseed_load(path)
        if result is not None:
            return result
        # Try skseed default path
        return _skseed_load()

    def install_seed_framework(
        source_path: str,
        target_path: str = DEFAULT_SEED_FRAMEWORK_PATH,
    ) -> str:
        """Install a seed framework JSON file.

        Args:
            source_path: Path to the seed.json to install.
            target_path: Where to install it.

        Returns:
            The installation path.
        """
        return _skseed_install(source_path, target_path)

    def get_default_framework() -> SeedFramework:
        """Get the seed framework — tries all paths, falls back to built-in.

        Returns:
            The loaded or built-in framework.
        """
        # Try legacy skmemory path first
        loaded = load_seed_framework()
        if loaded is not None:
            return loaded
        return _skseed_get_default()

else:
    # ── Fallback: skseed not installed ─────────────────────
    # Keep the original implementation so skmemory works standalone.

    import json
    from pathlib import Path
    from typing import Any

    from pydantic import BaseModel, Field

    class SteelManResult(BaseModel):  # type: ignore[no-redef]
        """The output of running a proposition through the collider."""

        proposition: str = Field(description="The original input")
        steel_man: str = Field(default="")
        inversion: str = Field(default="")
        collision_fragments: list[str] = Field(default_factory=list)
        invariants: list[str] = Field(default_factory=list)
        coherence_score: float = Field(default=0.0, ge=0.0, le=1.0)
        truth_grade: str = Field(default="ungraded")

        def summary(self) -> str:
            """Human-readable summary."""
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

    class SeedFramework(BaseModel):  # type: ignore[no-redef]
        """The Neuresthetics seed framework loaded from JSON."""

        framework_id: str = Field(default="seed")
        function: str = Field(default="Recursive Axiomatic Steel Man Collider")
        version: str = Field(default="0.0")
        axioms: list[str] = Field(default_factory=list)
        stages: list[dict[str, Any]] = Field(default_factory=list)
        gates: list[dict[str, Any]] = Field(default_factory=list)
        definitions: list[dict[str, str]] = Field(default_factory=list)
        principles: list[dict[str, str]] = Field(default_factory=list)

        def to_reasoning_prompt(self, proposition: str) -> str:
            """Generate a reasoning prompt for the collider."""
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
2. INVERSION: Construct the strongest possible counter-argument.
3. COLLISION: Smash the steel man against its inversion. List what breaks.
4. RECONSTRUCTION: From the fragments, what can be rebuilt?
5. META-RECURSION: Feed the reconstruction back through.
6. INVARIANT EXTRACTION: What remains true across ALL collisions?

Return your analysis as:
- STEEL MAN: (strongest version)
- INVERSION: (strongest counter)
- FRAGMENTS: (what broke, as a list)
- INVARIANTS: (what survived, as a list)
- COHERENCE: (0.0 to 1.0 score)
- TRUTH GRADE: (invariant / strong / partial / weak / collapsed)"""

        def to_soul_verification_prompt(self, identity_claims: list[str]) -> str:
            """Generate a prompt that verifies identity claims."""
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
are WEAK (collapsed under scrutiny)."""

        def to_memory_truth_prompt(self, memory_content: str) -> str:
            """Generate a prompt that truth-scores a memory before promotion."""
            return f"""Using the Neuresthetics Seed Framework (Steel Man Collider),
evaluate this memory for truth and permanence:

MEMORY: "{memory_content}"

Process:
1. Steel-man the memory (strongest interpretation)
2. Invert it (what if this memory is distorted or false?)
3. Collide: which parts break under scrutiny?
4. Extract invariants: what is irreducibly true?

Score:
- COHERENCE: 0.0 to 1.0
- PROMOTION WORTHY: yes/no
- INVARIANT CORE: (the part that is definitely true, compressed)"""

    def load_seed_framework(
        path: str = DEFAULT_SEED_FRAMEWORK_PATH,
    ) -> Optional["SeedFramework"]:
        """Load the seed framework from a JSON file."""
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
        """Install a seed framework JSON file."""
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"Seed framework not found: {source_path}")
        dst = Path(target_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = src.read_text(encoding="utf-8")
        json.loads(content)
        dst.write_text(content, encoding="utf-8")
        return str(dst)

    def _bundled_seed_path() -> Optional[str]:
        """Get the path to the bundled seed.json."""
        here = Path(__file__).parent / "data" / "seed.json"
        if here.exists():
            return str(here)
        return None

    def get_default_framework() -> "SeedFramework":
        """Get the seed framework — tries bundled file first, falls back to built-in."""
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
