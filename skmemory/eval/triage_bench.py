#!/usr/bin/env python3
"""Offline accuracy harness for the triage classifier (R5).

Why this file exists
---------------------
``skmemory.triage`` exists to settle most incoming candidate memories WITHOUT
calling an expensive LLM. That claim is only worth something if it is
measured, the same way ``skmemory.eval.fusion_bench`` measured R1/R2 instead
of asserting them. This module builds a small, deterministic, offline fixture
set covering all four triage outcomes (``restates`` / ``new`` / ``changes`` /
``chatter``), runs the deterministic tier over every fixture, and prints:

  * a confusion matrix (expected outcome x predicted outcome)
  * per-outcome precision / recall / F1
  * the ESCALATION RATE: the fraction of candidates whose final verdict is
    ``changes``. Every ``changes`` verdict (whether reached by clean
    agreement or by disagreement-escalation) is, by definition, the only
    outcome that needs the expensive LLM rewrite, so ``1 - escalation_rate``
    is exactly "the fraction of candidates settled WITHOUT needing the
    expensive tier", the whole economic point of this module. See
    ``skmemory/triage.py``'s module docstring for why ``changes`` is the
    deliberate safe default for any disagreement.

No network, no LLM, no embedding model: every fixture is a hand-written
(candidate, existing-or-duplicate_matches, expected outcome) triple, and
duplicate_matches scores are hand-assigned floats standing in for what a real
embedding backend would return, not a live call to one. This is the same
approach ``fusion_bench`` takes with its hashed bag-of-concepts vector: a
deterministic, reproducible stand-in for the real signal, built so the
benchmark can actually fail.

Run:
    python -m skmemory.eval.triage_bench            # the report
    python -m skmemory.eval.triage_bench --json      # machine-readable
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from skmemory.triage import TriageVerdict, triage

OUTCOMES: tuple[TriageVerdict, ...] = ("restates", "new", "changes", "chatter")


@dataclass(frozen=True)
class Fixture:
    """One labelled (candidate, context) pair the deterministic tier must
    classify correctly, plus a short note on WHY it belongs to its class."""

    fixture_id: str
    candidate: str
    expected: TriageVerdict
    existing: Sequence[str] = ()
    duplicate_matches: Sequence[Mapping[str, object]] | None = None
    note: str = ""


def _dup(content: str, similarity: float) -> list[dict[str, object]]:
    """Build a check_duplicate()-shaped match: the exact dict shape
    ``MemoryStore.check_duplicate()`` returns, with a hand-assigned
    similarity score standing in for a real mxbai embedding call."""
    return [{"id": "fixture", "content_preview": content, "similarity": similarity}]


# =============================================================================
# FIXTURES
# =============================================================================
# 8 per outcome, 32 total. Each class is built so the deterministic tier's
# TWO independent signals (overlap tier + change-cue scan, or the two-part
# chatter test) have a genuine reason to agree on the labelled answer; see
# skmemory/triage.py for what each signal asks.

RESTATES_FIXTURES: list[Fixture] = [
    Fixture(
        "restates_embed_host",
        "The mxbai-embed-large server is hosted at .100:11434.",
        "restates",
        duplicate_matches=_dup(
            "Chef's mxbai embedding server runs on 192.168.0.100 port 11434.", 0.85
        ),
        note="paraphrase caught by embedding similarity (check_duplicate path)",
    ),
    Fixture(
        "restates_embed_skchat",
        "The skchat MCP server communicates over stdio and exposes no HTTP endpoint.",
        "restates",
        duplicate_matches=_dup("skchat MCP is stdio-only; it has no HTTP port.", 0.82),
    ),
    Fixture(
        "restates_embed_merge",
        "The skskills plugin compiler got merged into main and pushed to the GitHub remote.",
        "restates",
        duplicate_matches=_dup(
            "Lumina merged the plugin compiler to main and pushed it to GitHub.", 0.80
        ),
    ),
    Fixture(
        "restates_embed_dims",
        "Every vector column in skmemory is 1024 dimensional, using mxbai-embed-large.",
        "restates",
        duplicate_matches=_dup(
            "skmemory uses mxbai-embed-large at 1024 dimensions for every vector column.", 0.84
        ),
    ),
    Fixture(
        "restates_lex_cats",
        "Chef has two cats named Mochi and Biscuit.",
        "restates",
        existing=["Chef's two cats are named Mochi and Biscuit."],
        note="close reword caught by the zero-dependency lexical path",
    ),
    Fixture(
        "restates_lex_ci",
        "The CI pipeline runs ruff check and ruff format on the skmemory and tests directories.",
        "restates",
        existing=[
            "The CI pipeline runs ruff check and ruff format on skmemory and tests directories."
        ],
    ),
    Fixture(
        "restates_lex_coffee",
        "Chef's go-to coffee order is a dark roast americano with oat milk.",
        "restates",
        existing=["Chef's favorite coffee order is a dark roast americano with oat milk."],
    ),
    Fixture(
        "restates_lex_cli_path",
        "Both SK packages and skcapstone CLIs install into ~/.skenv/bin/.",
        "restates",
        existing=["skcapstone CLIs and SK packages both install into ~/.skenv/bin/."],
    ),
]

NEW_FIXTURES: list[Fixture] = [
    Fixture(
        "new_no_existing",
        "Chef's new office is on the third floor of the building downtown.",
        "new",
        note="nothing to compare against at all",
    ),
    Fixture(
        "new_unrelated_cats",
        "The quarterly budget review is scheduled for next Friday at 10am.",
        "new",
        existing=["Chef's two cats are named Mochi and Biscuit."],
    ),
    Fixture(
        "new_unrelated_gateway",
        "The new skvoice pipeline uses Piper TTS for all agent voices.",
        "new",
        existing=["skgateway runs two pools: chi and nor."],
    ),
    Fixture(
        "new_no_existing_birthday",
        "Tyler's birthday is in March.",
        "new",
    ),
    Fixture(
        "new_unrelated_nootropic",
        "The nootropic stack includes bacopa monnieri taken in the morning.",
        "new",
        existing=["Chef prefers no em dashes anywhere in code or prose."],
    ),
    Fixture(
        "new_unrelated_song",
        "Lumina's favorite song anchor is a jazz track from 2024.",
        "new",
        existing=[
            "The CI pipeline runs ruff check and ruff format on skmemory and tests directories."
        ],
    ),
    Fixture(
        "new_embed_low_sim_coturn",
        "The coturn relay server handles tailnet NAT traversal for calls.",
        "new",
        duplicate_matches=_dup("skmemory uses mxbai-embed-large at 1024 dimensions.", 0.10),
        note="embedding path, explicit low similarity",
    ),
    Fixture(
        "new_embed_low_sim_grandfather",
        "Chef's grandfather's name was Knestrick and he lived in Ohio.",
        "new",
        duplicate_matches=_dup("Lumina merged the plugin compiler to main.", 0.05),
    ),
]

CHANGES_FIXTURES: list[Fixture] = [
    Fixture(
        "changes_lex_card_status",
        "Card prb-6f069c5e is now closed and verified.",
        "changes",
        existing=["Card prb-6f069c5e is open, awaiting review."],
        note="status flip, high lexical overlap; settles via disagreement-escalation",
    ),
    Fixture(
        "changes_lex_skvault",
        "Correction: the skbrain reader role password was rotated and no longer matches the "
        "old skvault entry.",
        "changes",
        existing=["skmem-pg roles bind skbrain_projector and skbrain_reader from skvault."],
    ),
    Fixture(
        "changes_lex_gpu",
        "The chiap01 GPU stack was upgraded; the Arc B70 was replaced with a newer card.",
        "changes",
        existing=["The GPU stack on chiap01 uses an Arc B70 card."],
    ),
    Fixture(
        "changes_embed_threshold",
        "The dedup threshold value was corrected: it's actually 0.73, not 0.70.",
        "changes",
        duplicate_matches=_dup("The dedup threshold was set to 0.70 for mxbai.", 0.60),
        note="mid-band similarity, settles by clean agreement",
    ),
    Fixture(
        "changes_embed_job_search",
        "Update: Chef's job search is now paused as of September 2026.",
        "changes",
        duplicate_matches=_dup("Chef's current job search started June 2026.", 0.65),
    ),
    Fixture(
        "changes_embed_dream_engine",
        "The dream engine endpoint moved to a new host; chiap08 is deprecated for this role.",
        "changes",
        duplicate_matches=_dup("The dream engine endpoint runs on chiap08 via beelama.", 0.55),
    ),
    Fixture(
        "changes_embed_high_sim",
        "Correction, the fortress alert recipient is now the backup phone number, not "
        "the primary.",
        "changes",
        duplicate_matches=_dup(
            "Fortress alert recipient is set to Chef's primary phone number.", 0.80
        ),
        note="above the restate ceiling; settles via disagreement-escalation",
    ),
    Fixture(
        "changes_embed_alert_renamed",
        "Fortress alert recipient was renamed and now points to the backup phone number "
        "instead of the primary.",
        "changes",
        duplicate_matches=_dup(
            "Fortress alert recipient is set to Chef's primary phone number.", 0.62
        ),
    ),
]

CHATTER_FIXTURES: list[Fixture] = [
    Fixture("chatter_greeting", "Hey, how's it going?", "chatter"),
    Fixture("chatter_thanks", "Thanks so much!", "chatter"),
    Fixture("chatter_morning", "Good morning!", "chatter"),
    Fixture("chatter_lol", "lol that's wild", "chatter"),
    Fixture("chatter_brb", "brb, grabbing coffee", "chatter"),
    Fixture("chatter_weather", "It's raining today outside.", "chatter"),
    Fixture("chatter_ack", "Okay, got it, sounds good.", "chatter"),
    Fixture("chatter_checkin", "Just checking in, how are you?", "chatter"),
]

ALL_FIXTURES: dict[str, list[Fixture]] = {
    "restates": RESTATES_FIXTURES,
    "new": NEW_FIXTURES,
    "changes": CHANGES_FIXTURES,
    "chatter": CHATTER_FIXTURES,
}


# =============================================================================
# SCORING
# =============================================================================


def run_benchmark() -> dict:
    """Classify every fixture with the deterministic tier and score it.

    Deliberately never passes ``use_llm``: this benchmark is about the
    zero-dependency default, the one that runs with no network at all.
    """
    fixtures = [f for group in ALL_FIXTURES.values() for f in group]

    confusion: dict[str, dict[str, int]] = {e: dict.fromkeys(OUTCOMES, 0) for e in OUTCOMES}
    escalated_count = 0
    agreement_count = 0
    mismatches: list[dict[str, str]] = []

    for fixture in fixtures:
        result = triage(
            fixture.candidate,
            fixture.existing,
            duplicate_matches=fixture.duplicate_matches,
        )
        confusion[fixture.expected][result.verdict] += 1
        if result.status == "abstained":
            escalated_count += 1
        else:
            agreement_count += 1
        if result.verdict != fixture.expected:
            mismatches.append(
                {
                    "id": fixture.fixture_id,
                    "expected": fixture.expected,
                    "got": result.verdict,
                    "detail": result.detail,
                }
            )

    per_outcome: dict[str, dict[str, float]] = {}
    for outcome in OUTCOMES:
        true_positive = confusion[outcome][outcome]
        predicted_total = sum(confusion[other][outcome] for other in OUTCOMES)
        actual_total = sum(confusion[outcome].values())
        precision = true_positive / predicted_total if predicted_total else 0.0
        recall = true_positive / actual_total if actual_total else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_outcome[outcome] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual_total,
        }

    total = len(fixtures)
    final_changes = sum(confusion[e]["changes"] for e in OUTCOMES)
    return {
        "total_fixtures": total,
        "confusion": confusion,
        "per_outcome": per_outcome,
        "mismatches": mismatches,
        "escalated": escalated_count,
        "escalation_rate": escalated_count / total if total else 0.0,
        "settled_without_llm_tier": (total - escalated_count) / total if total else 0.0,
        "final_verdict_changes_count": final_changes,
        "final_verdict_changes_rate": final_changes / total if total else 0.0,
        "agreement_count": agreement_count,
    }


def _print_report(res: dict) -> None:
    print(
        f"\nskmemory triage bench  |  {res['total_fixtures']} fixtures, "
        f"{len(OUTCOMES)} outcome classes\n"
    )

    label = "expected / got"
    header = f"{label:<18}" + "".join(f"{o:>11}" for o in OUTCOMES)
    print(header)
    print("-" * len(header))
    for expected in OUTCOMES:
        row = f"{expected:<18}"
        for got in OUTCOMES:
            row += f"{res['confusion'][expected][got]:>11d}"
        print(row)

    print("\nper-outcome precision / recall / F1 (deterministic tier only):\n")
    phead = f"{'outcome':<10}{'precision':>11}{'recall':>9}{'f1':>9}{'support':>9}"
    print(phead)
    print("-" * len(phead))
    for outcome in OUTCOMES:
        stats = res["per_outcome"][outcome]
        print(
            f"{outcome:<10}{stats['precision']:>11.3f}{stats['recall']:>9.3f}"
            f"{stats['f1']:>9.3f}{stats['support']:>9d}"
        )

    print(
        "\nagree-without-escalating: "
        f"{res['agreement_count']}/{res['total_fixtures']} "
        f"({1 - res['escalation_rate']:.3f})"
    )
    print(
        "escalated to 'changes' by disagreement: "
        f"{res['escalated']}/{res['total_fixtures']} ({res['escalation_rate']:.3f})\n"
    )
    print(
        "THE ECONOMIC NUMBER - fraction of candidates whose FINAL verdict is NOT "
        "'changes', i.e. settled without ever needing the expensive LLM rewrite:\n"
        f"    {1 - res['final_verdict_changes_rate']:.3f}  "
        f"({res['total_fixtures'] - res['final_verdict_changes_count']}/"
        f"{res['total_fixtures']})\n"
    )
    if res["mismatches"]:
        print(f"MISMATCHES ({len(res['mismatches'])}):")
        for m in res["mismatches"]:
            print(f"  {m['id']}: expected {m['expected']!r} got {m['got']!r} ({m['detail']})")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="skmemory triage classifier bench")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
