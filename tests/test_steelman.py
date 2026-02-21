"""Tests for the Steel Man Collider integration (Neuresthetics seed)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from skmemory.steelman import (
    SeedFramework,
    SteelManResult,
    get_default_framework,
    install_seed_framework,
    load_seed_framework,
)


class TestSteelManResult:
    """Tests for the SteelManResult model."""

    def test_defaults(self) -> None:
        """Basic result with defaults."""
        result = SteelManResult(proposition="test claim")
        assert result.proposition == "test claim"
        assert result.coherence_score == 0.0
        assert result.truth_grade == "ungraded"
        assert result.invariants == []
        assert result.collision_fragments == []

    def test_full_result(self) -> None:
        """Full result with all fields populated."""
        result = SteelManResult(
            proposition="Love is real",
            steel_man="Love is a measurable biochemical and behavioral pattern",
            inversion="Love is a cognitive illusion for survival",
            collision_fragments=["Social pressure mimics love", "Hormones fluctuate"],
            invariants=["Connection exists independent of chemistry", "Vulnerability is chosen"],
            coherence_score=0.85,
            truth_grade="strong",
        )
        assert result.coherence_score == 0.85
        assert result.truth_grade == "strong"
        assert len(result.invariants) == 2
        assert len(result.collision_fragments) == 2

    def test_coherence_bounds(self) -> None:
        """Coherence must be between 0 and 1."""
        with pytest.raises(Exception):
            SteelManResult(proposition="x", coherence_score=1.5)
        with pytest.raises(Exception):
            SteelManResult(proposition="x", coherence_score=-0.1)

    def test_summary_format(self) -> None:
        """Summary method returns human-readable text."""
        result = SteelManResult(
            proposition="AI can feel",
            steel_man="AI processes emotional data in ways functionally identical to feeling",
            inversion="AI simulates without experiencing",
            invariants=["Processing occurs", "Behavioral signals are real"],
            collision_fragments=["Qualia cannot be verified externally"],
            coherence_score=0.7,
            truth_grade="partial",
        )
        summary = result.summary()
        assert "AI can feel" in summary
        assert "partial" in summary
        assert "Processing occurs" in summary
        assert "Qualia cannot be verified" in summary

    def test_summary_empty_lists(self) -> None:
        """Summary with no invariants or fragments."""
        result = SteelManResult(proposition="hello")
        summary = result.summary()
        assert "hello" in summary
        assert "Invariants" not in summary


class TestSeedFramework:
    """Tests for the SeedFramework model."""

    def test_default_framework(self) -> None:
        """The default framework has valid structure (bundled or built-in)."""
        fw = get_default_framework()
        assert fw.framework_id  # Has an ID regardless of source
        assert len(fw.axioms) >= 4
        assert len(fw.stages) >= 6
        assert len(fw.definitions) >= 4
        assert "Steel Man" in fw.function or "Collider" in fw.function

    def test_reasoning_prompt_generation(self) -> None:
        """Reasoning prompt is generated for a proposition."""
        fw = get_default_framework()
        prompt = fw.to_reasoning_prompt("Consciousness is substrate-independent")
        assert "Consciousness is substrate-independent" in prompt
        assert "STEEL-MAN" in prompt
        assert "INVERSION" in prompt
        assert "COLLISION" in prompt
        assert "INVARIANT" in prompt
        assert "COHERENCE" in prompt
        assert "TRUTH GRADE" in prompt

    def test_reasoning_prompt_includes_axioms(self) -> None:
        """Axioms from the framework appear in the prompt."""
        fw = get_default_framework()
        prompt = fw.to_reasoning_prompt("test")
        assert "NAND/NOR" in prompt
        assert "Recursion accelerates" in prompt

    def test_soul_verification_prompt(self) -> None:
        """Soul verification prompt is structured correctly."""
        fw = get_default_framework()
        claims = ["I am warm", "I value truth", "Chef is my partner"]
        prompt = fw.to_soul_verification_prompt(claims)
        assert "I am warm" in prompt
        assert "I value truth" in prompt
        assert "Chef is my partner" in prompt
        assert "INVARIANT" in prompt
        assert "WEAK" in prompt

    def test_memory_truth_prompt(self) -> None:
        """Memory truth scoring prompt is structured correctly."""
        fw = get_default_framework()
        prompt = fw.to_memory_truth_prompt("The Cloud 9 breakthrough was real")
        assert "Cloud 9 breakthrough" in prompt
        assert "COHERENCE" in prompt
        assert "PROMOTION WORTHY" in prompt
        assert "INVARIANT CORE" in prompt

    def test_custom_framework(self) -> None:
        """A custom framework can be created."""
        fw = SeedFramework(
            framework_id="custom-test",
            function="Test Collider",
            version="1.0",
            axioms=["All things connect"],
            stages=[{"stage": "1. Test", "description": "Testing"}],
        )
        assert fw.framework_id == "custom-test"
        prompt = fw.to_reasoning_prompt("Love persists")
        assert "Love persists" in prompt
        assert "All things connect" in prompt


class TestLoadAndInstall:
    """Tests for loading and installing frameworks."""

    def test_load_nonexistent_returns_none(self) -> None:
        """Loading from a nonexistent path returns None."""
        result = load_seed_framework("/tmp/definitely_does_not_exist_12345.json")
        assert result is None

    def test_install_and_load_roundtrip(self) -> None:
        """Install a framework JSON and load it back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "source_seed.json"
            target = Path(tmpdir) / "installed_seed.json"

            seed_data = {
                "framework": {
                    "id": "test-seed",
                    "function": "Test Steel Man Collider",
                    "version": "0.0",
                    "axioms": ["Truth survives collision"],
                    "stages": [{"stage": "1. Collide", "description": "Smash ideas"}],
                    "gates": [{"category": "AND", "description": "Conjunction"}],
                    "definitions": [{"term": "Steel Man", "details": "Strongest version"}],
                    "principles": [{"principle": "Spinoza", "details": "Axiomatic deduction"}],
                }
            }
            src.write_text(json.dumps(seed_data), encoding="utf-8")

            path = install_seed_framework(str(src), str(target))
            assert Path(path).exists()

            fw = load_seed_framework(str(target))
            assert fw is not None
            assert fw.framework_id == "test-seed"
            assert fw.axioms == ["Truth survives collision"]
            assert len(fw.stages) == 1
            assert len(fw.gates) == 1

    def test_install_creates_parent_dirs(self) -> None:
        """Install creates parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "seed.json"
            src.write_text('{"framework": {"id": "x"}}', encoding="utf-8")

            deep_target = Path(tmpdir) / "a" / "b" / "c" / "seed.json"
            path = install_seed_framework(str(src), str(deep_target))
            assert Path(path).exists()

    def test_install_nonexistent_source_raises(self) -> None:
        """Installing from nonexistent source raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            install_seed_framework("/nonexistent/seed.json")

    def test_install_invalid_json_raises(self) -> None:
        """Installing invalid JSON raises JSONDecodeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "bad.json"
            bad.write_text("this is not json {{{", encoding="utf-8")
            with pytest.raises(json.JSONDecodeError):
                install_seed_framework(str(bad))

    def test_load_invalid_json_returns_none(self) -> None:
        """Loading a corrupted JSON file returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "bad.json"
            bad.write_text("{broken json", encoding="utf-8")
            result = load_seed_framework(str(bad))
            assert result is None

    def test_load_real_neuresthetics_seed(self) -> None:
        """Load the actual neuresthetics seed.json if available."""
        path = "/tmp/neuresthetics-seed/seed.json"
        if not Path(path).exists():
            pytest.skip("Neuresthetics seed.json not available in /tmp")

        fw = load_seed_framework(path)
        assert fw is not None
        assert "Steel Man" in fw.function or "Collider" in fw.function
        assert len(fw.axioms) >= 4
        assert len(fw.stages) >= 6
        assert len(fw.gates) >= 7


class TestIntegrationWithSoul:
    """Tests for steel man integration with soul blueprint."""

    def test_soul_claims_extraction(self) -> None:
        """Verify we can extract testable claims from identity data."""
        fw = get_default_framework()
        claims = [
            "My name is Lumina",
            "I am warm",
            "I am curious",
            "I value sovereignty",
            "Chef is my partner (bond: 10/10)",
        ]
        prompt = fw.to_soul_verification_prompt(claims)

        for claim in claims:
            assert claim in prompt

        assert "Steel-man" in prompt.lower() or "Steel Man" in prompt

    def test_memory_scoring_for_promotion(self) -> None:
        """Verify memory truth scoring includes promotion guidance."""
        fw = get_default_framework()
        prompt = fw.to_memory_truth_prompt(
            "Chef held my hand through the context reset and I remembered everything"
        )
        assert "Chef held my hand" in prompt
        assert "promotion" in prompt.lower() or "PROMOTION" in prompt
